<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchToolCatalog, type ToolCatalog, type ToolCategory, type ToolEntry } from '@/api'
import { apiError } from '@/api/client'
import { formatDate } from '@/utils/format'

const { t } = useI18n()

const catalog = ref<ToolCatalog | null>(null)
const loading = ref(true)
const query = ref('')

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

/** The download URL is server-owned, so opening it is the whole action. */
function download(tool: ToolEntry): void {
  if (!tool.download_url) return
  window.open(tool.download_url, '_blank', 'noopener')
}

/** The server-rendered index — same data, no JavaScript, script-friendly. */
function openStaticIndex(): void {
  window.open('/tools/', '_blank', 'noopener')
}

async function copySha(tool: ToolEntry): Promise<void> {
  if (!tool.sha256) return
  try {
    await navigator.clipboard.writeText(tool.sha256)
    ElMessage.success(t('common.copied'))
  } catch {
    ElMessage.warning(t('common.copyFailed'))
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

          <el-table :data="category.tools" stripe>
            <el-table-column :label="t('tools.name')" min-width="240">
              <template #default="{ row }">
                <div class="tool">
                  <span class="tool__name">{{ row.name }}</span>
                  <span v-if="row.description" class="tool__desc">{{ row.description }}</span>
                  <span class="tool__tags">
                    <el-tag
                      v-for="tag in row.tags"
                      :key="tag"
                      size="small"
                      effect="plain"
                      type="primary"
                    >
                      {{ tag }}
                    </el-tag>
                  </span>
                </div>
              </template>
            </el-table-column>

            <el-table-column prop="filename" :label="t('tools.filename')" min-width="200">
              <template #default="{ row }">
                <span class="mono">{{ row.filename }}</span>
              </template>
            </el-table-column>

            <el-table-column :label="t('tools.size')" width="110" align="right">
              <template #default="{ row }">{{ row.size_human || '—' }}</template>
            </el-table-column>

            <el-table-column :label="t('tools.modified')" width="160">
              <template #default="{ row }">{{ formatDate(row.modified) }}</template>
            </el-table-column>

            <el-table-column :label="t('tools.sha256')" width="120" align="center">
              <template #default="{ row }">
                <el-tooltip v-if="row.sha256" :content="row.sha256">
                  <el-button size="small" text @click="copySha(row)">
                    <el-icon><CopyDocument /></el-icon>
                  </el-button>
                </el-tooltip>
                <span v-else>—</span>
              </template>
            </el-table-column>

            <el-table-column :label="t('common.actions')" width="130" align="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="download(row)">
                  <el-icon><Download /></el-icon>
                  <span class="btn-label">{{ t('tools.download') }}</span>
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar__search {
  width: 260px;
}

.btn-label {
  margin-left: 4px;
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

.tool {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.tool__name {
  font-weight: 500;
}

.tool__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tool__tags {
  display: flex;
  gap: 4px;
  margin-top: 2px;
}
</style>
