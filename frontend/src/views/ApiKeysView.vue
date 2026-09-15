<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  createKey,
  deleteKey,
  fetchKeyStats,
  fetchKeys,
  type ApiKey,
  type CreatedApiKey,
  type KeyStatRow,
  type KeyStats,
} from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { useSessionStore } from '@/stores/session'
import { formatDate } from '@/utils/format'

const { t } = useI18n()
const session = useSessionStore()

const keys = ref<ApiKey[]>([])
const loading = ref(true)

const {
  page,
  pageSize,
  pageSizes,
  total,
  rows,
  onSortChange,
} = usePagination(keys)

// `el-select` needs a non-null value to keep the selection rendered, so the
// "permanent" choice travels as a sentinel string and is mapped back to `null`
// (the API's representation of "never expires") on submit.
type ExpiryChoice = '7' | '30' | '90' | '180' | '365' | 'permanent'

const form = reactive<{ name: string; expires: ExpiryChoice }>({
  name: '',
  expires: '90',
})
const creating = ref(false)

const newKey = ref<CreatedApiKey | null>(null)
const newKeyVisible = ref(false)

const statsVisible = ref(false)
const statsLoading = ref(false)
const stats = ref<KeyStats | null>(null)
const statsKeyName = ref('')

const statsRows = computed<KeyStatRow[]>(() => stats.value?.per_package ?? [])
const {
  page: statsPage,
  pageSize: statsPageSize,
  pageSizes: statsPageSizes,
  total: statsTotal,
  rows: statsRowsPage,
  reset: resetStatsPage,
} = usePagination(statsRows, { pageSize: 10, pageSizes: [10, 20, 50] })

const canCreate = computed(() => session.can('key:create'))
const canDelete = computed(() => session.can('key:delete'))

const expiryOptions = computed<Array<{ value: ExpiryChoice; label: string }>>(() => [
  { value: '90', label: t('keys.expiry.d90') },
  { value: '30', label: t('keys.expiry.d30') },
  { value: '180', label: t('keys.expiry.d180') },
  { value: '365', label: t('keys.expiry.d365') },
  { value: '7', label: t('keys.expiry.d7') },
  { value: 'permanent', label: t('keys.expiry.permanent') },
])

const newKeySnippets = computed(() => {
  if (!newKey.value) return { netrc: '', twine: '' }
  const base = window.location.origin
  const host = window.location.host
  return {
    netrc: `machine ${host}\n  login __token__\n  password ${newKey.value.key}`,
    twine:
      `twine upload --repository-url ${base}/ \\\n` +
      `  --username __token__ --password ${newKey.value.key} dist/*`,
  }
})

function statusOf(key: ApiKey): { type: 'success' | 'danger' | 'info'; label: string } {
  if (key.is_expired) return { type: 'danger', label: t('keys.status.expired') }
  if (key.is_permanent) return { type: 'info', label: t('keys.status.permanent') }
  return { type: 'success', label: t('keys.status.active') }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    keys.value = await fetchKeys()
  } catch (e) {
    ElMessage.error(apiError(e) || t('keys.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning(t('keys.nameRequired'))
    return
  }
  creating.value = true
  try {
    newKey.value = await createKey({
      name,
      expires_in_days: form.expires === 'permanent' ? null : Number(form.expires),
    })
    newKeyVisible.value = true
    form.name = ''
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('keys.createFailed'))
  } finally {
    creating.value = false
  }
}

async function confirmDelete(key: ApiKey): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('keys.deleteConfirm', { name: key.name }),
      t('keys.deleteTitle'),
      { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') },
    )
  } catch {
    return // user cancelled
  }
  try {
    await deleteKey(key.id)
    ElMessage.success(t('keys.deleted'))
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('keys.deleteFailed'))
  }
}

