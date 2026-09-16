<script setup lang="ts">
/**
 * Debian offline relay panel — the four air-gap steps in the browser.
 *
 * The protocol itself is machine-first (`/debian/offline/*`): the CLI and
 * `curl` drive the same endpoints.  This panel is a convenience in front of it,
 * so every button here does exactly what the equivalent CLI command does —
 * export a snapshot, diff it into a plan, pack a bundle, import a bundle — and
 * nothing is stored on the server between the steps beyond the bundles it
 * builds.
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  buildDebianBundle,
  computeDebianPlan,
  exportDebianSnapshot,
  fetchDebianOfflineStatus,
  importDebianBundle,
  saveBlob,
  type DebianOfflineBundle,
  type DebianOfflineImport,
  type DebianOfflineStatus,
} from '@/api'
import { apiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { formatBytes, formatDate } from '@/utils/format'

const emit = defineEmits<{ imported: [] }>()

const { t } = useI18n()
const session = useSessionStore()

const status = ref<DebianOfflineStatus | null>(null)
const loading = ref(true)
const busy = ref<'' | 'snapshot' | 'plan' | 'bundle' | 'import'>('')

const canOperate = computed(() => session.can('debian:offline'))
const canImport = computed(() => session.can('debian:upload'))
/** The panel is worthless without at least one half of the relay. */
const canSee = computed(() => canOperate.value || canImport.value)

const only = ref('')
const allowDowngrade = ref(false)
const verifyHashes = ref(false)
const recommends = ref(false)

const snapshotInput = ref<HTMLInputElement | null>(null)
const planInput = ref<HTMLInputElement | null>(null)
const bundleInput = ref<HTMLInputElement | null>(null)
const snapshotFile = ref<File | null>(null)
const planFile = ref<File | null>(null)
const bundleFile = ref<File | null>(null)

const lastPlan = ref<{ packages: number; bytes: number; unresolved: number; integrity: string } | null>(null)
const lastBundle = ref<DebianOfflineBundle | null>(null)
const lastImport = ref<DebianOfflineImport | null>(null)

async function loadStatus(): Promise<void> {
  loading.value = true
  try {
    status.value = await fetchDebianOfflineStatus()
  } catch (e) {
    ElMessage.error(apiError(e) || t('debian.offline.statusFailed'))
  } finally {
    loading.value = false
  }
}

function onPick(kind: 'snapshot' | 'plan' | 'bundle', event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  if (kind === 'snapshot') snapshotFile.value = file
  else if (kind === 'plan') planFile.value = file
  else bundleFile.value = file
}

/** Fetch a previously built bundle through the normal download route. */
function downloadBundle(filename: string): void {
  window.open(`/debian/offline/bundles/${encodeURIComponent(filename)}`, '_blank', 'noopener')
}

/** The plan is a text document; its header carries the counts worth showing. */
function parsePlanHeader(text: string): { packages: number; bytes: number; unresolved: number; integrity: string } {
  const header: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const [key, value] = line.split('\t')
    if (!key || value === undefined) continue
    if (key === 'columns') break
    header[key] = value.trim()
  }
  return {
    packages: Number(header.count || 0),
    bytes: Number(header.bytes || 0),
    unresolved: Number(header.unresolved || 0),
    integrity: header.snapshot_integrity || '',
  }
}

async function runSnapshot(): Promise<void> {
  busy.value = 'snapshot'
  try {
    const artifact = await exportDebianSnapshot()
    saveBlob(artifact.blob, artifact.filename)
    ElMessage.success(t('debian.offline.snapshotDone', { name: artifact.filename }))
  } catch (e) {
    ElMessage.error(apiError(e) || t('debian.offline.snapshotFailed'))
  } finally {
    busy.value = ''
  }
}

