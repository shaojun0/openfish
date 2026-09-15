<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  checkModelRoute,
  createModelRoute,
  deleteModelRoute,
  fetchModelRoutes,
  probeModelRoute,
  updateModelRoute,
  type ModelRoute,
  type ModelRouteHealth,
  type ModelRoutePayload,
  type ModelRoutes,
} from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { useSessionStore } from '@/stores/session'

/**
 * Model routing (`/models`).
 *
 * The route table is the one hub catalog an administrator edits in the
 * browser.  Adding a route opens an inline editor **as the first row of the
 * table**; editing reuses the same row.  Saving validates the entry server
 * side, rewrites `MODELS_FILE` atomically and probes the URL, and the
 * connectivity column shows whether the endpoint answers.  Everyone with
 * `model:read` may look; changing anything needs `model:write` (admin).
 */
const { t } = useI18n()
const session = useSessionStore()

const routes = ref<ModelRoutes | null>(null)
const loading = ref(true)

const canWrite = computed(() => session.can('model:write'))
const allRoutes = computed<ModelRoute[]>(() => routes.value?.routes ?? [])
const enabledCount = computed(() => allRoutes.value.filter((route) => route.enabled).length)

const providerOptions = computed<string[]>(() => {
  const fromServer = routes.value?.providers ?? []
  return fromServer.length ? fromServer : ['openai', 'mineru', 'anthropic']
})

// ── The inline editor row ────────────────────────────────────────────

/** Marker object that makes the first table row render as a form. */
interface EditorRow {
  __editor: true
}
type TableRow = ModelRoute | EditorRow

interface EditorForm {
  name: string
  provider: string
  base_url: string
  path: string
  model: string
  aliases: string
  api_key: string
  clearApiKey: boolean
  description: string
  enabled: boolean
}

const editorMode = ref<'create' | 'edit' | null>(null)
const editorOriginalName = ref<string | null>(null)
const editorHealth = ref<ModelRouteHealth | null>(null)
const apiKeyTouched = ref(false)
const saving = ref(false)
const probing = ref(false)
const checkingAll = ref(false)
const checkingName = ref<string | null>(null)

const editor = reactive<EditorForm>({
  name: '',
  provider: 'openai',
  base_url: '',
  path: '',
  model: '',
  aliases: '',
  api_key: '',
  clearApiKey: false,
  description: '',
  enabled: true,
})

function isEditor(row: TableRow): row is EditorRow {
  return (row as EditorRow).__editor === true
}

/** The create/edit row is always pinned to the top of the table. */
const tableData = computed<TableRow[]>(() => {
  const base =
    editorMode.value === 'edit' && editorOriginalName.value
      ? allRoutes.value.filter((route) => route.name !== editorOriginalName.value)
      : allRoutes.value
  return editorMode.value ? [{ __editor: true } as EditorRow, ...base] : base
})

const { page, pageSize, pageSizes, total, rows } = usePagination(tableData)

const apiKeyPlaceholder = computed(() =>
  editorMode.value === 'edit' ? t('models.apiKeyKeep') : t('models.apiKeyOptional'),
)

const pathPlaceholder = computed(
  () => routes.value?.default_paths?.[editor.provider] ?? t('models.pathPlaceholder'),
)

