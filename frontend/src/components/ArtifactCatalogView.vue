<script setup lang="ts">
/**
 * Shared view for the *flat* artifact catalogs — docker images and .deb
 * packages.  Both list one flat directory of files with a `catalog.json`
 * overlay, so only the labels, columns and setup snippet differ; the parent
 * views pick the endpoint and the i18n namespace.
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  fetchDebianCatalog,
  fetchDockerCatalog,
  type DebianCatalog,
  type DockerCatalog,
  type FlatArtifact,
} from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { formatDate } from '@/utils/format'

const props = defineProps<{ endpoint: 'docker' | 'debian' }>()

const { t } = useI18n()

type Catalog = DockerCatalog | DebianCatalog

const catalog = ref<Catalog | null>(null)
const loading = ref(true)

const isDocker = computed(() => props.endpoint === 'docker')
const artifacts = computed<FlatArtifact[]>(() => catalog.value?.artifacts ?? [])
const { page, pageSize, pageSizes, total, rows } = usePagination(artifacts)
const upstream = computed(() =>
  isDocker.value
    ? ((catalog.value as DockerCatalog | null)?.registry ?? '')
    : ((catalog.value as DebianCatalog | null)?.mirror ?? ''),
)

/** The server-rendered index page — uncacheable and script-friendly. */
const indexUrl = computed(() => `/${props.endpoint}/`)
const baseUrl = computed(() => `${window.location.origin}${indexUrl.value}`)

const usage = computed(() =>
  isDocker.value
    ? [
        '# 导入离线镜像',
        'docker load -i <镜像文件>.tar',
        '',
        '# 仓库清单（Registry v2 形状）',
        `curl ${baseUrl.value}v2/_catalog`,
      ].join('\n')
    : [
        '# 安装单个包',
        'sudo apt install ./<包名>.deb',
        '',
        '# 或作为扁平源： /etc/apt/sources.list.d/openfish.list',
        `deb [trusted=yes] ${baseUrl.value} ./`,
      ].join('\n'),
)

const indexEndpoint = computed(() =>
  isDocker.value ? `${baseUrl.value}v2/_catalog` : `${baseUrl.value}Packages`,
)

async function load(): Promise<void> {
  loading.value = true
  try {
    catalog.value = isDocker.value ? await fetchDockerCatalog() : await fetchDebianCatalog()
  } catch (e) {
    ElMessage.error(apiError(e) || t(`${props.endpoint}.loadFailed`))
  } finally {
    loading.value = false
  }
}

function download(item: FlatArtifact): void {
  if (!item.download_url) return
  window.open(item.download_url, '_blank', 'noopener')
}

function openStaticIndex(): void {
  window.open(indexUrl.value, '_blank', 'noopener')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t(`${endpoint}.title`) }}</h1>
        <p class="page__description">{{ t(`${endpoint}.description`) }}</p>
      </div>
      <div class="toolbar">
        <el-button @click="openStaticIndex">
          <el-icon><Link /></el-icon>
          <span class="btn-label">{{ t(`${endpoint}.staticIndex`) }}</span>
        </el-button>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      type="info"
      show-icon
      :closable="false"
      :title="t(`${endpoint}.proxyTitle`)"
      :description="t(`${endpoint}.proxyDesc`)"
    />

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t(`${endpoint}.setupTitle`) }}</span>
      </template>
      <div class="setup">
        <div v-if="upstream" class="setup__item">
          <div class="setup__label">{{ t(`${endpoint}.upstreamLabel`) }}</div>
          <el-tag class="mono" type="info" effect="plain">{{ upstream }}</el-tag>
        </div>
        <div class="setup__item">
          <div class="setup__label">{{ t(`${endpoint}.indexLabel`) }}</div>
          <el-tag class="mono" type="primary" effect="plain">{{ indexEndpoint }}</el-tag>
        </div>
        <CodeBlock :label="t(`${endpoint}.usageLabel`)" :code="usage" />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t(`${endpoint}.listTitle`) }}</span>
      </template>

      <el-empty
        v-if="!loading && artifacts.length === 0"
        :description="
          catalog && !catalog.exists
            ? t(`${endpoint}.missingDesc`, { root: catalog.root })
            : t(`${endpoint}.empty`)
        "
      />

      <el-table v-else v-loading="loading" :data="rows" stripe>
        <el-table-column :label="t(`${endpoint}.name`)" min-width="220">
          <template #default="{ row }">
            <div class="item">
              <span class="item__name mono">{{ row.name }}</span>
              <span v-if="row.description" class="item__desc">{{ row.description }}</span>
              <span class="item__tags">
                <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain">
                  {{ tag }}
                </el-tag>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="version" :label="t(`${endpoint}.version`)" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.version" size="small" effect="plain">{{ row.version }}</el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>

        <el-table-column
          v-if="endpoint === 'debian'"
          prop="arch"
          :label="t(`${endpoint}.arch`)"
          width="110"
        >
          <template #default="{ row }">{{ row.arch || '—' }}</template>
        </el-table-column>

        <el-table-column :label="t(`${endpoint}.kind`)" width="130">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">{{ row.kind }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t(`${endpoint}.size`)" width="110" align="right">
          <template #default="{ row }">{{ row.size_human || '—' }}</template>
        </el-table-column>

        <el-table-column :label="t(`${endpoint}.modified`)" width="150">
          <template #default="{ row }">{{ formatDate(row.modified) }}</template>
        </el-table-column>

        <el-table-column :label="t(`${endpoint}.status`)" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.download_url" size="small" type="success" effect="plain">
              {{ t(`${endpoint}.servable`) }}
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">
              {{ t(`${endpoint}.metadataOnly`) }}
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
              <span class="btn-label">{{ t(`${endpoint}.download`) }}</span>
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
  </div>
</template>

<style scoped>
.item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.item__name {
  font-weight: 500;
}

.item__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.item__tags {
  display: flex;
  gap: 4px;
  margin-top: 2px;
}
</style>