async function runPlan(): Promise<void> {
  if (!snapshotFile.value) {
    ElMessage.warning(t('debian.offline.needSnapshot'))
    return
  }
  busy.value = 'plan'
  try {
    const artifact = await computeDebianPlan(snapshotFile.value, {
      only: only.value.trim(),
      allowDowngrade: allowDowngrade.value,
      verifyHashes: verifyHashes.value,
      recommends: recommends.value,
    })
    lastPlan.value = parsePlanHeader(await artifact.blob.text())
    saveBlob(artifact.blob, artifact.filename)
    ElMessage.success(
      t('debian.offline.planDone', { count: lastPlan.value.packages }),
    )
  } catch (e) {
    ElMessage.error(apiError(e) || t('debian.offline.planFailed'))
  } finally {
    busy.value = ''
  }
}

async function runBundle(): Promise<void> {
  if (!planFile.value) {
    ElMessage.warning(t('debian.offline.needPlan'))
    return
  }
  busy.value = 'bundle'
  try {
    const bundle = await buildDebianBundle(planFile.value)
    lastBundle.value = bundle
    ElMessage.success(
      t('debian.offline.bundleDone', { count: bundle.packages, size: bundle.size_human }),
    )
    if (bundle.download_url) window.open(bundle.download_url, '_blank', 'noopener')
    await loadStatus()
  } catch (e) {
    ElMessage.error(apiError(e) || t('debian.offline.bundleFailed'))
  } finally {
    busy.value = ''
  }
}

async function runImport(): Promise<void> {
  if (!bundleFile.value) {
    ElMessage.warning(t('debian.offline.needBundle'))
    return
  }
  busy.value = 'import'
  try {
    lastImport.value = await importDebianBundle(bundleFile.value)
    const report = lastImport.value
    ElMessage.success(
      t('debian.offline.importDone', { imported: report.imported, skipped: report.skipped }),
    )
    emit('imported')
    await loadStatus()
  } catch (e) {
    ElMessage.error(apiError(e) || t('debian.offline.importFailed'))
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  if (canSee.value) loadStatus()
})
</script>

