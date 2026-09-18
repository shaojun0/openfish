<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { isImportJobTerminal, type ImportJob } from '@/composables/useImportJob'
import { formatDate } from '@/utils/format'

/**
 * Import progress panel, shared by the repo list (a new import) and the repo
 * detail page (an incremental sync).  It renders whatever the last poll of
 * `/api/v1/imports/<job_id>` returned.
 *
 * §8.2 makes two states mandatory on screen and this panel is the only place
 * they are shown:
 *   * `partial: true` — the issue mirror stopped at `IMPORT_MAX_ISSUES`.
 *     A silent truncation would let a 20 000-issue repo look fully mirrored,
 *     so it gets a warning alert, not a badge.
 *   * `error` — the pipeline's own message, verbatim.
 */
const props = defineProps<{
  job: ImportJob
  /** Hide the card chrome when the caller already provides a container. */
  bare?: boolean
  /** True while the caller's poller is still ticking. */
  polling?: boolean
}>()

const { t } = useI18n()

const percent = computed(() => {
  const raw = props.job.progress
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return Math.max(0, Math.min(100, Math.round(raw)))
  }
  const total = props.job.total ?? 0
  const done = props.job.done ?? 0
  if (total > 0) return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
  return isImportJobTerminal(props.job) ? 100 : 0
})

const totalCount = computed(() => props.job.total ?? 0)
const doneCount = computed(() => props.job.done ?? 0)

const statusType = computed(() => {
  switch (String(props.job.status ?? '').toLowerCase()) {
    case 'done':
    case 'ready':
      return 'success'
    case 'error':
    case 'failed':
    case 'dead':
      return 'danger'
    case 'cancelled':
      return 'info'
    default:
      return 'primary'
  }
})

const statusLabel = computed(() => {
  const key = `import.status.${String(props.job.status ?? 'unknown').toLowerCase()}`
  return t(key)
})

/** Phases declared by §8.2, as S1's `JOB_PHASES` implements them.  An
 *  unexpected one is shown verbatim rather than as a missing i18n key. */
const KNOWN_PHASES = [
  'validate',
  'migrate',
  'poll',
  'mirror_issues',
  'index_commits',
  'build_search',
  'done',
]

const phaseLabel = computed(() => {
  const phase = String(props.job.phase ?? '').toLowerCase()
  if (KNOWN_PHASES.includes(phase)) return t(`import.phase.${phase}`)
  return phase || t('import.phaseUnknown')
})

const progressStatus = computed<'success' | 'exception' | undefined>(() => {
  const status = String(props.job.status ?? '').toLowerCase()
  if (status === 'done' || status === 'ready') return 'success'
  if (status === 'error' || status === 'failed' || status === 'dead') return 'exception'
  return undefined
})
</script>

<template>
  <el-card v-if="!bare" class="import-job" shadow="never">
    <template #header>
      <div class="import-job__header">
        <span class="card-title">{{ t('import.title') }}</span>
        <span class="import-job__tags">
          <el-tag size="small" effect="plain">#{{ job.id }}</el-tag>
          <el-tag v-if="job.mode" size="small" type="info" effect="plain">{{ job.mode }}</el-tag>
          <el-tag size="small" :type="statusType" effect="dark">{{ statusLabel }}</el-tag>
          <el-tag v-if="polling" size="small" type="warning" effect="plain">
            {{ t('import.polling') }}
          </el-tag>
        </span>
      </div>
    </template>

    <div class="import-job__body">
      <el-progress
        :percentage="percent"
        :status="progressStatus"
        :stroke-width="14"
        :text-inside="true"
      />

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item :label="t('import.phaseLabel')">{{ phaseLabel }}</el-descriptions-item>
        <el-descriptions-item :label="t('import.progress')">{{ percent }}%</el-descriptions-item>
        <el-descriptions-item :label="t('import.total')">{{ totalCount }}</el-descriptions-item>
        <el-descriptions-item :label="t('import.done')">{{ doneCount }}</el-descriptions-item>
        <el-descriptions-item v-if="job.cursor" :label="t('import.cursor')">
          <span class="mono">{{ job.cursor }}</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('import.startedAt')">
          {{ formatDate(job.started_at) }}
        </el-descriptions-item>
        <el-descriptions-item v-if="job.finished_at" :label="t('import.finishedAt')">
          {{ formatDate(job.finished_at) }}
        </el-descriptions-item>
      </el-descriptions>

      <!-- §8.2: never truncate silently. -->
      <el-alert
        v-if="job.partial"
        class="import-job__alert import-job__alert--partial"
        type="warning"
        show-icon
        :closable="false"
        :title="t('import.partialTitle')"
        :description="t('import.partialDesc', { done: doneCount, total: totalCount })"
      />

      <el-alert
        v-if="job.error"
        class="import-job__alert"
        type="error"
        show-icon
        :closable="false"
        :title="t('import.errorTitle')"
        :description="job.error"
      />
    </div>
  </el-card>

  <div v-else class="import-job import-job--bare">
    <div class="import-job__header">
      <span class="import-job__tags">
        <el-tag size="small" effect="plain">#{{ job.id }}</el-tag>
        <el-tag size="small" :type="statusType" effect="dark">{{ statusLabel }}</el-tag>
        <el-tag v-if="job.phase" size="small" type="info" effect="plain">{{ phaseLabel }}</el-tag>
        <el-tag v-if="polling" size="small" type="warning" effect="plain">
          {{ t('import.polling') }}
        </el-tag>
      </span>
    </div>
    <el-progress
      :percentage="percent"
      :status="progressStatus"
      :stroke-width="14"
      :text-inside="true"
    />
    <el-alert
      v-if="job.partial"
      class="import-job__alert import-job__alert--partial"
      type="warning"
      show-icon
      :closable="false"
      :title="t('import.partialTitle')"
      :description="t('import.partialDesc', { done: doneCount, total: totalCount })"
    />
    <el-alert
      v-if="job.error"
      class="import-job__alert"
      type="error"
      show-icon
      :closable="false"
      :title="t('import.errorTitle')"
      :description="job.error"
    />
  </div>
</template>

<style scoped>
.import-job__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.import-job__tags {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.import-job__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.import-job--bare {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.import-job__alert :deep(.el-alert__description) {
  white-space: pre-wrap;
}
</style>
