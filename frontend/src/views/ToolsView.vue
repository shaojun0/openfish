<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  fetchToolCatalog,
  uploadTool,
  type ToolCatalog,
  type ToolCategory,
  type ToolEntry,
} from '@/api'
import { apiError } from '@/api/client'
import ToolsCategoryTable from '@/components/ToolsCategoryTable.vue'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const session = useSessionStore()

const catalog = ref<ToolCatalog | null>(null)
const loading = ref(true)
const query = ref('')

/** Uploading is an administrative act (`tool:upload`), so the control is hidden without it. */
const canUpload = computed(() => session.can('tool:upload'))
const uploadVisible = ref(false)
const uploading = ref(false)
const uploadPercent = ref(0)
const uploadCategory = ref('')
const uploadFile = ref<File | null>(null)
const uploadInput = ref<HTMLInputElement | null>(null)

/** Keep a category only when the filter leaves something in it. */
const visibleCategories = computed<ToolCategory[]>(() => {
  const needle = query.value.trim().toLowerCase()
  const categories = catalog.value?.categories ?? []
  if (!needle) return categories.filter((c) => c.tools.length > 0 || !query.value)
  return categories
    .map((category) => ({
      ...category,
      tools: category.tools.filter((tool) => matches(tool, category, needle)),
    }))
    .filter((category) => category.tools.length > 0)
})

function matches(tool: ToolEntry, category: ToolCategory, needle: string): boolean {
  return [tool.name, tool.filename, tool.description ?? '', category.key, ...tool.tags]
    .join(' ')
    .toLowerCase()
    .includes(needle)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    catalog.value = await fetchToolCatalog()
  } catch (e) {
    ElMessage.error(apiError(e) || t('tools.loadFailed'))
  } finally {
    loading.value = false
  }
}

/** The server-rendered index — same data, no JavaScript, script-friendly. */
function openStaticIndex(): void {
  window.open('/tools/', '_blank', 'noopener')
}

// ── Upload (tool:upload) ─────────────────────────────────────────────

function openUpload(): void {
  uploadVisible.value = true
  uploadCategory.value = ''
  uploadFile.value = null
  uploadPercent.value = 0
  if (uploadInput.value) uploadInput.value.value = ''
}

function onUploadPick(event: Event): void {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0] ?? null
}

async function submitUpload(): Promise<void> {
  const file = uploadFile.value
  if (!file) {
    ElMessage.warning(t('tools.needFile'))
    return
  }
  uploading.value = true
  uploadPercent.value = 0
  try {
    const entry = await uploadTool(uploadCategory.value.trim(), file, (percent) => {
      uploadPercent.value = percent
    })
    ElMessage.success(t('tools.uploaded', { name: entry.filename }))
    uploadVisible.value = false
    await load()
  } catch (e) {
    // The server's 400/409/413 message names the exact problem; keep it.
    ElMessage.error(apiError(e) || t('tools.uploadFailed'))
  } finally {
    uploading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('tools.title') }}</h1>
        <p class="page__description">{{ t('tools.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button v-if="canUpload" type="primary" @click="openUpload">
          <el-icon><Upload /></el-icon>
          <span class="btn-label">{{ t('tools.upload') }}</span>
        </el-button>
        <el-button @click="openStaticIndex">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('tools.staticIndex') }}</span>
        </el-button>
        <el-input
          v-model="query"
          class="toolbar__search"
          clearable
          :placeholder="t('tools.filterPlaceholder')"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="!loading && catalog && !catalog.exists"
      type="info"
      show-icon
      :closable="false"
      :title="t('tools.missingTitle')"
      :description="t('tools.missingDesc', { root: catalog.root })"
    />

    <el-card shadow="never">
      <el-empty
        v-if="!loading && visibleCategories.length === 0"
        :description="query ? t('tools.emptyFiltered', { query }) : t('tools.empty')"
      >
        <el-button @click="load">{{ t('common.refresh') }}</el-button>
      </el-empty>

      <div v-else v-loading="loading" class="categories">
        <el-card
          v-for="category in visibleCategories"
          :key="category.key"
          class="category"
          shadow="never"
        >
          <template #header>
            <div class="category__header">
              <el-icon class="category__icon">
                <component :is="category.icon || 'FolderOpened'" />
              </el-icon>
              <span class="category__name">
                {{ category.name || t('tools.uncategorized') }}
              </span>
              <span v-if="category.description" class="category__desc">
                {{ category.description }}
              </span>
              <el-tag size="small" type="info" effect="plain">
                {{ t('tools.count', { count: category.tools.length }) }}
              </el-tag>
            </div>
          </template>

          <ToolsCategoryTable :category="category" />
        </el-card>
      </div>
    </el-card>

    <el-dialog
      v-model="uploadVisible"
      :title="t('tools.uploadTitle')"
      width="520px"
      :close-on-click-modal="!uploading"
      :close-on-press-escape="!uploading"
      :show-close="!uploading"
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="t('tools.categoryLabel')">
          <el-input
            v-model="uploadCategory"
            :disabled="uploading"
            :placeholder="t('tools.categoryPlaceholder')"
          />
        </el-form-item>
        <el-form-item :label="t('tools.fileLabel')">
          <input
            ref="uploadInput"
            class="tools-view__file-input"
            type="file"
            @change="onUploadPick"
          />
          <el-button :disabled="uploading" @click="uploadInput?.click()">
            <el-icon><Upload /></el-icon>
            <span class="btn-label">
              {{ uploadFile ? uploadFile.name : t('tools.chooseFile') }}
            </span>
          </el-button>
        </el-form-item>
        <el-progress v-if="uploading" :percentage="uploadPercent" :stroke-width="10" />
        <p class="tools-view__hint">{{ t('tools.uploadHint') }}</p>
      </el-form>
      <template #footer>
        <el-button :disabled="uploading" @click="uploadVisible = false">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="primary"
          :loading="uploading"
          :disabled="!uploadFile"
          @click="submitUpload"
        >
          {{ t('common.upload') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar__search {
  width: 260px;
}

.tools-view__file-input {
  display: none;
}

.tools-view__hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.categories {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.category {
  border: 1px solid var(--el-border-color-lighter);
}

.category__header {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.category__icon {
  color: var(--el-color-primary);
}

.category__name {
  font-weight: 600;
}

.category__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
