<script setup lang="ts">
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import {
  asList,
  cancelAgentTask,
  fetchAgentTaskLog,
  fetchFinding,
  fetchFindingEvidence,
  fetchRepos,
  fixFinding,
  retryAgentTask,
} from '@/api/agentHub'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import { useSessionStore } from '@/stores/session'
import { formatDate, formatDateOnly } from '@/utils/format'

/**
 * One finding (`/findings/<id>`).
 *
 * This is the page a developer lands on from the debt board, and its job is to
 * make a decision *defensible*: the explanation the agent wrote, the gates the
 * run actually passed, the historical issues this problem was already
 * discussed in (§4.6/§8.3), and the full `FindingEvent` timeline — including
 * the automatic transitions (`stale`, reactivation) nobody triggered by hand.
 *
 * `detail` is LLM-authored prose, so it is printed as text, not Markdown. The
 * only place HTML is rendered in this slice is the issue body, and that is
 * `v-html`-free on purpose (§8.4).
 */
interface EvidenceRef {
  issue_number?: number | null
  number?: number | null
  issue?: { number?: number | null; url?: string | null; title?: string | null } | null
  relation?: string | null
  /** `evidence` = linked through `finding_evidence`; `keyword` = anchor recall. */
  origin?: 'evidence' | 'keyword'
  url?: string | null
  title?: string | null
  state?: string | null
  author?: string | null
}

interface Finding {
  id: number | string
  /** S3 serialises the numeric repo id; `repo_slug` only when joined in. */
  repo_id?: number | null
  repo_slug?: string | null
  rule_id?: string | null
  level?: string | null
  severity?: string | null
  status?: string | null
  file_path?: string | null
  symbol?: string | null
  line_hint?: number | null
  title?: string | null
  detail?: string | null
  fingerprint?: string | null
  seen_count?: number | null
  owner?: string | null
  due?: string | null
  decided_by?: string | null
  decided_at?: string | null
  pr_url?: string | null
  created_at?: string | null
  updated_at?: string | null
  evidence?: EvidenceRef[] | null
  events?: FindingEvent[] | null
}

interface FindingEvent {
  at?: string | null
  actor?: string | null
  from_status?: string | null
  to_status?: string | null
  reason?: string | null
  run_id?: number | string | null
}

interface GateResult {
  gate?: string | null
  passed?: boolean | null
  exit_code?: number | null
  stdout_tail?: string | null
  duration_ms?: number | null
}

interface RunSummary {
  id?: number | string
  commit_sha?: string | null
  policy_hash?: string | null
  findings_new?: number | null
  findings_matched?: number | null
  gates_total?: number | null
  gates_passed?: number | null
  gates_failed?: number | null
  gates?: GateResult[] | null
  status?: string | null
  started_at?: string | null
  finished_at?: string | null
}

interface AgentTask {
  id: number | string
  repo_slug?: string | null
  kind?: string | null
  status?: string | null
  payload?: unknown
  attempts?: number | null
  max_attempts?: number | null
  result_ref?: string | null
  log_ref?: string | null
  created_at?: string | null
}

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const id = computed(() => String(route.params.id ?? ''))
const canRun = computed(() => session.can('agent:run'))
/** `GET /agent/tasks/<id>/log` is `agent:admin`, so the button is too. */
const canViewLog = computed(() => session.can('agent:admin'))

const finding = ref<Finding | null>(null)
const events = ref<FindingEvent[]>([])
const evidence = ref<EvidenceRef[]>([])
const gates = ref<GateResult[]>([])
const run = ref<RunSummary | null>(null)
const loading = ref(true)
const loadError = ref('')

/** `repo_id -> slug`; the evidence route is keyed by slug, S3 by numeric id. */
const repoSlugs = ref<Record<string, string>>({})
const evidenceLoading = ref(false)
const evidenceError = ref('')
const evidenceTruncated = ref(false)
const evidenceOmitted = ref(0)

const task = ref<AgentTask | null>(null)
const fixing = ref(false)
const logVisible = ref(false)
const logLoading = ref(false)
const logText = ref('')

