<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchAdminStats, refreshAdminStats, type AdminStats, type PackageSummary } from '@/api'
import { apiError } from '@/api/client'
import StatCard from '@/components/StatCard.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { formatDate } from '@/utils/format'

const { t } = useI18n()

const stats = ref<AdminStats | null>(null)
const loading = ref(true)
const refreshing = ref(false)

const adminPackages = computed<PackageSummary[]>(() => stats.value?.packages ?? [])
const adminKeys = computed<AdminStats['keys']>(() => stats.value?.keys ?? [])

const {
  page: packagesPage,
  pageSize: packagesPageSize,
  pageSizes: packagesPageSizes,
  total: packagesTotal,
  rows: packagesRows,
  onSortChange: onPackagesSortChange,
} = usePagination(adminPackages)

const {
  page: keysPage,
  pageSize: keysPageSize,
  pageSizes: keysPageSizes,
  total: keysTotal,
  rows: keysRows,
  onSortChange: onKeysSortChange,
} = usePagination(adminKeys)

async function load(): Promise<void> {
  loading.value = true
  try {
    stats.value = await fetchAdminStats()
  } catch (e) {
    ElMessage.error(apiError(e) || t('admin.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function refresh(): Promise<void> {
  refreshing.value = true
  try {
    await refreshAdminStats()
    await load()
    ElMessage.success(t('admin.refreshed'))
  } catch (e) {
    ElMessage.error(apiError(e) || t('admin.refreshFailed'))
  } finally {
    refreshing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('admin.title') }}</h1>
        <p class="page__description">{{ t('admin.description') }}</p>
      </div>
      <el-button type="primary" :loading="refreshing" @click="refresh">
        <el-icon><Refresh /></el-icon>
        <span class="btn-label">{{ t('admin.refresh') }}</span>
      </el-button>
    </div>

    <div class="stat-grid">
      <StatCard
        :label="t('stat.packages')"
        :value="stats?.overview.package_count ?? 0"
        icon="Box"
      />
      <StatCard
        :label="t('stat.files')"
        :value="stats?.overview.file_count ?? 0"
        icon="Document"
        tone="info"
      />
      <StatCard
        :label="t('stat.storage')"
        :value="stats?.overview.total_storage_human ?? '0 B'"
        icon="Coin"
        tone="warning"
      />
      <StatCard
        :label="t('stat.downloads')"
        :value="stats?.overview.total_downloads ?? 0"
        icon="Download"
        tone="success"
      />
      <StatCard
        :label="t('stat.uploads')"
        :value="stats?.overview.total_uploads ?? 0"
        icon="Upload"
      />
      <StatCard
        :label="t('stat.activeKeys')"
        :value="`${stats?.overview.active_keys ?? 0} / ${stats?.overview.total_keys ?? 0}`"
        icon="Key"
        tone="danger"
      />
    </div>

    <el-card shadow="never">
      <template #header><span class="card-title">{{ t('admin.packagesTitle') }}</span></template>
      <el-table :data="packagesRows" stripe @sort-change="onPackagesSortChange">
        <el-table-column type="index" :label="t('admin.rank')" width="70" />
        <el-table-column prop="name" :label="t('packages.name')" min-width="220" sortable="custom">
          <template #default="{ row }">
            <a class="pkg-link mono" :href="`/simple/${encodeURIComponent(row.name)}/`" target="_blank" rel="noopener">
              {{ row.name }}
            </a>
          </template>
        </el-table-column>
        <el-table-column prop="file_count" :label="t('packages.files')" width="100" align="right" sortable="custom" />
        <el-table-column prop="total_size" :label="t('packages.size')" width="130" align="right" sortable="custom">
          <template #default="{ row }">{{ row.total_size_human }}</template>
        </el-table-column>
        <el-table-column prop="download_count" :label="t('packages.downloads')" width="120" align="right" sortable="custom" />
        <el-table-column prop="upload_count" :label="t('packages.uploads')" width="110" align="right" sortable="custom" />
      </el-table>

      <TablePager
        v-model:page="packagesPage"
        v-model:page-size="packagesPageSize"
        :page-sizes="packagesPageSizes"
        :total="packagesTotal"
      />
    </el-card>

    <el-card shadow="never">
      <template #header><span class="card-title">{{ t('admin.keysTitle') }}</span></template>
      <el-table :data="keysRows" stripe @sort-change="onKeysSortChange">
        <el-table-column prop="name" :label="t('keys.table.name')" min-width="180" />
        <el-table-column prop="created_by" :label="t('keys.table.prefix')" min-width="160" />
        <el-table-column prop="download_count" :label="t('keys.table.downloads')" width="120" align="right" sortable="custom" />
        <el-table-column prop="upload_count" :label="t('keys.table.uploads')" width="110" align="right" sortable="custom" />
        <el-table-column :label="t('keys.table.lastUsed')" width="160">
          <template #default="{ row }">{{ formatDate(row.last_used) }}</template>
        </el-table-column>
        <el-table-column :label="t('keys.table.status')" width="110">
          <template #default="{ row }">
            <el-tag :type="row.is_expired ? 'danger' : row.is_permanent ? 'info' : 'success'" size="small" effect="plain">
              {{
                row.is_expired
                  ? t('keys.status.expired')
                  : row.is_permanent
                    ? t('keys.status.permanent')
                    : t('keys.status.active')
              }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="keysPage"
        v-model:page-size="keysPageSize"
        :page-sizes="keysPageSizes"
        :total="keysTotal"
      />
    </el-card>
  </div>
</template>

<style scoped>
.card-title {
  font-weight: 600;
}

.btn-label {
  margin-left: 4px;
}

.pkg-link {
  color: var(--el-color-primary);
  text-decoration: none;
}

.pkg-link:hover {
  text-decoration: underline;
}
</style>
