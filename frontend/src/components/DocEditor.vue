<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  fetchDocAssets,
  renderDocPreview,
  saveDoc,
  type DocAsset,
  type DocDetail,
} from '@/api'
import { apiError } from '@/api/client'
import DocAssetManager from '@/components/DocAssetManager.vue'

/**
 * In-browser Markdown editor for one documentation document.
 *
 * Modelled on GitHub's Markdown editor — a formatting toolbar over a source
 * box with a live preview — with a split view so the rendered result updates
 * as you type (the "Typora effect" without pulling in an editor dependency).
 * The preview is rendered by the *server* renderer through
 * `POST …/preview`, so what you see is exactly what the saved document will
 * show.  Images come from the document's own `assets/` folder via the asset
 * manager, and are referenced relatively so the raw `.md` stays portable.
 */
const props = defineProps<{
  visible: boolean
  ecosystem: string
  doc: DocDetail | null
}>()

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'saved', doc: DocDetail): void
  (event: 'changed'): void
}>()

const { t } = useI18n()

const sourceRef = ref<HTMLTextAreaElement | null>(null)
const content = ref('')
const baseline = ref('')
const previewHtml = ref('')
const assets = ref<DocAsset[]>([])
const mode = ref<'edit' | 'split' | 'preview'>('split')
const saving = ref(false)
const previewing = ref(false)
const assetsOpen = ref(false)

const dirty = computed(() => content.value !== baseline.value)
let previewTimer: ReturnType<typeof setTimeout> | undefined
let previewSeq = 0

// ── Preview ──────────────────────────────────────────────────────────

function schedulePreview(): void {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(() => void runPreview(), 250)
}

async function runPreview(): Promise<void> {
  if (!props.doc) return
  const seq = ++previewSeq
  previewing.value = true
  try {
    const html = await renderDocPreview(props.ecosystem, props.doc.id, content.value)
    if (seq === previewSeq) previewHtml.value = html
  } catch {
    // Keep the last good preview rather than blanking the pane.
  } finally {
    if (seq === previewSeq) previewing.value = false
  }
}

// ── Source editing helpers ───────────────────────────────────────────

type Edit = { text: string; selectStart: number; selectEnd: number }

function replaceSelection(build: (selected: string) => Edit): void {
  const el = sourceRef.value
  if (!el) return
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  const selected = content.value.slice(start, end)
  const edit = build(selected)
  content.value = content.value.slice(0, start) + edit.text + content.value.slice(end)
  void nextTick(() => {
    const node = sourceRef.value
    if (!node) return
    node.focus()
    node.setSelectionRange(start + edit.selectStart, start + edit.selectEnd)
  })
}

function insertText(text: string, cursor?: number): void {
  const at = cursor ?? text.length
  replaceSelection(() => ({ text, selectStart: at, selectEnd: at }))
}

function wrap(before: string, after = before, placeholder = ''): void {
  replaceSelection((selected) => {
    const body = selected || placeholder
    return {
      text: `${before}${body}${after}`,
      selectStart: before.length,
      selectEnd: before.length + body.length,
    }
  })
}

const LINE_MARKER_RE = /^(\s*)(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*)?/

function linePrefix(prefix: string, ordered = false): void {
  const el = sourceRef.value
  if (!el) return
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  const lineStart = content.value.lastIndexOf('\n', Math.max(0, start - 1)) + 1
  const block = content.value.slice(lineStart, end)
  const text = block
    .split('\n')
    .map((line, index) => {
      const stripped = line.replace(LINE_MARKER_RE, '$1')
      return ordered ? `${index + 1}. ${stripped}` : `${prefix}${stripped}`
    })
    .join('\n')
  content.value = content.value.slice(0, lineStart) + text + content.value.slice(end)
  void nextTick(() => {
    const node = sourceRef.value
    if (!node) return
    node.focus()
    node.setSelectionRange(lineStart, lineStart + text.length)
  })
}

function codeBlock(): void {
  replaceSelection((selected) => {
    const body = selected || 'code'
    return {
      text: '```\n' + body + '\n```',
      selectStart: 4,
      selectEnd: 4 + body.length,
    }
  })
}

function tableBlock(): void {
  insertText('\n| 列 1 | 列 2 |\n| --- | --- |\n| 内容 | 内容 |\n')
}

/** The Markdown for an empty image, with the cursor placed inside the target. */
const IMAGE_PLACEHOLDER = '![alt](assets/)'

/** One toolbar button: a text label (optionally emphasised) or an icon. */
interface ToolButton {
  /** i18n key for the tooltip; unique, so the loop uses it as its key too. */
  titleKey: string
  text?: string
  textTag?: 'strong' | 'em' | 's'
  /** A globally-registered Element Plus icon name; used when there is no text. */
  icon?: string
  run: () => void
}