/** A ready-to-paste mapping for the DSH side, built from enabled routes. */
const dshSnippet = computed(() => {
  const models: Record<string, { base_url: string; model: string; path: string }> = {}
  for (const route of allRoutes.value) {
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

// ── Loading ──────────────────────────────────────────────────────────

async function load(showSpinner = true): Promise<void> {
  if (showSpinner) loading.value = true
  try {
    routes.value = await fetchModelRoutes()
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.loadFailed'))
  } finally {
    loading.value = false
  }
}

// ── Display helpers ──────────────────────────────────────────────────

function endpointOf(route: ModelRoute): string {
  const url = route.base_url.replace(/\/$/, '')
  if (!route.path) return url
  return `${url}${route.path.startsWith('/') ? route.path : '/' + route.path}`
}

function providerLabel(provider: string): string {
  switch (provider) {
    case 'openai':
      return t('models.providerOpenai')
    case 'mineru':
      return t('models.providerMineru')
    case 'anthropic':
      return t('models.providerAnthropic')
    default:
      return provider
  }
}

function healthType(health: ModelRouteHealth | null): 'success' | 'warning' | 'danger' | 'info' {
  if (!health) return 'info'
  switch (health.status) {
    case 'ok':
      return 'success'
    case 'auth':
    case 'method':
    case 'not_found':
    case 'client_error':
      return 'warning'
    default:
      return 'danger'
  }
}

function healthLabel(health: ModelRouteHealth | null): string {
  if (!health) return t('models.healthUnknown')
  let base: string
  switch (health.status) {
    case 'ok':
      base = t('models.healthOk')
      break
    case 'auth':
      base = t('models.healthAuth')
      break
    case 'method':
      base = t('models.healthMethod')
      break
    case 'not_found':
      base = t('models.healthNotFound')
      break
    case 'client_error':
      base = t('models.healthClientError')
      break
    case 'server_error':
      base = t('models.healthServerError')
      break
    default:
      base = t('models.healthUnreachable')
  }
  const detail = [
    health.http_status ? `HTTP ${health.http_status}` : '',
    health.latency_ms != null ? `${health.latency_ms} ms` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return detail ? `${base} · ${detail}` : base
}

function healthTooltip(health: ModelRouteHealth | null): string {
  if (!health) return ''
  return [
    health.error,
    health.url,
    health.checked_at ? `${t('models.checkedAt')} ${health.checked_at}` : '',
  ]
    .filter(Boolean)
    .join('\n')
}

function rowClassName({ row }: { row: TableRow }): string {
  return isEditor(row) ? 'models-view__editor-row' : ''
}

// ── Create / edit ────────────────────────────────────────────────────

function focusEditor(): void {
  requestAnimationFrame(() => {
    const input = document.querySelector<HTMLInputElement>('.models-view__editor-row input')
    input?.focus()
  })
}

function resetEditor(): void {
  editor.name = ''
  editor.provider = providerOptions.value[0] ?? 'openai'
  editor.base_url = ''
  editor.path = ''
  editor.model = ''
  editor.aliases = ''
  editor.api_key = ''
  editor.clearApiKey = false
  editor.description = ''
  editor.enabled = true
  apiKeyTouched.value = false
  editorHealth.value = null
}

function startCreate(): void {
  resetEditor()
  editorMode.value = 'create'
  editorOriginalName.value = null
  page.value = 1
  focusEditor()
}

function startEdit(route: ModelRoute): void {
  resetEditor()
  editor.name = route.name
  editor.provider = route.provider
  editor.base_url = route.base_url
  editor.path = route.path
  editor.model = route.model
  editor.aliases = route.aliases.join(', ')
  editor.description = route.description ?? ''
  editor.enabled = route.enabled
  editorHealth.value = route.health
  editorMode.value = 'edit'
  editorOriginalName.value = route.name
  page.value = 1
  focusEditor()
}

function cancelEdit(): void {
  editorMode.value = null
  editorOriginalName.value = null
  editorHealth.value = null
  apiKeyTouched.value = false
  saving.value = false
}

function validate(): string | null {
  if (!editor.name.trim()) return t('models.errName')
  if (!editor.description.trim()) return t('models.errDescription')
  if (!editor.base_url.trim()) return t('models.errUrl')
  if (!/^https?:\/\/\S+$/i.test(editor.base_url.trim())) return t('models.errUrlScheme')
  return null
}

function parseAliases(raw: string): string[] {
  const out: string[] = []
  for (const part of raw.split(',')) {
    const text = part.trim()
    if (text && !out.includes(text)) out.push(text)
  }
  return out
}

function buildPayload(): ModelRoutePayload {
  const payload: ModelRoutePayload = {
    name: editor.name.trim(),
    provider: editor.provider,
    base_url: editor.base_url.trim(),
    description: editor.description.trim(),
    model: editor.model.trim(),
    aliases: parseAliases(editor.aliases),
    path: editor.path.trim(),
    enabled: editor.enabled,
  }
  if (editorMode.value === 'create') {
    payload.api_key = editor.api_key.trim()
  } else if (editor.clearApiKey) {
    payload.api_key = ''
  } else if (apiKeyTouched.value) {
    // Touched but emptied counts as "clear"; untouched keeps the stored key.
    payload.api_key = editor.api_key.trim()
  }
  return payload
}

async function save(): Promise<void> {
  const problem = validate()
  if (problem) {
    ElMessage.warning(problem)
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    const result =
      editorMode.value === 'edit' && editorOriginalName.value
        ? await updateModelRoute(editorOriginalName.value, payload)
        : await createModelRoute(payload)
    editorHealth.value = result.health
    ElMessage.success(
      t(editorMode.value === 'edit' ? 'models.updated' : 'models.created', {
        name: result.route.name,
      }),
    )
    cancelEdit()
    await load(false)
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function remove(route: ModelRoute): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('models.deleteConfirm', { name: route.name }),
      t('models.deleteTitle'),
      {
        type: 'warning',
        confirmButtonText: t('common.delete'),
        cancelButtonText: t('common.cancel'),
      },
    )
  } catch {
    return // the user cancelled
  }
  try {
    await deleteModelRoute(route.name)
    ElMessage.success(t('models.deleted', { name: route.name }))
    if (editorMode.value === 'edit' && editorOriginalName.value === route.name) cancelEdit()
    await load(false)
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.deleteFailed'))
  }
}

// ── Connectivity probes ──────────────────────────────────────────────

async function probeDraft(): Promise<void> {
  if (!editor.base_url.trim()) {
    ElMessage.warning(t('models.errUrl'))
    return
  }
  probing.value = true
  try {
    editorHealth.value = await probeModelRoute({
      provider: editor.provider,
      base_url: editor.base_url.trim(),
      path: editor.path.trim(),
      api_key: editor.api_key.trim() || undefined,
    })
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.probeFailed'))
  } finally {
    probing.value = false
  }
}

async function checkOne(route: ModelRoute): Promise<void> {
  checkingName.value = route.name
  try {
    route.health = await checkModelRoute(route.name)
  } catch (e) {
    ElMessage.error(apiError(e) || t('models.probeFailed'))
  } finally {
    checkingName.value = null
  }
}

async function checkAll(): Promise<void> {
  const list = allRoutes.value
  if (!list.length) return
  checkingAll.value = true
  try {
    const results = await Promise.allSettled(list.map((route) => checkModelRoute(route.name)))
    let failed = 0
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') list[index].health = result.value
      else failed += 1
    })
    if (failed) ElMessage.warning(t('models.probeAllPartial', { failed }))
    else ElMessage.success(t('models.probeAllDone'))
  } finally {
    checkingAll.value = false
  }
}

onMounted(() => load())
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('models.title') }}</h1>
        <p class="page__description">{{ t('models.description') }}</p>
      </div>
      <div class="page__actions">
        <el-button
          v-if="canWrite"
          :loading="checkingAll"
          :disabled="!allRoutes.length || editorMode !== null"
          @click="checkAll"
        >
          <el-icon><Connection /></el-icon>
          <span class="btn-label">{{ t('models.probeAll') }}</span>
        </el-button>
        <el-button
          v-if="canWrite"
          type="primary"
          :disabled="editorMode !== null"
          @click="startCreate"
        >
          <el-icon><Plus /></el-icon>
          <span class="btn-label">{{ t('models.add') }}</span>
        </el-button>
        <el-button :loading="loading" @click="load()">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
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
        {{ t('models.total', { count: allRoutes.length }) }}
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
        v-else-if="!loading && allRoutes.length === 0 && !editorMode"
        :description="
          routes && !routes.exists
            ? t('models.missingDesc', { source: routes.source })
            : t('models.empty')
        "
      />

      <el-table
        v-else
        v-loading="loading"
        :data="rows"
        :row-class-name="rowClassName"
        stripe
      >
        <el-table-column :label="t('models.name')" min-width="220">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="editor-cell">
              <el-input
                v-model="editor.name"
                size="small"
                :placeholder="t('models.namePlaceholder')"
              />
              <el-input
                v-model="editor.description"
                size="small"
                :placeholder="t('models.descriptionPlaceholder')"
              />
            </div>
            <div v-else class="route">
              <span class="route__name">{{ row.name }}</span>
              <span v-if="row.description" class="route__desc">{{ row.description }}</span>
              <span v-if="row.aliases.length" class="route__aliases">
                <el-tag v-for="alias in row.aliases" :key="alias" size="small" effect="plain">
                  {{ alias }}
                </el-tag>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.provider')" width="150">
          <template #default="{ row }">
            <el-select
              v-if="isEditor(row)"
              v-model="editor.provider"
              size="small"
              class="full-width"
            >
              <el-option
                v-for="provider in providerOptions"
                :key="provider"
                :label="providerLabel(provider)"
                :value="provider"
              />
            </el-select>
            <el-tag v-else size="small" type="info" effect="plain">
              {{ providerLabel(row.provider) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.endpoint')" min-width="260">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="editor-cell">
              <el-input
                v-model="editor.base_url"
                size="small"
                placeholder="http://10.0.0.8:8000"
              />
              <el-input v-model="editor.path" size="small" :placeholder="pathPlaceholder" />
            </div>
            <span v-else class="mono">{{ endpointOf(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.model')" width="180">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="editor-cell">
              <el-input
                v-model="editor.model"
                size="small"
                :placeholder="t('models.modelPlaceholder')"
              />
              <el-input
                v-model="editor.aliases"
                size="small"
                :placeholder="t('models.aliasesPlaceholder')"
              />
            </div>
            <span v-else class="mono">{{ row.model || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.apiKey')" width="180">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="editor-cell">
              <el-input
                v-model="editor.api_key"
                type="password"
                show-password
                size="small"
                :placeholder="apiKeyPlaceholder"
                @input="apiKeyTouched = true"
              />
              <el-checkbox
                v-if="editorMode === 'edit'"
                v-model="editor.clearApiKey"
                size="small"
              >
                {{ t('models.clearApiKey') }}
              </el-checkbox>
            </div>
            <el-tag v-else-if="row.has_api_key" size="small" type="success" effect="plain">
              {{ row.api_key_hint || t('models.keySet') }}
            </el-tag>
            <span v-else class="muted">{{ t('models.keyNone') }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.status')" width="96" align="center">
          <template #default="{ row }">
            <el-switch v-if="isEditor(row)" v-model="editor.enabled" />
            <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'" effect="plain">
              {{ row.enabled ? t('models.on') : t('models.off') }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('models.health')" width="200">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="editor-cell editor-cell--row">
              <el-button size="small" :loading="probing" @click="probeDraft">
                {{ t('models.probe') }}
              </el-button>
              <el-tag
                v-if="editorHealth"
                size="small"
                :type="healthType(editorHealth)"
                effect="plain"
                :title="healthTooltip(editorHealth)"
              >
                {{ healthLabel(editorHealth) }}
              </el-tag>
            </div>
            <div v-else class="editor-cell editor-cell--row">
              <el-tag
                size="small"
                :type="healthType(row.health)"
                effect="plain"
                :title="healthTooltip(row.health)"
              >
                {{ healthLabel(row.health) }}
              </el-tag>
              <el-button
                v-if="canWrite"
                size="small"
                text
                type="primary"
                :loading="checkingName === row.name"
                @click="checkOne(row)"
              >
                {{ t('models.check') }}
              </el-button>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('common.actions')" width="170" fixed="right">
          <template #default="{ row }">
            <div v-if="isEditor(row)" class="actions">
              <el-button size="small" type="primary" :loading="saving" @click="save">
                {{ t('common.save') }}
              </el-button>
              <el-button size="small" :disabled="saving" @click="cancelEdit">
                {{ t('common.cancel') }}
              </el-button>
            </div>
            <div v-else-if="canWrite" class="actions">
              <el-button size="small" text type="primary" @click="startEdit(row)">
                {{ t('common.edit') }}
              </el-button>
              <el-button size="small" text type="danger" @click="remove(row)">
                {{ t('common.delete') }}
              </el-button>
            </div>
            <span v-else class="muted">—</span>
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

.page__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
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

.muted {
  color: var(--el-text-color-secondary);
}

.full-width {
  width: 100%;
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
  flex-wrap: wrap;
}

.editor-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.editor-cell--row {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.actions {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

:deep(.models-view__editor-row) {
  background: var(--el-fill-color-light);
}

:deep(.models-view__editor-row td) {
  vertical-align: top;
  padding-top: 10px;
  padding-bottom: 10px;
}
</style>
