<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchNpmCatalog, type NpmCatalog, type NpmPackage } from '@/api'
import { apiError } from '@/api/client'
import BuildCatalogView from '@/components/BuildCatalogView.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { formatDate } from '@/utils/format'

const { t } = useI18n()

/**
 * One Node page, two sub-elements: the npm package catalog and the prebuilt
 * Node.js mirror.  The dropdown picks which one the page is showing — the
 * same pattern the Python page uses for packages vs CPython builds.
 */
const infoType = ref<'package' | 'build'>('package')

const catalog = ref<NpmCatalog | null>(null)
const loading = ref(true)

const packages = computed<NpmPackage[]>(() => catalog.value?.packages ?? [])
const { page, pageSize, pageSizes, total, rows } = usePagination(packages)

const heading = computed(() =>
  infoType.value === 'package'
    ? { title: t('npm.title'), description: t('npm.description') }
    : { title: t('build.nodeTitle'), description: t('build.nodeDescription') },
)

async function load(): Promise<void> {
  loading.value = true
  try {
    catalog.value = await fetchNpmCatalog()
  } catch (e) {
    ElMessage.error(apiError(e) || t('npm.loadFailed'))
  } finally {
    loading.value = false
  }
}

function download(item: NpmPackage): void {
  if (!item.download_url) return
  window.open(item.download_url, '_blank', 'noopener')
}

/** The server-rendered index — same data, plus the legacy `/-/all` JSON. */
function openStaticIndex(): void {
  window.open('/npm/', '_blank', 'noopener')
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
          <el-option value="package" :label="t('npm.infoPackages')" />
          <el-option value="build" :label="t('npm.infoBuilds')" />
        </el-select>
        <template v-if="infoType === 'package'">
          <el-button @click="openStaticIndex">
            <el-icon><Link /></el-icon>
            <span class="btn-label">{{ t('npm.staticIndex') }}</span>
          </el-button>
          <el-button :loading="loading" @click="load">
            <el-icon><Refresh /></el-icon>
            <span class="btn-label">{{ t('common.refresh') }}</span>
          </el-button>
        </template>
        <el-button v-else @click="openTab('/node-builds/')">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t('build.staticIndex') }}</span>
        </el-button>
      </div>
    </div>

    <BuildCatalogView v-if="infoType === 'build'" kind="node" />

    <template v-else>
      <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('npm.listTitle') }}</span>
      </template>

      <el-empty
        v-if="!loading && (catalog?.package_count ?? 0) === 0"
        :description="catalog && !catalog.exists ? t('npm.missingDesc', { root: catalog.root }) : t('npm.empty')"
      />

      <el-table v-else v-loading="loading" :data="rows" stripe>
        <el-table-column prop="name" :label="t('npm.name')" min-width="220">
          <template #default="{ row }">
            <div class="pkg">
              <span class="pkg__name mono">{{ row.name }}</span>
              <span v-if="row.description" class="pkg__desc">{{ row.description }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="version" :label="t('npm.version')" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.version }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('npm.size')" width="110" align="right">
          <template #default="{ row }">{{ row.size_human || '—' }}</template>
        </el-table-column>

        <el-table-column :label="t('npm.modified')" width="160">
          <template #default="{ row }">{{ formatDate(row.modified) }}</template>
        </el-table-column>

        <el-table-column :label="t('npm.status')" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.download_url" size="small" type="success" effect="plain">
              {{ t('npm.servable') }}
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">
              {{ t('npm.metadataOnly') }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('common.actions')" width="130" align="right">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              :disabled="!row.download_url"
              @click="download(row)"
            >
              <el-icon><Download /></el-icon>
              <span class="btn-label">{{ t('npm.download') }}</span>
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
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.toolbar__type {
  width: 160px;
}

.pkg {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pkg__name {
  font-weight: 500;
}

.pkg__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
