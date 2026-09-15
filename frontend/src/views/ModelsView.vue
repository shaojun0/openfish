<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchModelRoutes, type ModelRoute, type ModelRoutes } from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'

const { t } = useI18n()

const routes = ref<ModelRoutes | null>(null)
const loading = ref(true)

const enabledCount = computed(
  () => (routes.value?.routes ?? []).filter((route) => route.enabled).length,
)

const routeList = computed<ModelRoute[]>(() => routes.value?.routes ?? [])
const { page, pageSize, pageSizes, total, rows } = usePagination(routeList)

/** A ready-to-paste mapping for the DSH side, built from enabled routes. */
const dshSnippet = computed(() => {
  const models: Record<string, { base_url: string; model: string; path: string }> = {}
  for (const route of routes.value?.routes ?? []) {
    if (!route.enabled) continue
    for (const alias of route.aliases.length ? route.aliases : [route.name]) {
      models[alias] = {
        base_url: route.base_url,
        model: route.model || route.name,
        path: route.path,
      }
    }
  }
  return JSON.stringify({ models }, null, 2)
})

async function load(): Promise<void> {
  loading.value = true
  try {
    routes.value = await fetchModelRoutes()
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.loadFailed'))
  } finally {
    loading.value = false
  }
}

function routeSummary(route: ModelRoute): string {
  const url = route.base_url.replace(/\/$/, '')
  return `${url}${route.path.startsWith('/') ? route.path : '/' + route.path}`
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('models.title') }}</h1>
        <p class="page__description">{{ t('models.description') }}</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        <span class="btn-label">{{ t('common.refresh') }}</span>
      </el-button>
    </div>

    <el-alert
      type="info"
      show-icon
      :closable="false"
      :title="t('models.scaffoldTitle')"
      :description="t('models.scaffoldDesc')"
    />

    <div class="stat-row">
      <el-tag type="success" effect="plain">
        {{ t('models.enabled', { count: enabledCount }) }}
      </el-tag>
      <el-tag type="info" effect="plain">
        {{ t('models.total', { count: routes?.routes.length ?? 0 }) }}
      </el-tag>
      <el-tag v-if="routes?.source" class="mono" type="warning" effect="plain">
        {{ routes.source }}
      </el-tag>
    </div>

    <el-card shadow="never">
      <el-alert
        v-if="routes?.error"
        type="error"
        show-icon
        :closable="false"
        :title="t('models.parseError')"
        :description="routes.error"
      />

      <el-empty
        v-else-if="!loading && (routes?.routes.length ?? 0) === 0"
        :description="routes && !routes.exists ? t('models.missingDesc', { source: routes.source }) : t('models.empty')"
      />

      <el-table v-else v-loading="loading" :data="rows" stripe>
        <el-table-column :label="t('models.name')" min-width="200">
          <template #default="{ row }">
            <div class="route">
              <span class="route__name">{{ row.name }}</span>
              <span v-if="row.description" class="route__desc">{{ row.description }}</span>
              <span class="route__aliases">
                <el-tag
                  v-for="alias in row.aliases"
                  :key="alias"
                  size="small"
                  effect="plain"
                >
                  {{ alias }}
                </el-tag>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="provider" :label="t('models.provider')" width="160">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">{{ row.provider }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.endpoint')" min-width="280">
          <template #default="{ row }">
            <span class="mono">{{ routeSummary(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="model" :label="t('models.model')" width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.model || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.status')" width="110" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.enabled" size="small" type="success" effect="plain">
              {{ t('models.on') }}
            </el-tag>
            <el-tag v-else size="small" type="danger" effect="plain">
              {{ t('models.off') }}
            </el-tag>
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

    <el-card v-if="enabledCount" shadow="never">
      <template #header>
        <span class="card-title">{{ t('models.snippetTitle') }}</span>
      </template>
      <p class="hint">{{ t('models.snippetDesc') }}</p>
      <CodeBlock :code="dshSnippet" />
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

.hint {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.stat-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.route {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.route__name {
  font-weight: 500;
}

.route__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.route__aliases {
  display: flex;
  gap: 4px;
  margin-top: 2px;
}
</style>
