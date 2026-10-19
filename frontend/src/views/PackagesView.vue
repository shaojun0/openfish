<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchPackages, fetchStatsOverview, type PackageSummary, type StatsOverview } from '@/api'
import { apiError } from '@/api/client'
import { appUrl } from '@/base'
import BuildCatalogView from '@/components/BuildCatalogView.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { formatBytes } from '@/utils/format'

const { t } = useI18n()

/**
 * One Python page, two sub-elements: the package list and the prebuilt
 * CPython mirror.  The dropdown picks which one the page is showing, so the
 * two live together instead of as separate sidebar entries.
 */
const infoType = ref<'package' | 'build'>('package')

const packages = ref<PackageSummary[]>([])
const loading = ref(true)
const query = ref('')
const loadError = ref('')
/**
 * Sizes come from the server's cached statistics snapshot. When the background
 * refresh has not finished its first `stat` walk yet, `total_size` is 0 for
 * every row; `sizes_ready` tells the page to say so instead of pretending the
 * registry is empty.
 */
const sizesReady = ref(true)

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return packages.value
  return packages.value.filter((p) => p.name.toLowerCase().includes(needle))
})

const { page, pageSize, pageSizes, total, rows, onSortChange, reset } = usePagination(filtered)

const heading = computed(() =>
  infoType.value === 'package'
    ? { title: t('packages.title'), description: t('packages.description') }
    : { title: t('build.pythonTitle'), description: t('build.pythonDescription') },
)

// A new search is a new result set: start it from the first page.
watch(query, reset)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    // The package list is cache-first on the server, so this answers even while
    // the background statistics pass is still running; the overview tells us
    // whether the sizes in that answer are final.
    const [list, overview] = await Promise.all([
      fetchPackages(),
      fetchStatsOverview().catch((): StatsOverview | null => null),
    ])
    packages.value = list
    sizesReady.value = overview?.sizes_ready ?? true
  } catch (e) {
    // Distinguish "request failed" from "the registry is empty": the empty
    // table used to read as "no packages" when the real problem was a timeout.
    loadError.value = apiError(e) || t('packages.loadFailed')
    ElMessage.error(loadError.value)
  } finally {
    loading.value = false
  }
}

/**
 * Index page and JSON metadata are backend-owned, machine-facing URLs.
 * They are opened directly rather than fetched — `pip` reads exactly the
 * same HTML, so there is only one implementation to keep correct.
 */
function indexUrl(name: string): string {
  return appUrl(`/simple/${encodeURIComponent(name)}/`)
}

function jsonUrl(name: string): string {
  return appUrl(`/simple/${encodeURIComponent(name)}/?format=json`)
}

onMounted(() => {
  if (infoType.value === 'package') load()
})
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ heading.title }}</h1>
        <p class="page__description">{{ heading.description }}</p>
      </div>
      <div class="toolbar">
        <el-select v-model="infoType" class="toolbar__type">
          <el-option value="package" :label="t('packages.infoPackages')" />
          <el-option value="build" :label="t('packages.infoBuilds')" />
        </el-select>
        <template v-if="infoType === 'package'">
          <el-input
            v-model="query"
            class="toolbar__search"
            clearable
            :placeholder="t('packages.filterPlaceholder')"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :loading="loading" @click="load">
            <el-icon><Refresh /></el-icon>
            <span class="btn-label">{{ t('common.refresh') }}</span>
          </el-button>
        </template>
        <el-link v-else :href="appUrl('/python-builds/')" target="_blank" rel="noopener">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('build.staticIndex') }}</span>
        </el-link>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      class="page__alert"
      type="error"
      :closable="false"
      show-icon
      :title="loadError"
    >
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>
    <el-alert
      v-else-if="infoType === 'package' && !sizesReady"
      class="page__alert"
      type="info"
      :closable="false"
      show-icon
      :title="t('packages.sizesWarming')"
    />

    <BuildCatalogView v-if="infoType === 'build'" kind="python" />

    <el-card v-else shadow="never">
      <el-table
        v-loading="loading"
        :data="rows"
        stripe
        :empty-text="query ? t('packages.emptyFiltered', { query }) : t('packages.empty')"
        @sort-change="onSortChange"
      >
        <el-table-column prop="name" :label="t('packages.name')" min-width="220" sortable="custom">
          <template #default="{ row }">
            <a class="pkg-link mono" :href="indexUrl(row.name)" target="_blank" rel="noopener">
              {{ row.name }}
            </a>
          </template>
        </el-table-column>

        <el-table-column
          prop="file_count"
          :label="t('packages.files')"
          width="100"
          align="right"
          sortable="custom"
        />

        <el-table-column
          prop="total_size"
          :label="t('packages.size')"
          width="130"
          align="right"
          sortable="custom"
        >
          <template #default="{ row }">
            {{ row.total_size_human || formatBytes(row.total_size) }}
          </template>
        </el-table-column>

        <el-table-column
          prop="download_count"
          :label="t('packages.downloads')"
          width="120"
          align="right"
          sortable="custom"
        />

        <el-table-column
          prop="upload_count"
          :label="t('packages.uploads')"
          width="110"
          align="right"
          sortable="custom"
        />

        <el-table-column :label="t('common.actions')" width="190" align="right">
          <template #default="{ row }">
            <el-link type="primary" :href="indexUrl(row.name)" target="_blank" rel="noopener">
              <el-icon><Link /></el-icon>
              <span class="btn-label">{{ t('packages.openIndex') }}</span>
            </el-link>
            <el-tooltip :content="t('packages.openJson')">
              <el-link :href="jsonUrl(row.name)" target="_blank" rel="noopener">
                <el-icon><Document /></el-icon>
              </el-link>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="page"
        v-model:page-size="pageSize"
        :page-sizes="pageSizes"
        :total="total"
      />
    </el-card>
  </div>
</template>

<style scoped>
.page__alert {
  margin-bottom: 12px;
}

.toolbar__type {
  width: 160px;
}

.toolbar__search {
  width: 260px;
}

.pkg-link {
  color: var(--el-color-primary);
  text-decoration: none;
}

.pkg-link:hover {
  text-decoration: underline;
}
</style>
