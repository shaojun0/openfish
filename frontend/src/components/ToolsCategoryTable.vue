<script setup lang="ts">
/**
 * One tools category's table, with its own pagination state.
 *
 * The tools page renders a card per category, so each table needs an
 * independent pager; keeping the table (and its `usePagination`) in a child
 * component gives every category that state without a hand-rolled map in the
 * parent.  The category card header stays in the parent view.
 */
import { ElMessage } from 'element-plus'
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ToolCategory, ToolEntry } from '@/api'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { formatDate } from '@/utils/format'

const props = defineProps<{ category: ToolCategory }>()

const { t } = useI18n()

const tools = computed<ToolEntry[]>(() => props.category.tools)
const { page, pageSize, pageSizes, total, rows, reset } = usePagination(tools, {
  pageSize: 10,
  pageSizes: [10, 20, 50],
})

// A search that changes what this category holds is a new result set.
watch(tools, reset)

function download(tool: ToolEntry): void {
  if (!tool.download_url) return
  window.open(tool.download_url, '_blank', 'noopener')
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
</script>

<template>
  <el-table :data="rows" stripe>
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

  <TablePager
    v-model:page="page"
    v-model:page-size="pageSize"
    :page-sizes="pageSizes"
    :total="total"
  />
</template>

<style scoped>
.btn-label {
  margin-left: 4px;
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