async function openStats(key: ApiKey): Promise<void> {
  statsKeyName.value = key.name
  stats.value = key.stats_detail ?? null
  statsVisible.value = true
  resetStatsPage()
  if (stats.value) return
  statsLoading.value = true
  try {
    stats.value = await fetchKeyStats(key.id)
  } catch (e) {
    ElMessage.error(apiError(e) || t('keys.statsFailed'))
  } finally {
    statsLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('keys.title') }}</h1>
        <p class="page__description">{{ t('keys.description') }}</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        <span class="btn-label">{{ t('common.refresh') }}</span>
      </el-button>
    </div>

    <el-card v-if="canCreate" shadow="never">
      <template #header><span class="card-title">{{ t('keys.createTitle') }}</span></template>
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <div class="create-row">
          <el-form-item class="create-row__name" :label="t('keys.nameLabel')" required>
            <el-input
              v-model="form.name"
              :placeholder="t('keys.namePlaceholder')"
              maxlength="128"
              clearable
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-form-item class="create-row__expiry" :label="t('keys.expiryLabel')">
            <el-select v-model="form.expires" class="create-row__select">
              <el-option
                v-for="opt in expiryOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item class="create-row__submit">
            <el-button type="primary" :loading="creating" @click="submit">
              <el-icon><Plus /></el-icon>
              <span class="btn-label">{{ creating ? t('keys.generating') : t('keys.generate') }}</span>
            </el-button>
          </el-form-item>
        </div>
        <p class="hint">{{ t('keys.expiryHint') }}</p>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table
        v-loading="loading"
        :data="rows"
        stripe
        :empty-text="t('keys.empty')"
        @sort-change="onSortChange"
      >
        <el-table-column prop="name" :label="t('keys.table.name')" min-width="180" sortable="custom" />
        <el-table-column prop="prefix" :label="t('keys.table.prefix')" min-width="150">
          <template #default="{ row }"><span class="mono">{{ row.prefix }}</span></template>
        </el-table-column>
        <el-table-column :label="t('keys.table.status')" width="110">
          <template #default="{ row }">
            <el-tag :type="statusOf(row).type" size="small" effect="plain">
              {{ statusOf(row).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="download_count"
          :label="t('keys.table.downloads')"
          width="110"
          align="right"
          sortable="custom"
        />
        <el-table-column
          prop="upload_count"
          :label="t('keys.table.uploads')"
          width="100"
          align="right"
          sortable="custom"
        />
        <el-table-column :label="t('keys.table.created')" width="150">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('keys.table.expires')" width="150">
          <template #default="{ row }">
            {{ row.is_permanent ? t('common.never') : formatDate(row.expires_at) }}
          </template>
        </el-table-column>
        <el-table-column :label="t('keys.table.lastUsed')" width="150">
          <template #default="{ row }">{{ formatDate(row.last_used) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="150" align="right" fixed="right">
          <template #default="{ row }">
            <el-tooltip :content="t('keys.statsTitle')">
              <el-button size="small" text @click="openStats(row)">
                <el-icon><TrendCharts /></el-icon>
              </el-button>
            </el-tooltip>
            <el-tooltip v-if="canDelete" :content="t('common.delete')">
              <el-button size="small" text type="danger" @click="confirmDelete(row)">
                <el-icon><Delete /></el-icon>
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

    <!-- Raw key is revealed exactly once. -->
    <el-dialog
      v-model="newKeyVisible"
      :title="t('keys.newKeyTitle')"
      width="620px"
      :close-on-click-modal="false"
    >
      <el-alert type="warning" :closable="false" show-icon class="dialog-alert">
        {{ t('keys.newKeyWarning') }}
      </el-alert>
      <CodeBlock :code="newKey?.key ?? ''" />
      <el-divider />
      <CodeBlock :label="t('home.netrcTitle')" :code="newKeySnippets.netrc" />
      <div class="dialog-gap" />
      <CodeBlock :label="t('home.twineTitle')" :code="newKeySnippets.twine" />
      <template #footer>
        <el-button type="primary" @click="newKeyVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="statsVisible" :title="`${t('keys.statsTitle')} — ${statsKeyName}`" size="560px">
      <div v-loading="statsLoading" class="stats">
        <div class="stats__totals">
          <el-statistic :title="t('stat.downloads')" :value="stats?.total_downloads ?? 0" />
          <el-statistic :title="t('stat.uploads')" :value="stats?.total_uploads ?? 0" />
        </div>
        <el-divider>{{ t('keys.perPackage') }}</el-divider>
        <el-table
          :data="statsRowsPage"
          size="small"
          :empty-text="t('keys.statsEmpty')"
        >
          <el-table-column prop="package_name" :label="t('packages.name')" min-width="180" />
          <el-table-column :label="t('common.detail')" width="110">
            <template #default="{ row }">
              {{ row.event_type === 'download' ? t('keys.eventDownload') : t('keys.eventUpload') }}
            </template>
          </el-table-column>
          <el-table-column prop="count" :label="t('stat.downloads')" width="90" align="right" />
        </el-table>

        <TablePager
          v-model:page="statsPage"
          v-model:page-size="statsPageSize"
          :page-sizes="statsPageSizes"
          :total="statsTotal"
          compact
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.create-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
}

.create-row__name {
  flex: 1 1 260px;
  margin-bottom: 0;
}

.create-row__expiry {
  flex: 0 1 220px;
  margin-bottom: 0;
}

.create-row__select {
  width: 100%;
}

.create-row__submit {
  margin-bottom: 0;
}

.hint {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.dialog-alert {
  margin-bottom: 14px;
}

.dialog-gap {
  height: 12px;
}

.stats__totals {
  display: flex;
  gap: 40px;
}
</style>