const today = dayjs().format('YYYY-MM-DD')

/** The repo as `owner/name`, falling back to `#<id>` when the map missed it. */
const repoLabel = computed(() => {
  const joined = String(finding.value?.repo_slug ?? '')
  if (joined) return joined
  const slug = repoSlugOf(finding.value?.repo_id)
  if (slug) return slug
  return finding.value?.repo_id ? `#${finding.value.repo_id}` : '—'
})

function slugMap(data: unknown): Record<string, string> {
  const map: Record<string, string> = {}
  for (const repo of asList(data, ['items', 'repos']) as Array<{
    id?: number
    slug?: string
  }>) {
    if (repo.id !== undefined && repo.slug) map[String(repo.id)] = repo.slug
  }
  return map
}

function repoSlugOf(repoId: number | null | undefined): string {
  if (repoId === undefined || repoId === null) return ''
  return repoSlugs.value[String(repoId)] ?? ''
}

/**
 * The finding's linked history (`finding_evidence`) plus anchor recall, read
 * from `GET /repos/<slug>/context/search?finding_id=…` (§4.6 / §13.2). Failure
 * is deliberately **not** fatal: the card shows its error and empty state while
 * the finding itself still renders.
 */
async function loadEvidence(slug: string): Promise<EvidenceRef[]> {
  if (!slug) return []
  evidenceLoading.value = true
  evidenceError.value = ''
  evidenceTruncated.value = false
  evidenceOmitted.value = 0
  try {
    const data = (await fetchFindingEvidence(slug, id.value, { limit: 20 })) as {
      items?: EvidenceRef[]
      truncated?: boolean
      omitted?: number
    }
    evidenceTruncated.value = data?.truncated === true
    evidenceOmitted.value = Number(data?.omitted ?? 0)
    return asList(data, ['items']) as EvidenceRef[]
  } catch (e) {
    evidenceError.value = apiError(e) || t('findingDetail.evidenceFailed')
    return []
  } finally {
    evidenceLoading.value = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    // The board passes `?repo=<slug>` (see Findings.vue), so the evidence
    // request starts in the very same wave as the detail request. On a bare
    // deep link the slug is unknown until the finding answers, so it is
    // resolved from `repo_id` afterwards — one extra request, never N+1.
    const slugFromRoute = String(route.query.repo ?? '')
    const [data, repos, early] = await Promise.all([
      fetchFinding(id.value),
      fetchRepos({ per_page: 200 }).catch(() => null),
      slugFromRoute ? loadEvidence(slugFromRoute) : Promise.resolve(null),
    ])
    repoSlugs.value = slugMap(repos)
    const row =
      (data?.finding as Finding | undefined) ?? (data as unknown as Finding | null) ?? null
    finding.value = row
    events.value = asList(
      data?.events ?? (row as { events?: FindingEvent[] } | null)?.events,
      ['items'],
    ) as FindingEvent[]
    run.value = (data?.run as RunSummary | undefined) ?? null
    gates.value = asList(data?.gates ?? run.value?.gates, ['items']) as GateResult[]
    if (early === null) {
      evidence.value = await loadEvidence(
        String(row?.repo_slug ?? '') || repoSlugOf(row?.repo_id),
      )
    } else {
      evidence.value = early
    }
  } catch (e) {
    finding.value = null
    loadError.value = apiError(e) || t('findingDetail.loadFailed')
  } finally {
    loading.value = false
  }
}

// ── Gates summary (§9.4) ─────────────────────────────────────────────

const gatesTotal = computed(() => run.value?.gates_total ?? gates.value.length)
const gatesPassed = computed(
  () =>
    run.value?.gates_passed ??
    gates.value.filter((gate) => gate.passed === true).length,
)
const gatesFailed = computed(
  () =>
    run.value?.gates_failed ??
    gates.value.filter((gate) => gate.passed === false).length,
)

// ── Evidence helpers ─────────────────────────────────────────────────

function evidenceNumber(item: EvidenceRef): number | null {
  const number = item.issue_number ?? item.number ?? item.issue?.number
  return typeof number === 'number' ? number : null
}

