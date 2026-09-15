<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchPackages, type PackageSummary } from '@/api'
import { apiError } from '@/api/client'
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
  try {
    packages.value = await fetchPackages()
  } catch (e) {
    ElMessage.error(apiError(e) || t('packages.loadFailed'))
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
  return `/simple/${encodeURIComponent(name)}/`
}

function jsonUrl(name: string): string {
  return `/simple/${encodeURIComponent(name)}/?format=json`
}

function openTab(url: string): void {
  window.open(url, '_blank', 'noopener')
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
        <el-button v-else @click="openTab('/python-builds/')">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('build.staticIndex') }}</span>
        </el-button>
      </div>
    </div>

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
            <el-button size="small" text type="primary" @click="openTab(indexUrl(row.name))">
              <el-icon><Link /></el-icon>
              <span class="btn-label">{{ t('packages.openIndex') }}</span>
            </el-button>
            <el-tooltip :content="t('packages.openJson')">
              <el-button size="small" text @click="openTab(jsonUrl(row.name))">
                <el-icon><Document /></el-icon>
              </el-button>
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
