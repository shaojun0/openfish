<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import {
  createDoc,
  deleteDoc,
  fetchDoc,
  fetchDocCatalog,
  type DocCatalog,
  type DocDetail,
  type DocEntry,
} from '@/api'
import { apiError } from '@/api/client'
import DocEditor from '@/components/DocEditor.vue'
import { useSessionStore } from '@/stores/session'

/**
 * One ecosystem's documentation leaf.
 *
 * The route parameter picks the ecosystem (`/documentation/python`,
 * `/documentation/npm`, …), so every sidebar group reuses this single view.
 * The server-rendered index and the raw Markdown stay under `/docs/<eco>/`
 * (both `doc:read`); this page is the public SPA shell one namespace over.
 * A document is a folder project
 * with its own assets: readers can view and download it, and an administrator
 * (`doc:upload`) can create one from the list's "+" control, edit it in the
 * browser, or delete it.  The server enforces the same rule regardless of what
 * the UI chose to render.
 */
const { t } = useI18n()
const route = useRoute()
const session = useSessionStore()

const ecosystem = computed(() => String(route.params.ecosystem ?? ''))
const catalog = ref<DocCatalog | null>(null)
const detail = ref<DocDetail | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const loadError = ref('')

const adding = ref(false)
const newTitle = ref('')
const newFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const creating = ref(false)
const editorVisible = ref(false)

const canUpload = computed(() => session.can('doc:upload'))
const documents = computed<DocEntry[]>(() => catalog.value?.documents ?? [])
const machineIndexUrl = computed(
  () => `/docs/${encodeURIComponent(ecosystem.value)}/`,
)

async function load(keepSelection = false): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    catalog.value = await fetchDocCatalog(ecosystem.value)
    const wanted = keepSelection ? detail.value?.id : undefined
    const target =
      catalog.value.documents.find((entry) => entry.id === wanted) ??
      catalog.value.documents[0]
    if (target) await select(target)
    else detail.value = null
  } catch (e) {
    catalog.value = null
    detail.value = null
    loadError.value = apiError(e) || t('docs.loadFailed')
  } finally {
    loading.value = false
  }
}

/** Refetch only the catalog, keeping the current document open. */
async function refreshCatalog(): Promise<void> {
  try {
    catalog.value = await fetchDocCatalog(ecosystem.value)
  } catch {
    // The next explicit refresh will surface the error.
  }
}

async function select(entry: DocEntry): Promise<void> {
  detailLoading.value = true
  try {
    detail.value = await fetchDoc(ecosystem.value, entry.id)
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.loadFailed'))
  } finally {
    detailLoading.value = false
  }
}

/** Raw `.md` download — the browser sends the session cookie itself. */
function download(entry: DocEntry): void {
  window.open(entry.download_url, '_blank', 'noopener')
}

function openMachineIndex(): void {
  window.open(machineIndexUrl.value, '_blank', 'noopener')
}

// ── Create ───────────────────────────────────────────────────────────

function startAdd(): void {
  adding.value = true
  newTitle.value = ''
  newFile.value = null
  void nextTick(() => {
    const input = document.querySelector<HTMLInputElement>('.docs-view__title-input input')
    input?.focus()
  })
}

function cancelAdd(): void {
  adding.value = false
  newTitle.value = ''
  newFile.value = null
  if (fileInput.value) fileInput.value.value = ''
}

function onFilePick(event: Event): void {
  const input = event.target as HTMLInputElement
  newFile.value = input.files?.[0] ?? null
}

async function create(): Promise<void> {
  const title = newTitle.value.trim()
  if (!title && !newFile.value) {
    ElMessage.warning(t('docs.needTitleOrFile'))
    return
  }
  creating.value = true
  try {
    const result = await createDoc(ecosystem.value, title, newFile.value)
    ElMessage.success(
      result.replaced
        ? t('docs.replaced', { title: result.document.title })
        : t('docs.created', { title: result.document.title }),
    )
    const created = result.document
    cancelAdd()
    await load()
    if (created) await select(created)
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.createFailed'))
  } finally {
    creating.value = false
  }
}