function evidenceHref(item: EvidenceRef): string {
  const external = item.url ?? item.issue?.url ?? null
  if (external) return external
  const number = evidenceNumber(item)
  const slug = String(finding.value?.repo_slug ?? '')
  if (!number || !slug) return ''
  const path = slug.split('/').map(encodeURIComponent).join('/')
  return `/repos/${path}?issue=${number}`
}

function evidenceLabel(item: EvidenceRef): string {
  const number = evidenceNumber(item)
  return number ? `#${number}` : t('findings.evidenceLink')
}

function relationLabel(relation: string | null | undefined): string {
  switch (relation) {
    case 'duplicate_of':
      return t('findingDetail.relationDuplicateOf')
    case 'fixed_by':
      return t('findingDetail.relationFixedBy')
    case 'mentions':
      return t('findingDetail.relationMentions')
    case null:
    case undefined:
    case '':
      // No `finding_evidence` row: the item came back from anchor recall.
      return t('findingDetail.relationRecall')
    default:
      return relation
  }
}

// ── Display helpers ──────────────────────────────────────────────────

function levelLabel(level: string | null | undefined): string {
  if (level === 'blocking') return t('findings.levelBlocking')
  if (level === 'debt') return t('findings.levelDebt')
  return level || t('common.unknown')
}

function statusLabel(status: string | null | undefined): string {
  const known = ['open', 'acknowledged', 'wontfix', 'fixed', 'stale']
  return known.includes(String(status))
    ? t(`findings.status.${status}`)
    : status || t('common.unknown')
}

function statusType(status: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'fixed':
      return 'success'
    case 'open':
      return 'danger'
    case 'acknowledged':
    case 'wontfix':
      return 'warning'
    default:
      return 'info'
  }
}

function severityLabel(severity: string | null | undefined): string {
  const known = ['critical', 'high', 'medium', 'low']
  return known.includes(String(severity))
    ? t(`findings.severity.${severity}`)
    : severity || t('common.unknown')
}

function eventType(event: FindingEvent): 'success' | 'warning' | 'danger' | 'primary' {
  switch (event.to_status) {
    case 'fixed':
      return 'success'
    case 'open':
      return 'danger'
    case 'acknowledged':
    case 'wontfix':
      return 'warning'
    default:
      return 'primary'
  }
}

const overdue = computed(() => {
  const due = finding.value?.due
  if (!due) return false
  if (!['open', 'acknowledged', 'wontfix'].includes(String(finding.value?.status))) return false
  return String(due) < today
})

const repeated = computed(() => Number(finding.value?.seen_count ?? 0) > 1)

// ── Fix task (§6.4, §5.3) ────────────────────────────────────────────

async function createFixTask(): Promise<void> {
  fixing.value = true
  try {
    // S3 answers a receipt (`{finding_id, task_id, kind, queued}`), not an
    // AgentTask — `queued: false` means the runtime refused the job and the
    // page must not pretend a task exists.
    const receipt = (await fixFinding(id.value, {})) as {
      finding_id?: number
      task_id?: number | null
      kind?: string | null
      queued?: boolean
    }
    if (receipt?.queued === false || receipt?.task_id === null || receipt?.task_id === undefined) {
      task.value = null
      ElMessage.warning(t('findingDetail.fixNotQueued'))
      return
    }
    task.value = {
      id: receipt.task_id,
      kind: receipt.kind ?? 'fix',
      status: 'queued',
    }
    ElMessage.success(t('findingDetail.fixCreated', { id: receipt.task_id }))
  } catch (e) {
    ElMessage.error(apiError(e) || t('findingDetail.fixFailed'))
  } finally {
    fixing.value = false
  }
}

async function retryTask(): Promise<void> {
  if (!task.value) return
  try {
    task.value = (await retryAgentTask(task.value.id)) as AgentTask
    ElMessage.success(t('findingDetail.taskRetried'))
  } catch (e) {
    ElMessage.error(apiError(e) || t('findingDetail.taskActionFailed'))
  }
}

