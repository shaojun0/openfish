<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import {
  deleteDoc,
  fetchDoc,
  fetchDocCatalog,
  uploadDoc,
  type DocCatalog,
  type DocDetail,
  type DocEntry,
} from '@/api'
import { apiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'

/**
 * One ecosystem's documentation leaf.
 *
 * The route parameter picks the ecosystem (`/docs/python`, `/docs/npm`, …), so
 * every sidebar group reuses this single view.  Documents are Markdown: the
 * server renders and escapes them, and this component only displays the result.
 * Reading and downloading needs `doc:read`; the upload and delete controls are
 * shown to `doc:upload` holders (the built-in admin role), and the server
 * enforces the same rule regardless of what the UI chose to render.
 */

const { t } = useI18n()
const route = useRoute()
const session = useSessionStore()

const ecosystem = computed(() => String(route.params.ecosystem ?? ''))
const catalog = ref<DocCatalog | null>(null)
const detail = ref<DocDetail | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const uploading = ref(false)
const loadError = ref('')

const canUpload = computed(() => session.can('doc:upload'))
const documents = computed<DocEntry[]>(() => catalog.value?.documents ?? [])
const machineIndexUrl = computed(
  () => `/docs/${encodeURIComponent(ecosystem.value)}/`,
)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  detail.value = null
  try {
    catalog.value = await fetchDocCatalog(ecosystem.value)
    const first = catalog.value.documents[0]
    if (first) await select(first)
  } catch (e) {
    catalog.value = null
    loadError.value = apiError(e) || t('docs.loadFailed')
  } finally {
    loading.value = false
  }
}

async function select(entry: DocEntry): Promise<void> {
  detailLoading.value = true
  try {
    detail.value = await fetchDoc(ecosystem.value, entry.name)
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

/** `el-upload` custom request: only Markdown, and only for `doc:upload`. */
async function onUpload(options: UploadRequestOptions): Promise<void> {
  const file = options.file as File
  if (!file.name.toLowerCase().endsWith('.md')) {
    ElMessage.error(t('docs.onlyMarkdown'))
    options.onError?.(new Error('not a markdown file'))
    return
  }
  uploading.value = true
  try {
    const entry = await uploadDoc(ecosystem.value, file)
    ElMessage.success(t('docs.uploaded', { name: entry.name }))
    options.onSuccess?.(entry)
    await load()
    await select(entry)
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.uploadFailed'))
    options.onError?.(e as Error)
  } finally {
    uploading.value = false
  }
}

async function remove(entry: DocEntry): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('docs.deleteConfirm', { name: entry.name }),
      t('common.confirm'),
      { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') },
    )
  } catch {
    return
  }
  try {
    await deleteDoc(ecosystem.value, entry.name)
    ElMessage.success(t('docs.deleted'))
    if (detail.value?.name === entry.name) detail.value = null
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.deleteFailed'))
  }
}

watch(ecosystem, load, { immediate: true })
</script>

<template>
  <div class="page docs-view">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('docs.title', { ecosystem }) }}</h1>
        <p class="page__description">{{ t('docs.description') }}</p>
      </div>
      <div class="toolbar">
        <el-upload
          v-if="canUpload"
          accept=".md,text/markdown"
          :show-file-list="false"
          :http-request="onUpload"
        >
          <el-button type="primary" :loading="uploading">
            <el-icon><Upload /></el-icon>
            <span class="btn-label">{{ t('docs.upload') }}</span>
          </el-button>
        </el-upload>
        <el-button @click="openMachineIndex">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('docs.staticIndex') }}</span>
        </el-button>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="canUpload"
      class="docs-view__hint"
      type="info"
      show-icon
      :closable="false"
      :title="t('docs.adminHintTitle')"
      :description="t('docs.adminHint')"
    />

    <el-alert
      v-if="loadError"
      type="error"
      show-icon
      :closable="false"
      :title="t('docs.loadFailed')"
      :description="loadError"
    />

    <el-empty
      v-else-if="!loading && documents.length === 0"
      :description="t('docs.empty')"
    >
      <el-button :loading="loading" @click="load">{{ t('common.refresh') }}</el-button>
    </el-empty>

    <div v-else class="docs-view__body">
      <el-card class="docs-view__list" shadow="never">
        <template #header>
          <div class="docs-view__list-header">
            <span class="docs-view__list-title">{{ t('docs.documents') }}</span>
            <el-tag size="small" type="info" effect="plain">
              {{ t('common.total', { count: documents.length }) }}
            </el-tag>
          </div>
        </template>
        <ul class="docs-view__items">
          <li
            v-for="entry in documents"
            :key="entry.name"
            class="docs-view__item"
            :class="{ 'docs-view__item--active': detail?.name === entry.name }"
          >
            <button class="docs-view__item-main" type="button" @click="select(entry)">
              <span class="docs-view__item-title">{{ entry.title }}</span>
              <span class="docs-view__item-meta">
                {{ entry.filename }} · {{ entry.size_human }}
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
              {{ detail.filename }} · {{ detail.size_human }}
              <template v-if="detail.modified"> · {{ detail.modified.slice(0, 10) }}</template>
            </span>
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
  </div>
</template>

<style scoped>
.docs-view__hint {
  margin-bottom: 16px;
}

.docs-view__body {
  display: grid;
  grid-template-columns: minmax(220px, 300px) 1fr;
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

.docs-view__content-download {
  margin-left: auto;
}

.btn-label {
  margin-left: 4px;
}

/* The rendered body comes from `v-html`, so scoped styles need `:deep()`. */
.markdown {
  line-height: 1.7;
  color: var(--el-text-color-primary);
  word-wrap: break-word;
}

.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4) {
  margin: 1.2em 0 0.6em;
  line-height: 1.3;
}

.markdown :deep(h1) {
  font-size: 1.7em;
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding-bottom: 0.3em;
}

.markdown :deep(h2) {
  font-size: 1.4em;
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding-bottom: 0.25em;
}

.markdown :deep(p),
.markdown :deep(ul),
.markdown :deep(ol),
.markdown :deep(blockquote),
.markdown :deep(table) {
  margin: 0.7em 0;
}

.markdown :deep(code) {
  font-family: 'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 0.88em;
  background: var(--el-fill-color-light);
  padding: 2px 5px;
  border-radius: 4px;
}

.markdown :deep(pre) {
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px;
  overflow-x: auto;
}

.markdown :deep(pre code) {
  background: none;
  padding: 0;
}

.markdown :deep(blockquote) {
  border-left: 4px solid var(--el-border-color);
  padding: 2px 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
}

.markdown :deep(table) {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92em;
}

.markdown :deep(th),
.markdown :deep(td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 6px 10px;
  text-align: left;
}

.markdown :deep(th) {
  background: var(--el-fill-color-light);
}

.markdown :deep(hr) {
  border: none;
  border-top: 1px solid var(--el-border-color-lighter);
  margin: 1.4em 0;
}

.markdown :deep(img) {
  max-width: 100%;
}

.markdown :deep(a) {
  color: var(--el-color-primary);
  text-decoration: none;
}

.markdown :deep(a:hover) {
  text-decoration: underline;
}
</style>