/**
 * The whole toolbar as data — one entry per format instead of five
 * hand-written button groups, so the template stays a single loop.
 */
const toolbar = computed<ToolButton[][]>(() => [
  [
    { titleKey: 'docs.fmtH1', text: 'H1', run: () => linePrefix('# ') },
    { titleKey: 'docs.fmtH2', text: 'H2', run: () => linePrefix('## ') },
    { titleKey: 'docs.fmtH3', text: 'H3', run: () => linePrefix('### ') },
  ],
  [
    { titleKey: 'docs.fmtBold', text: 'B', textTag: 'strong', run: () => wrap('**', '**', 'bold') },
    { titleKey: 'docs.fmtItalic', text: 'I', textTag: 'em', run: () => wrap('*', '*', 'italic') },
    { titleKey: 'docs.fmtStrike', text: 'S', textTag: 's', run: () => wrap('~~', '~~', 'strike') },
    { titleKey: 'docs.fmtInlineCode', icon: 'Operation', run: () => wrap('`', '`', 'code') },
    { titleKey: 'docs.fmtCodeBlock', icon: 'Document', run: codeBlock },
  ],
  [
    { titleKey: 'docs.fmtQuote', text: '❝', run: () => linePrefix('> ') },
    { titleKey: 'docs.fmtBullet', text: '•', run: () => linePrefix('- ') },
    { titleKey: 'docs.fmtOrdered', text: '1.', run: () => linePrefix('', true) },
  ],
  [
    { titleKey: 'docs.fmtLink', icon: 'Link', run: () => wrap('[', '](https://)', 'text') },
    {
      titleKey: 'docs.fmtImage',
      icon: 'Picture',
      run: () => insertText(IMAGE_PLACEHOLDER, IMAGE_PLACEHOLDER.length - 1),
    },
    { titleKey: 'docs.fmtTable', icon: 'Grid', run: tableBlock },
    { titleKey: 'docs.fmtHr', text: '—', run: () => insertText('\n---\n') },
  ],
])

function onKeydown(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void save()
    return
  }
  if (event.key === 'Tab') {
    event.preventDefault()
    insertText('  ')
  }
}

// ── Assets ───────────────────────────────────────────────────────────

function insertAsset(markdown: string): void {
  insertText(`\n${markdown}\n`)
  if (mode.value === 'edit') mode.value = 'split'
}

async function refreshAssets(): Promise<void> {
  if (!props.doc) return
  try {
    assets.value = await fetchDocAssets(props.ecosystem, props.doc.id)
    emit('changed')
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.assetLoadFailed'))
  }
}

// ── Save / close ─────────────────────────────────────────────────────

