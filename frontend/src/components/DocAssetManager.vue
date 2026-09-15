<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { deleteDocAsset, uploadDocAsset, type DocAsset } from '@/api'
import { apiError } from '@/api/client'
import { useClipboard } from '@/composables/useClipboard'

/**
 * Lightweight asset manager for one documentation project.
 *
 * Every document owns its own `assets/` directory, so a screenshot belongs to
 * the document that uses it instead of every ecosystem sharing one flat pile.
 * This component uploads into that directory, lists what is there, and hands
 * the editor a ready-to-paste Markdown reference — always the **relative**
 * `assets/<name>` form, so the raw `.md` stays portable and the renderer makes
 * it absolute when serving the page.
 */
const props = defineProps<{
  ecosystem: string
  docId: string
  assets: DocAsset[]
}>()

const emit = defineEmits<{
  /** Insert a Markdown reference for an asset at the editor's cursor. */
  (event: 'insert', value: string): void
  /** An asset was uploaded or removed; the parent should refresh. */
  (event: 'changed'): void
}>()

const { t } = useI18n()
const { copy } = useClipboard()
const uploading = ref(false)
const busy = ref<string | null>(null)

async function onUpload(options: UploadRequestOptions): Promise<void> {
  const file = options.file as File
  uploading.value = true
  try {
    const asset = await uploadDocAsset(props.ecosystem, props.docId, file)
    ElMessage.success(t('docs.assetUploaded', { name: asset.name }))
    options.onSuccess?.(asset)
    emit('changed')
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.assetUploadFailed'))
    options.onError?.(e as Error)
  } finally {
    uploading.value = false
  }
}

function markdownFor(asset: DocAsset): string {
  return asset.is_image
    ? `![${asset.name}](assets/${asset.name})`
    : `[${asset.name}](assets/${asset.name})`
}

function insert(asset: DocAsset): void {
  emit('insert', markdownFor(asset))
}

async function copyLink(asset: DocAsset): Promise<void> {
  const absolute = new URL(asset.url, window.location.origin).href
  await copy(absolute)
}

async function remove(asset: DocAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('docs.assetDeleteConfirm', { name: asset.name }),
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
  busy.value = asset.name
  try {
    await deleteDocAsset(props.ecosystem, props.docId, asset.name)
    ElMessage.success(t('docs.assetDeleted'))
    emit('changed')
  } catch (e) {
    ElMessage.error(apiError(e) || t('docs.assetDeleteFailed'))
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <div class="asset-manager">
    <div class="asset-manager__bar">
      <el-upload
        :show-file-list="false"
        :http-request="onUpload"
      >
        <el-button size="small" :loading="uploading">
          <el-icon><Upload /></el-icon>
          <span class="btn-label">{{ t('docs.uploadAsset') }}</span>
        </el-button>
      </el-upload>
      <span class="asset-manager__hint">{{ t('docs.assetsHint') }}</span>
    </div>

    <el-empty
      v-if="assets.length === 0"
      :description="t('docs.noAssets')"
      :image-size="56"
    />

    <ul v-else class="asset-manager__list">
      <li v-for="asset in assets" :key="asset.name" class="asset-manager__item">
        <span class="asset-manager__thumb">
          <img v-if="asset.is_image" :src="asset.url" :alt="asset.name" loading="lazy" />
          <el-icon v-else><Document /></el-icon>
        </span>
        <span class="asset-manager__meta">
          <span class="asset-manager__name" :title="asset.name">{{ asset.name }}</span>
          <span class="asset-manager__size">{{ asset.size_human }}</span>
        </span>
        <span class="asset-manager__actions">
          <el-tooltip :content="t('docs.insert')" placement="top">
            <el-button link @click="insert(asset)">
              <el-icon><Plus /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip :content="t('docs.copyLink')" placement="top">
            <el-button link @click="copyLink(asset)">
              <el-icon><CopyDocument /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip :content="t('common.delete')" placement="top">
            <el-button
              link
              type="danger"
              :loading="busy === asset.name"
              @click="remove(asset)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </el-tooltip>
        </span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.asset-manager__bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.asset-manager__hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  flex: 1;
  min-width: 160px;
}

.asset-manager__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.asset-manager__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px;
  border-radius: 6px;
}

.asset-manager__item:hover {
  background: var(--el-fill-color-light);
}

.asset-manager__thumb {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-radius: 4px;
  background: var(--el-fill-color);
  color: var(--el-text-color-secondary);
}

.asset-manager__thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.asset-manager__meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.asset-manager__name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-manager__size {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.asset-manager__actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}
</style>