// ── Edit / delete ────────────────────────────────────────────────────

function openEditor(): void {
  if (detail.value) editorVisible.value = true
}

function onSaved(saved: DocDetail): void {
  detail.value = saved
  void refreshCatalog()
}

async function remove(entry: DocEntry): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('docs.deleteConfirm', { title: entry.title }),
      t('common.confirm'),
      {
        type: 'warning',
        confirmButtonText: t('common.delete'),
        cancelButtonText: t('common.cancel'),
      },
    )
  } catch {
    return
  }
  try {
    await deleteDoc(ecosystem.value, entry.id)
    ElMessage.success(t('docs.deleted'))
    if (detail.value?.id === entry.id) detail.value = null
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.deleteFailed'))
  }
}

watch(ecosystem, () => load(), { immediate: true })
</script>

<template>
  <div class="page docs-view">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('docs.title', { ecosystem }) }}</h1>
        <p class="page__description">{{ t('docs.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button
          v-if="canUpload"
          type="primary"
          :disabled="!detail"
          @click="openEditor"
        >
          <el-icon><EditPen /></el-icon>
          <span class="btn-label">{{ t('docs.edit') }}</span>
        </el-button>
        <el-button @click="openMachineIndex">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('docs.staticIndex') }}</span>
        </el-button>
        <el-button :loading="loading" @click="load(true)">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      show-icon
      :closable="false"
      :title="t('docs.loadFailed')"
      :description="loadError"
    />

    <el-empty
      v-else-if="!loading && documents.length === 0 && !adding"
      :description="t('docs.empty')"
    >
      <el-button v-if="canUpload" type="primary" @click="startAdd">
        <el-icon><Plus /></el-icon>
        <span class="btn-label">{{ t('docs.addDocument') }}</span>
      </el-button>
      <el-button v-else :loading="loading" @click="load()">
        {{ t('common.refresh') }}
      </el-button>
    </el-empty>

    <div v-else class="docs-view__body">
      <el-card class="docs-view__list" shadow="never">
        <template #header>
          <div class="docs-view__list-header">
            <span class="docs-view__list-title">{{ t('docs.documents') }}</span>
            <span class="docs-view__list-tools">
              <el-tag size="small" type="info" effect="plain">
                {{ t('common.total', { count: documents.length }) }}
              </el-tag>
              <el-tooltip
                v-if="canUpload"
                :content="t('docs.addDocument')"
                placement="top"
              >
                <el-button
                  link
                  class="docs-view__add"
                  :disabled="adding"
                  @click="startAdd"
                >
                  <el-icon><Plus /></el-icon>
                </el-button>
              </el-tooltip>
            </span>
          </div>
        </template>

        <div v-if="adding" class="docs-view__add-row">
          <el-input
            v-model="newTitle"
            class="docs-view__title-input"
            size="small"
            :placeholder="t('docs.newTitlePlaceholder')"
            @keyup.enter="create"
          />
          <div class="docs-view__add-actions">
            <input
              ref="fileInput"
              class="docs-view__file-input"
              type="file"
              accept=".md,text/markdown"
              @change="onFilePick"
            />
            <el-button size="small" @click="fileInput?.click()">
              <el-icon><Upload /></el-icon>
              <span class="btn-label docs-view__file-label">
                {{ newFile ? newFile.name : t('docs.chooseFile') }}
              </span>
            </el-button>
            <el-button size="small" type="primary" :loading="creating" @click="create">
              {{ t('common.create') }}
            </el-button>
            <el-button size="small" @click="cancelAdd">
              {{ t('common.cancel') }}
            </el-button>
          </div>
          <p class="docs-view__add-hint">{{ t('docs.emptyIfNoFile') }}</p>
        </div>

        <ul class="docs-view__items">
          <li
            v-for="entry in documents"
            :key="entry.id"
            class="docs-view__item"
            :class="{ 'docs-view__item--active': detail?.id === entry.id }"
          >
            <button class="docs-view__item-main" type="button" @click="select(entry)">
              <span class="docs-view__item-title">{{ entry.title }}</span>
              <span class="docs-view__item-meta">
                {{ entry.id }} · {{ entry.size_human }}
                <template v-if="entry.asset_count"> · {{ t('docs.assetCount', { count: entry.asset_count }) }}</template>
              </span>
            </button>
            <span class="docs-view__item-actions">
              <el-tooltip :content="t('docs.download')" placement="top">
                <el-button link @click="download(entry)">
                  <el-icon><Download /></el-icon>
                </el-button>
              </el-tooltip>
              <el-tooltip v-if="canUpload" :content="t('common.delete')" placement="top">
                <el-button link type="danger" @click="remove(entry)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </el-tooltip>
            </span>
          </li>
        </ul>
      </el-card>

      <el-card v-loading="detailLoading" class="docs-view__content" shadow="never">
        <template v-if="detail" #header>
          <div class="docs-view__content-header">
            <span class="docs-view__content-title">{{ detail.title }}</span>
            <span class="docs-view__content-meta">
              {{ detail.id }}/document.md · {{ detail.size_human }}
              <template v-if="detail.modified"> · {{ detail.modified.slice(0, 10) }}</template>
            </span>
            <el-button
              v-if="canUpload"
              class="docs-view__content-edit"
              link
              @click="openEditor"
            >
              <el-icon><EditPen /></el-icon>
              <span class="btn-label">{{ t('docs.edit') }}</span>
            </el-button>
            <el-button class="docs-view__content-download" link @click="download(detail)">
              <el-icon><Download /></el-icon>
              <span class="btn-label">{{ t('docs.download') }}</span>
            </el-button>
          </div>
        </template>
        <div v-if="detail" class="markdown" v-html="detail.html" />
        <el-empty v-else :description="t('docs.selectOne')" />
      </el-card>
    </div>

    <DocEditor
      v-model:visible="editorVisible"
      :ecosystem="ecosystem"
      :doc="detail"
      @saved="onSaved"
      @changed="refreshCatalog"
    />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.docs-view__body {
  display: grid;
  grid-template-columns: minmax(240px, 320px) 1fr;
  gap: 16px;
  align-items: start;
}

@media (max-width: 900px) {
  .docs-view__body {
    grid-template-columns: 1fr;
  }
}

.docs-view__list {
  border: 1px solid var(--el-border-color-lighter);
}

.docs-view__list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.docs-view__list-title {
  font-weight: 600;
}

.docs-view__list-tools {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.docs-view__add {
  padding: 2px;
}

.docs-view__add-row {
  display: grid;
  gap: 8px;
  padding: 8px;
  margin-bottom: 8px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.docs-view__add-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.docs-view__file-input {
  display: none;
}

.docs-view__file-label {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.docs-view__add-hint {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.docs-view__items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.docs-view__item {
  display: flex;
  align-items: center;
  gap: 4px;
  border-radius: 6px;
  padding: 2px 4px;
}

.docs-view__item:hover {
  background: var(--el-fill-color-light);
}

.docs-view__item--active {
  background: var(--el-color-primary-light-9);
}

.docs-view__item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: none;
  border: none;
  padding: 6px 4px;
  text-align: left;
  cursor: pointer;
  color: inherit;
  font: inherit;
}

.docs-view__item-title {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.docs-view__item-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.docs-view__item-actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

.docs-view__content {
  border: 1px solid var(--el-border-color-lighter);
  min-height: 320px;
}

.docs-view__content-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.docs-view__content-title {
  font-weight: 600;
}

.docs-view__content-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.docs-view__content-edit {
  margin-left: auto;
}

.docs-view__content-download {
  margin-left: 0;
}

.btn-label {
  margin-left: 4px;
}
</style>