async function save(): Promise<void> {
  if (!props.doc || saving.value) return
  saving.value = true
  try {
    const detail = await saveDoc(props.ecosystem, props.doc.id, content.value)
    baseline.value = content.value
    assets.value = detail.assets
    previewHtml.value = detail.html
    ElMessage.success(t('docs.saved'))
    emit('saved', detail)
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function confirmDiscard(): Promise<boolean> {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(t('docs.unsavedClose'), t('common.confirm'), {
      type: 'warning',
      confirmButtonText: t('docs.discard'),
      cancelButtonText: t('common.cancel'),
    })
    return true
  } catch {
    return false
  }
}

function requestClose(): void {
  void confirmDiscard().then((ok) => {
    if (ok) emit('update:visible', false)
  })
}

function beforeClose(done: () => void): void {
  void confirmDiscard().then((ok) => {
    if (ok) done()
  })
}

function onClosed(): void {
  if (previewTimer) clearTimeout(previewTimer)
  assetsOpen.value = false
}

// ── Lifecycle ────────────────────────────────────────────────────────

watch(
  () => props.visible,
  (open) => {
    if (!open || !props.doc) return
    content.value = props.doc.content
    baseline.value = props.doc.content
    previewHtml.value = props.doc.html
    assets.value = props.doc.assets
    mode.value = 'split'
    void runPreview()
  },
)

watch(content, () => {
  if (mode.value !== 'edit') schedulePreview()
})

watch(mode, (value) => {
  if (value !== 'edit') void runPreview()
})

onBeforeUnmount(() => {
  if (previewTimer) clearTimeout(previewTimer)
})
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="t('docs.editTitle')"
    width="min(1400px, 94vw)"
    top="4vh"
    :close-on-click-modal="false"
    :before-close="beforeClose"
    class="doc-editor-dialog"
    @update:model-value="emit('update:visible', $event)"
    @closed="onClosed"
  >
    <div v-if="doc" class="doc-editor">
      <div class="doc-editor__toolbar">
        <el-button-group v-for="(group, index) in toolbar" :key="index">
          <el-tooltip
            v-for="button in group"
            :key="button.titleKey"
            :content="t(button.titleKey)"
            placement="top"
          >
            <el-button size="small" @click="button.run">
              <component :is="button.textTag" v-if="button.textTag">{{ button.text }}</component>
              <el-icon v-else-if="button.icon"><component :is="button.icon" /></el-icon>
              <template v-else>{{ button.text }}</template>
            </el-button>
          </el-tooltip>
        </el-button-group>

        <span class="doc-editor__spacer" />

        <el-button size="small" @click="assetsOpen = true">
          <el-icon><FolderOpened /></el-icon>
          <span class="btn-label">{{ t('docs.assets') }}</span>
          <el-tag v-if="assets.length" size="small" class="doc-editor__count">
            {{ assets.length }}
          </el-tag>
        </el-button>

        <span class="doc-editor__modes">
          <el-button
            size="small"
            :type="mode === 'edit' ? 'primary' : 'default'"
            @click="mode = 'edit'"
          >
            {{ t('docs.modeEdit') }}
          </el-button>
          <el-button
            size="small"
            :type="mode === 'split' ? 'primary' : 'default'"
            @click="mode = 'split'"
          >
            {{ t('docs.modeSplit') }}
          </el-button>
          <el-button
            size="small"
            :type="mode === 'preview' ? 'primary' : 'default'"
            @click="mode = 'preview'"
          >
            {{ t('docs.modePreview') }}
          </el-button>
        </span>
      </div>

      <div class="doc-editor__panes" :class="`doc-editor__panes--${mode}`">
        <textarea
          v-show="mode !== 'preview'"
          ref="sourceRef"
          v-model="content"
          class="doc-editor__source"
          spellcheck="false"
          :placeholder="t('docs.editorPlaceholder')"
          @keydown="onKeydown"
        />
        <div
          v-show="mode !== 'edit'"
          v-loading="previewing"
          class="doc-editor__preview markdown"
          v-html="previewHtml"
        />
      </div>

      <div class="doc-editor__footer">
        <span class="doc-editor__status">
          <span :class="dirty ? 'doc-editor__dirty' : 'doc-editor__clean'">
            {{ dirty ? t('docs.unsavedBadge') : t('docs.savedBadge') }}
          </span>
          <code class="doc-editor__path">{{ doc.id }}/document.md</code>
        </span>
        <span class="doc-editor__footer-actions">
          <el-button @click="requestClose">{{ t('common.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="save">
            <el-icon><Check /></el-icon>
            <span class="btn-label">{{ t('docs.save') }}</span>
          </el-button>
        </span>
      </div>
    </div>

    <el-drawer
      v-model="assetsOpen"
      :title="t('docs.assets')"
      size="380px"
      append-to-body
    >
      <DocAssetManager
        v-if="doc"
        :ecosystem="ecosystem"
        :doc-id="doc.id"
        :assets="assets"
        @insert="insertAsset"
        @changed="refreshAssets"
      />
    </el-drawer>
  </el-dialog>
</template>

<style scoped>
.doc-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.doc-editor__toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.doc-editor__spacer {
  flex: 1;
}

.doc-editor__count {
  margin-left: 6px;
}

.doc-editor__modes {
  display: inline-flex;
  gap: 6px;
}

.doc-editor__panes {
  display: grid;
  gap: 12px;
}

.doc-editor__panes--split {
  grid-template-columns: 1fr 1fr;
}

.doc-editor__panes--edit,
.doc-editor__panes--preview {
  grid-template-columns: 1fr;
}

@media (max-width: 900px) {
  .doc-editor__panes--split {
    grid-template-columns: 1fr;
  }
}

.doc-editor__source {
  width: 100%;
  height: 62vh;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-primary);
  font-family: 'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 13.5px;
  line-height: 1.7;
  tab-size: 2;
  resize: none;
  outline: none;
}

.doc-editor__source:focus {
  border-color: var(--el-color-primary);
}

.doc-editor__preview {
  height: 62vh;
  overflow: auto;
  padding: 0 16px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
}

.doc-editor__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.doc-editor__status {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.doc-editor__dirty {
  color: var(--el-color-warning);
}

.doc-editor__clean {
  color: var(--el-color-success);
}

.doc-editor__path {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  padding: 2px 6px;
  border-radius: 4px;
}

.doc-editor__footer-actions {
  display: inline-flex;
  gap: 8px;
}
</style>