async function cancelTask(): Promise<void> {
  if (!task.value) return
  try {
    task.value = (await cancelAgentTask(task.value.id)) as AgentTask
    ElMessage.success(t('findingDetail.taskCancelled'))
  } catch (e) {
    ElMessage.error(apiError(e) || t('findingDetail.taskActionFailed'))
  }
}

async function openLog(): Promise<void> {
  if (!task.value) return
  logVisible.value = true
  logLoading.value = true
  logText.value = ''
  try {
    // S4 answers references, not bytes: the log lives inside the sandbox work
    // directory.  Render the references readably instead of faking a log body.
    const data = (await fetchAgentTaskLog(task.value.id)) as {
      task_id?: number
      status?: string | null
      result_ref?: string | null
      log_ref?: string | null
    }
    const lines = [
      data?.task_id !== undefined ? `task #${data.task_id}` : '',
      data?.status ? `status: ${data.status}` : '',
      data?.result_ref ? `result_ref: ${data.result_ref}` : '',
      data?.log_ref ? `log_ref: ${data.log_ref}` : '',
    ].filter(Boolean)
    logText.value = lines.join('\n')
  } catch (e) {
    ElMessage.error(apiError(e) || t('findingDetail.logFailed'))
  } finally {
    logLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page finding-detail">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('findingDetail.title', { id }) }}</h1>
        <p class="page__description">{{ t('findingDetail.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button @click="router.push('/findings')">
          <el-icon><Back /></el-icon>
          <span class="btn-label">{{ t('findingDetail.back') }}</span>
        </el-button>
        <el-button v-if="canRun && finding" type="primary" :loading="fixing" @click="createFixTask">
          <el-icon><MagicStick /></el-icon>
          <span class="btn-label">{{ t('findingDetail.createFixTask') }}</span>
        </el-button>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="loadError"
      type="error"
      show-icon
      :closable="false"
      :title="t('findingDetail.loadFailed')"
      :description="loadError"
    />

    <template v-if="finding">
      <!-- Status strip -->
      <div class="finding-detail__tags">
        <el-tag :type="finding.level === 'blocking' ? 'danger' : 'warning'" effect="dark">
          {{ levelLabel(finding.level) }}
        </el-tag>
        <el-tag size="small" effect="plain">{{ severityLabel(finding.severity) }}</el-tag>
        <el-tag size="small" :type="statusType(finding.status)" effect="plain">
          {{ statusLabel(finding.status) }}
        </el-tag>
        <el-tag v-if="overdue" type="danger" effect="plain">{{ t('findings.overdue') }}</el-tag>
        <el-tag v-if="repeated" type="warning" effect="plain">
          {{ t('findings.repeated', { count: finding.seen_count ?? 0 }) }}
        </el-tag>
        <el-link
          v-if="finding.pr_url"
          type="primary"
          :href="finding.pr_url"
          target="_blank"
          rel="noopener"
        >
          {{ t('findingDetail.prLink') }}
        </el-link>
      </div>

      <!-- Overview -->
      <el-card shadow="never">
        <template #header>
          <span class="card-title">{{ t('findingDetail.overview') }}</span>
        </template>
        <el-descriptions :column="2" border>
          <el-descriptions-item :label="t('findingDetail.repo')">
            <span class="mono">{{ repoLabel }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.ruleId')">
            <span class="mono">{{ finding.rule_id || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.location')">
            <span class="mono">{{ finding.file_path || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.symbol')">
            <span class="mono">
              {{ finding.symbol || '—' }}<template v-if="finding.line_hint">:{{ finding.line_hint }}</template>
            </span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.fingerprint')">
            <span class="mono finding-detail__fingerprint">{{ finding.fingerprint || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.seenCount')">
            {{ finding.seen_count ?? 0 }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.owner')">
            {{ finding.owner || '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.due')">
            {{ finding.due ? formatDateOnly(finding.due) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.decidedBy')">
            {{ finding.decided_by || '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.decidedAt')">
            {{ formatDate(finding.decided_at) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.createdAt')">
            {{ formatDate(finding.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('findingDetail.updatedAt')">
            {{ formatDate(finding.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- Explanation -->
      <el-card shadow="never">
        <template #header>
          <span class="card-title">{{ t('findingDetail.explanation') }}</span>
        </template>
        <p v-if="finding.title" class="finding-detail__title">{{ finding.title }}</p>
        <pre v-if="finding.detail" class="finding-detail__detail">{{ finding.detail }}</pre>
        <el-empty v-else :description="t('findingDetail.detailEmpty')" />
      </el-card>

      <!-- Gates (§9.4) -->
      <el-card shadow="never">
        <template #header>
          <div class="finding-detail__card-header">
            <span class="card-title">{{ t('findingDetail.gatesTitle') }}</span>
            <span class="finding-detail__gates-summary">
              <el-tag size="small" type="success" effect="plain">
                {{ t('findingDetail.gatesPassed', { count: gatesPassed }) }}
              </el-tag>
              <el-tag size="small" :type="gatesFailed > 0 ? 'danger' : 'info'" effect="plain">
                {{ t('findingDetail.gatesFailed', { count: gatesFailed }) }}
              </el-tag>
              <el-tag size="small" effect="plain">
                {{ t('findingDetail.gatesTotal', { count: gatesTotal }) }}
              </el-tag>
            </span>
          </div>
        </template>

        <el-table v-if="gates.length" :data="gates" size="small" :empty-text="t('findingDetail.noGates')">
          <el-table-column :label="t('findingDetail.gate')" min-width="200">
            <template #default="{ row }">
              <span class="mono">{{ row.gate || t('common.unknown') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('findingDetail.gateResult')" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.passed ? 'success' : 'danger'" effect="plain">
                {{ row.passed ? t('findingDetail.gatePassed') : t('findingDetail.gateFailed') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('findingDetail.exitCode')" width="100" align="right">
            <template #default="{ row }">{{ row.exit_code ?? '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('findingDetail.duration')" width="120" align="right">
            <template #default="{ row }">
              {{ row.duration_ms != null ? `${row.duration_ms} ms` : '—' }}
            </template>
          </el-table-column>
          <el-table-column :label="t('findingDetail.stdout')" min-width="260">
            <template #default="{ row }">
              <span class="finding-detail__stdout mono">{{ row.stdout_tail || '—' }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else :description="t('findingDetail.noGates')" />
      </el-card>

      <!-- Historical issues (§4.6) -->
      <el-card v-loading="evidenceLoading" shadow="never">
        <template #header>
          <span class="card-title">{{ t('findingDetail.evidenceTitle') }}</span>
        </template>
        <p class="finding-detail__hint">{{ t('findingDetail.evidenceDesc') }}</p>
        <el-alert
          v-if="evidenceError"
          class="finding-detail__evidence-note"
          type="warning"
          show-icon
          :closable="false"
          :title="t('findingDetail.evidenceFailed')"
          :description="evidenceError"
        />
        <p v-if="evidenceTruncated" class="finding-detail__hint">
          {{ t('findingDetail.evidenceTruncated', { count: evidenceOmitted }) }}
        </p>
        <ul v-if="evidence.length" class="finding-detail__evidence">
          <li v-for="(item, index) in evidence" :key="index" class="finding-detail__evidence-item">
            <el-tag size="small" effect="plain">{{ relationLabel(item.relation) }}</el-tag>
            <a
              class="finding-detail__evidence-link"
              :href="evidenceHref(item) || undefined"
              target="_blank"
              rel="noopener"
            >
              {{ evidenceLabel(item) }}
            </a>
            <span v-if="item.title || item.issue?.title" class="finding-detail__evidence-title">
              {{ item.title || item.issue?.title }}
            </span>
          </li>
        </ul>
        <el-empty v-else-if="!evidenceLoading" :description="t('findingDetail.noEvidence')" />
      </el-card>

      <!-- Event history -->
      <el-card shadow="never">
        <template #header>
          <span class="card-title">{{ t('findingDetail.eventsTitle') }}</span>
        </template>
        <el-timeline v-if="events.length">
          <el-timeline-item
            v-for="(event, index) in events"
            :key="index"
            :timestamp="formatDate(event.at)"
            :type="eventType(event)"
            placement="top"
          >
            <div class="finding-detail__event">
              <span class="finding-detail__event-transition">
                {{ event.from_status || t('findingDetail.eventStart') }}
                →
                {{ event.to_status || t('common.unknown') }}
              </span>
              <span class="finding-detail__event-actor">{{ event.actor || t('common.unknown') }}</span>
              <span v-if="event.reason" class="finding-detail__event-reason">{{ event.reason }}</span>
              <span v-if="event.run_id" class="finding-detail__event-run mono">
                run #{{ event.run_id }}
              </span>
            </div>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else :description="t('findingDetail.noEvents')" />
      </el-card>

      <!-- Fix task -->
      <el-card shadow="never">
        <template #header>
          <div class="finding-detail__card-header">
            <span class="card-title">{{ t('findingDetail.fixTitle') }}</span>
            <el-button
              v-if="canRun"
              type="primary"
              size="small"
              :loading="fixing"
              @click="createFixTask"
            >
              <el-icon><MagicStick /></el-icon>
              <span class="btn-label">{{ t('findingDetail.createFixTask') }}</span>
            </el-button>
          </div>
        </template>
        <p class="finding-detail__hint">{{ t('findingDetail.fixDesc') }}</p>

        <template v-if="task">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item :label="t('findingDetail.taskId')">
              <span class="mono">#{{ task.id }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('findingDetail.taskKind')">
              {{ task.kind || '—' }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('findingDetail.taskStatus')">
              {{ task.status || t('common.unknown') }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('findingDetail.taskLogRef')">
              <span class="mono">{{ task.log_ref || task.result_ref || '—' }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="finding-detail__task-actions">
            <el-button v-if="canViewLog" size="small" @click="openLog">
              <el-icon><Document /></el-icon>
              <span class="btn-label">{{ t('findingDetail.viewLog') }}</span>
            </el-button>
            <el-button size="small" @click="retryTask">
              <el-icon><RefreshRight /></el-icon>
              <span class="btn-label">{{ t('findingDetail.taskRetry') }}</span>
            </el-button>
            <el-button size="small" type="danger" plain @click="cancelTask">
              <el-icon><CircleClose /></el-icon>
              <span class="btn-label">{{ t('findingDetail.taskCancel') }}</span>
            </el-button>
          </div>
        </template>
        <el-empty v-else :description="t('findingDetail.noTask')" />
      </el-card>
    </template>

    <el-card v-else-if="!loading" shadow="never">
      <el-empty :description="t('findingDetail.notFound')" />
    </el-card>

    <el-dialog v-model="logVisible" :title="t('findingDetail.logTitle')" width="720px">
      <div v-loading="logLoading">
        <el-alert
          type="info"
          show-icon
          :closable="false"
          :title="t('findingDetail.logRefsNote')"
          class="finding-detail__log-note"
        />
        <CodeBlock :code="logText || t('findingDetail.logEmpty')" />
      </div>
      <template #footer>
        <el-button type="primary" @click="logVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.finding-detail__tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.finding-detail__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.finding-detail__gates-summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.finding-detail__fingerprint {
  word-break: break-all;
}

.finding-detail__title {
  margin: 0 0 8px;
  font-weight: 600;
}

.finding-detail__detail {
  margin: 0;
  padding: 12px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.finding-detail__stdout {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.finding-detail__hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.finding-detail__log-note {
  margin-bottom: 12px;
}

.finding-detail__evidence {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.finding-detail__evidence-note {
  margin-bottom: 12px;
}

.finding-detail__evidence-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.finding-detail__evidence-link {
  color: var(--el-color-primary);
  text-decoration: none;
}

.finding-detail__evidence-link:hover {
  text-decoration: underline;
}

.finding-detail__evidence-title {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.finding-detail__event {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.finding-detail__event-transition {
  font-weight: 500;
}

.finding-detail__event-actor,
.finding-detail__event-reason,
.finding-detail__event-run {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.finding-detail__task-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
</style>