<template>
  <div v-if="canSee" class="debian-offline">
    <el-card shadow="never">
      <template #header>
        <div class="header-row">
          <span class="card-title">{{ t('debian.offline.statusTitle') }}</span>
          <el-button size="small" :loading="loading" @click="loadStatus">
            <el-icon><Refresh /></el-icon>
            <span class="btn-label">{{ t('common.refresh') }}</span>
          </el-button>
        </div>
      </template>
      <div v-loading="loading" class="status">
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.upstream') }}</span>
          <el-tag
            class="mono"
            :type="status?.configured ? 'success' : 'info'"
            effect="plain"
          >
            {{ status?.upstream || t('debian.offline.localOnly') }}
          </el-tag>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.suites') }}</span>
          <span class="status__tags">
            <el-tag v-for="item in status?.suites || []" :key="item" size="small" effect="plain">
              {{ item }}
            </el-tag>
          </span>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.components') }}</span>
          <span class="status__tags">
            <el-tag v-for="item in status?.components || []" :key="item" size="small" effect="plain">
              {{ item }}
            </el-tag>
          </span>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.arches') }}</span>
          <span class="status__tags">
            <el-tag v-for="item in status?.arches || []" :key="item" size="small" effect="plain">
              {{ item }}
            </el-tag>
          </span>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.rootLabel') }}</span>
          <span class="mono status__path">{{ status?.root || '—' }}</span>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.offlineDir') }}</span>
          <span class="mono status__path">{{ status?.offline_dir || '—' }}</span>
        </div>
        <div class="status__item">
          <span class="status__label">{{ t('debian.offline.limitLabel') }}</span>
          <span>{{ t('debian.offline.limitValue', { mb: status?.max_mb ?? 0 }) }}</span>
        </div>
      </div>
    </el-card>

    <el-card v-if="canOperate" shadow="never">
      <template #header>
        <span class="card-title">{{ t('debian.offline.stepsTitle') }}</span>
      </template>

      <ol class="steps">
        <li class="step">
          <div class="step__head">
            <span class="step__no">1</span>
            <span class="step__title">{{ t('debian.offline.step1Title') }}</span>
          </div>
          <p class="step__desc">{{ t('debian.offline.step1Desc') }}</p>
          <el-button type="primary" :loading="busy === 'snapshot'" @click="runSnapshot">
            <el-icon><Download /></el-icon>
            <span class="btn-label">{{ t('debian.offline.exportSnapshot') }}</span>
          </el-button>
        </li>

        <li class="step">
          <div class="step__head">
            <span class="step__no">2</span>
            <span class="step__title">{{ t('debian.offline.step2Title') }}</span>
          </div>
          <p class="step__desc">{{ t('debian.offline.step2Desc') }}</p>
          <input
            ref="snapshotInput"
            class="hidden-input"
            type="file"
            accept=".txt,text/plain"
            @change="onPick('snapshot', $event)"
          />
          <div class="step__actions">
            <el-button @click="snapshotInput?.click()">
              <el-icon><FolderOpened /></el-icon>
              <span class="btn-label">
                {{ snapshotFile?.name || t('debian.offline.chooseSnapshot') }}
              </span>
            </el-button>
            <el-button
              type="primary"
              :disabled="!snapshotFile"
              :loading="busy === 'plan'"
              @click="runPlan"
            >
              <el-icon><DocumentChecked /></el-icon>
              <span class="btn-label">{{ t('debian.offline.makePlan') }}</span>
            </el-button>
          </div>
          <div class="options">
            <el-input
              v-model="only"
              class="options__only"
              :placeholder="t('debian.offline.onlyPlaceholder')"
              clearable
            />
            <el-checkbox v-model="allowDowngrade">
              {{ t('debian.offline.allowDowngrade') }}
            </el-checkbox>
            <el-checkbox v-model="verifyHashes">
              {{ t('debian.offline.verifyHashes') }}
            </el-checkbox>
            <el-checkbox v-model="recommends">
              {{ t('debian.offline.recommends') }}
            </el-checkbox>
          </div>
        </li>

        <li class="step">
          <div class="step__head">
            <span class="step__no">3</span>
            <span class="step__title">{{ t('debian.offline.step3Title') }}</span>
          </div>
          <p class="step__desc">{{ t('debian.offline.step3Desc') }}</p>
          <input
            ref="planInput"
            class="hidden-input"
            type="file"
            accept=".txt,text/plain"
            @change="onPick('plan', $event)"
          />
          <div class="step__actions">
            <el-button @click="planInput?.click()">
              <el-icon><FolderOpened /></el-icon>
              <span class="btn-label">{{ planFile?.name || t('debian.offline.choosePlan') }}</span>
            </el-button>
            <el-button
              type="primary"
              :disabled="!planFile"
              :loading="busy === 'bundle'"
              @click="runBundle"
            >
              <el-icon><Box /></el-icon>
              <span class="btn-label">{{ t('debian.offline.buildBundle') }}</span>
            </el-button>
          </div>
        </li>

        <li class="step">
          <div class="step__head">
            <span class="step__no">4</span>
            <span class="step__title">{{ t('debian.offline.step4Title') }}</span>
          </div>
          <p class="step__desc">{{ t('debian.offline.step4Desc') }}</p>
          <p v-if="!canImport" class="step__desc step__desc--warn">
            {{ t('debian.offline.importPermission') }}
          </p>
          <template v-else>
            <input
              ref="bundleInput"
              class="hidden-input"
              type="file"
              accept=".gz,.tgz,application/gzip"
              @change="onPick('bundle', $event)"
            />
            <div class="step__actions">
              <el-button @click="bundleInput?.click()">
                <el-icon><FolderOpened /></el-icon>
                <span class="btn-label">
                  {{ bundleFile?.name || t('debian.offline.chooseBundle') }}
                </span>
              </el-button>
              <el-button
                type="primary"
                :disabled="!bundleFile"
                :loading="busy === 'import'"
                @click="runImport"
              >
                <el-icon><UploadFilled /></el-icon>
                <span class="btn-label">{{ t('debian.offline.doImport') }}</span>
              </el-button>
            </div>
          </template>
        </li>
      </ol>
    </el-card>

    <el-card v-if="lastPlan || lastBundle || lastImport" shadow="never">
      <template #header>
        <span class="card-title">{{ t('debian.offline.resultTitle') }}</span>
      </template>
      <el-descriptions :column="2" border>
        <template v-if="lastPlan">
          <el-descriptions-item :label="t('debian.offline.planResult')">
            {{ t('debian.offline.planSummary', {
              count: lastPlan.packages,
              size: formatBytes(lastPlan.bytes),
            }) }}
            <el-tag
              v-if="lastPlan.unresolved"
              size="small"
              type="warning"
              effect="plain"
              class="result__tag"
            >
              {{ t('debian.offline.unresolved', { count: lastPlan.unresolved }) }}
            </el-tag>
            <el-tag
              v-if="lastPlan.integrity === 'mismatch'"
              size="small"
              type="danger"
              effect="plain"
              class="result__tag"
            >
              {{ t('debian.offline.integrityMismatch') }}
            </el-tag>
          </el-descriptions-item>
        </template>
        <template v-if="lastBundle">
          <el-descriptions-item :label="t('debian.offline.bundleResult')">
            <span class="mono">{{ lastBundle.filename }}</span>
            — {{ t('debian.offline.bundleSummary', {
              count: lastBundle.packages,
              skipped: lastBundle.skipped,
              size: lastBundle.size_human,
            }) }}
            <div class="mono result__digest">{{ lastBundle.sha256 }}</div>
          </el-descriptions-item>
        </template>
        <template v-if="lastImport">
          <el-descriptions-item :label="t('debian.offline.importResult')">
            {{ t('debian.offline.importSummary', {
              imported: lastImport.imported,
              skipped: lastImport.skipped,
              failed: lastImport.failed,
              size: lastImport.total_size_human,
            }) }}
          </el-descriptions-item>
        </template>
      </el-descriptions>
    </el-card>

    <el-card v-if="status && status.bundles.length" shadow="never">
      <template #header>
        <span class="card-title">
          {{ t('debian.offline.bundlesTitle', { count: status.bundle_count }) }}
        </span>
      </template>
      <el-table :data="status.bundles" stripe>
        <el-table-column :label="t('debian.offline.bundleFile')" min-width="320">
          <template #default="{ row }">
            <span class="mono">{{ row.filename }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="size_human" :label="t('debian.offline.bundleSize')" width="120" align="right" />
        <el-table-column :label="t('debian.offline.bundleModified')" width="170">
          <template #default="{ row }">{{ formatDate(row.modified) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="130" align="right">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              @click="downloadBundle(row.filename)"
            >
              <el-icon><Download /></el-icon>
              <span class="btn-label">{{ t('debian.offline.downloadBundle') }}</span>
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.debian-offline {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.status {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px 24px;
}

.status__item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.status__label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  flex: none;
}

.status__tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.status__path {
  font-size: 12px;
  overflow-wrap: anywhere;
}

.steps {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.step {
  border-left: 2px solid var(--el-border-color);
  padding-left: 14px;
}

.step__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.step__no {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 12px;
  flex: none;
}

.step__title {
  font-weight: 600;
}

.step__desc {
  margin: 6px 0 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.step__desc--warn {
  color: var(--el-color-warning);
}

.step__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.options {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.options__only {
  max-width: 340px;
}

.hidden-input {
  display: none;
}

.result__tag {
  margin-left: 8px;
}

.result__digest {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}
</style>
