<script setup lang="ts">
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { asList, asTotal, decideFinding, fetchFindings, fetchRepos } from '@/api/agentHub'
import { apiError } from '@/api/client'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { useSessionStore } from '@/stores/session'
import { formatDateOnly } from '@/utils/format'

/**
 * Debt board (`/findings`).
 *
 * The board is split by `level`, not by severity, because `level` is the field
 * that changes what a developer is allowed to do (§6):
 *
 *   * `blocking` — the row is visually loud, and **延期/不修 are absent**, not
 *     merely disabled.  §6.2 is explicit: a blocking finding is never silenced
 *     by `wontfix`, and `decide` rejects both actions (I3).  Only *fix* or
 *     *false positive* exist as buttons.
 *   * `debt` — all four decisions are available, and `acknowledge`/`wontfix`
 *     demand an owner and a due date (I2).  The dialog validates that here as
 *     well as server-side, so the rule is visible before a round-trip.
 *
 * An overdue row and a row whose `seen_count` grew are both marked: those are
 * exactly the two conditions §6.2 uses to drag a finding back to `open`, so a
 * board that hid them would hide the reactivation before it happens.
 *
 * The evidence column deliberately shows **no per-row evidence**: fetching it
 * here would be an N+1 (one request per rendered row) and the linked history is
 * one click away, rendered in full on the detail page.  The column is instead
 * an explicit "view evidence" entry that carries the repository slug, so
 * `FindingDetail` can start its evidence request in parallel with the detail
 * request (§13.2).
 */
interface Finding {
  id: number | string
  /** S3 serialises the numeric id; the slug comes from the repo map. */
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
  seen_count?: number | null
  owner?: string | null
  due?: string | null
  pr_url?: string | null
}

type DecisionAction = 'fix' | 'acknowledge' | 'wontfix' | 'false_positive'

const { t } = useI18n()
const session = useSessionStore()

const findings = ref<Finding[]>([])
const loading = ref(true)
/** Server-reported total; `findings` is capped by the API's `limit`. */
const serverTotal = ref(0)
/** `repo_id -> slug`, so a row can name and link its repository. */
const repoSlugs = ref<Record<string, string>>({})

const filterRepo = ref('')
const filterStatus = ref('')
const filterLevel = ref('')
const filterRule = ref('')
const filterOwner = ref('')

const canDecide = computed(() => session.can('finding:decide'))

const blocking = computed(() =>
  findings.value.filter((row) => String(row.level ?? 'debt') === 'blocking'),
)
const debt = computed(() =>
  findings.value.filter((row) => String(row.level ?? 'debt') !== 'blocking'),
)

const showBlocking = computed(() => filterLevel.value !== 'debt')
const showDebt = computed(() => filterLevel.value !== 'blocking')

const {
  page: blockingPage,
  pageSize: blockingPageSize,
  pageSizes: blockingPageSizes,
  total: blockingTotal,
  rows: blockingRows,
} = usePagination(blocking, { pageSize: 10, pageSizes: [10, 20, 50] })

const {
  page: debtPage,
  pageSize: debtPageSize,
  pageSizes: debtPageSizes,
  total: debtTotal,
  rows: debtRows,
} = usePagination(debt, { pageSize: 10, pageSizes: [10, 20, 50] })

const today = dayjs().format('YYYY-MM-DD')

const openCount = computed(
  () => findings.value.filter((row) => row.status === 'open').length,
)
const blockingOpenCount = computed(
  () => blocking.value.filter((row) => row.status === 'open').length,
)
const debtOpenCount = computed(
  () => debt.value.filter((row) => row.status === 'open').length,
)
const overdueCount = computed(() => findings.value.filter(isOverdue).length)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await fetchFindings({
      repo: filterRepo.value.trim(),
      status: filterStatus.value,
      level: filterLevel.value,
      rule: filterRule.value.trim(),
      owner: filterOwner.value.trim(),
    })
    findings.value = asList(data, ['items', 'findings']) as Finding[]
    serverTotal.value = asTotal(data, findings.value.length)
  } catch (e) {
    findings.value = []
    serverTotal.value = 0
    ElMessage.error(apiError(e) || t('findings.loadFailed'))
  } finally {
    loading.value = false
  }
}

/**
 * `repo_id -> slug` for the repo column and for evidence links.  Findings carry
 * only the numeric id (S3 serialises `repo_id`), so without this map a row could
 * not name — let alone link — the repository it belongs to.  Failure is not
 * fatal: rows then fall back to `#<id>`.
 */
async function loadRepoSlugs(): Promise<void> {
  try {
    const data = await fetchRepos({ per_page: 200 })
    const map: Record<string, string> = {}
    for (const repo of asList(data, ['items', 'repos']) as Array<{
      id?: number
      slug?: string
    }>) {
      if (repo.id !== undefined && repo.slug) map[String(repo.id)] = repo.slug
    }
    repoSlugs.value = map
  } catch {
    repoSlugs.value = {}
  }
}

function isOverdue(row: Finding): boolean {
  if (!row.due) return false
  if (!['open', 'acknowledged', 'wontfix'].includes(String(row.status))) return false
  return String(row.due) < today
}

function isRepeated(row: Finding): boolean {
  return Number(row.seen_count ?? 0) > 1
}

function isBlocking(row: Finding | null): boolean {
  return String(row?.level ?? '') === 'blocking'
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

/** Slug for a row: the API may join it in, otherwise the repo map supplies it. */
function repoSlugOf(row: Finding): string {
  if (row.repo_slug) return String(row.repo_slug)
  if (row.repo_id !== undefined && row.repo_id !== null) {
    return repoSlugs.value[String(row.repo_id)] ?? ''
  }
  return ''
}

function repoLabel(row: Finding): string {
  return repoSlugOf(row) || (row.repo_id ? `#${row.repo_id}` : '—')
}

/** SPA path of the row's repository, or '' when the slug is unknown. */
function repoHref(row: Finding): string {
  const slug = repoSlugOf(row)
  if (!slug) return ''
  return `/repos/${slug.split('/').map(encodeURIComponent).join('/')}`
}

/**
 * The detail location.  The slug travels in the query so the detail page can
 * request the finding and its evidence in the same wave — evidence otherwise
 * has to wait for `repo_id -> slug` resolution (§13.2).
 */
function detailRoute(row: Finding): { path: string; query: Record<string, string> } {
  const slug = repoSlugOf(row)
  return { path: `/findings/${row.id}`, query: slug ? { repo: slug } : {} }
}

function statusBadgeType(row: Finding): 'success' | 'warning' | 'danger' | 'info' {
  if (isOverdue(row)) return 'danger'
  if (isRepeated(row)) return 'warning'
  return 'info'
}

function rowClassName({ row }: { row: Finding }): string {
  const classes: string[] = []
  if (isBlocking(row)) classes.push('findings-view__row--blocking')
  if (isOverdue(row)) classes.push('findings-view__row--overdue')
  return classes.join(' ')
}

// ── Decisions ────────────────────────────────────────────────────────

const decisionVisible = ref(false)
const deciding = ref(false)
const active = ref<Finding | null>(null)
const decision = reactive({
  action: 'fix' as DecisionAction,
  owner: '',
  due: '',
  reason: '',
  /** §6.3: a false positive is confirmed by a *second* account. */
  confirmedBy: '',
})

/** Blocking allows exactly two actions — no deferral, no wontfix (I3). */
const availableActions = computed<DecisionAction[]>(() =>
  isBlocking(active.value)
    ? ['fix', 'false_positive']
    : ['fix', 'acknowledge', 'wontfix', 'false_positive'],
)

const needsOwnerDue = computed(
  () => decision.action === 'acknowledge' || decision.action === 'wontfix',
)
const needsReason = computed(
  () => decision.action === 'false_positive' || decision.action === 'wontfix',
)
const needsConfirmer = computed(() => decision.action === 'false_positive')

function actionLabel(action: DecisionAction): string {
  switch (action) {
    case 'fix':
      return t('findings.actionFix')
    case 'acknowledge':
      return t('findings.actionAcknowledge')
    case 'wontfix':
      return t('findings.actionWontfix')
    default:
      return t('findings.actionFalsePositive')
  }
}

function openDecision(row: Finding, action: DecisionAction): void {
  active.value = row
  decision.action = action
  decision.owner = row.owner ?? ''
  decision.due = row.due ?? ''
  decision.reason = ''
  decision.confirmedBy = ''
  decisionVisible.value = true
}

function disablePast(date: Date): boolean {
  return date.getTime() < Date.now() - 24 * 60 * 60 * 1000
}

/** The client-side half of I2/I3 — the server's 409 is the other half. */
function validateDecision(): string | null {
  if (isBlocking(active.value) && needsOwnerDue.value) {
    return t('findings.blockingNoDefer')
  }
  if (needsOwnerDue.value) {
    if (!decision.owner.trim()) return t('findings.needOwner')
    if (!decision.due) return t('findings.needDue')
    if (String(decision.due) <= today) return t('findings.dueMustBeFuture')
  }
  if (needsReason.value && !decision.reason.trim()) return t('findings.needReason')
  if (needsConfirmer.value) {
    if (!decision.confirmedBy.trim()) return t('findings.needConfirmedBy')
    // §6.3 means literally a *second* person; the server 409s the same case.
    if (decision.confirmedBy.trim() === (session.user ?? '')) {
      return t('findings.confirmMustDiffer')
    }
  }
  return null
}

async function submitDecision(): Promise<void> {
  const row = active.value
  if (!row) return
  const problem = validateDecision()
  if (problem) {
    ElMessage.warning(problem)
    return
  }
  deciding.value = true
  try {
    const payload: {
      action: DecisionAction
      owner?: string
      due?: string
      reason?: string
      confirmed_by?: string
    } = { action: decision.action }
    if (needsOwnerDue.value) {
      payload.owner = decision.owner.trim()
      payload.due = decision.due
    }
    if (decision.reason.trim()) payload.reason = decision.reason.trim()
    if (needsConfirmer.value) payload.confirmed_by = decision.confirmedBy.trim()
    await decideFinding(row.id, payload)
    ElMessage.success(t('findings.decided', { rule: row.rule_id ?? '' }))
    decisionVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('findings.decideFailed'))
  } finally {
    deciding.value = false
  }
}

onMounted(() => {
  void load()
  void loadRepoSlugs()
})
</script>

<template>
  <div class="page findings-view">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('findings.title') }}</h1>
        <p class="page__description">{{ t('findings.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <div class="findings-view__stats">
      <el-tag type="danger" effect="dark">{{ t('findings.statBlocking', { count: blockingOpenCount }) }}</el-tag>
      <el-tag type="warning" effect="plain">{{ t('findings.statDebt', { count: debtOpenCount }) }}</el-tag>
      <el-tag type="danger" effect="plain">{{ t('findings.statOverdue', { count: overdueCount }) }}</el-tag>
      <el-tag type="info" effect="plain">{{ t('findings.statOpen', { count: openCount }) }}</el-tag>
      <el-tag type="info" effect="plain">{{ t('common.total', { count: serverTotal }) }}</el-tag>
    </div>

    <el-alert
      v-if="serverTotal > findings.length"
      type="info"
      show-icon
      :closable="false"
      :title="t('findings.truncated', { shown: findings.length, total: serverTotal })"
    />

    <el-card shadow="never">
      <div class="findings-view__filters">
        <el-input
          v-model="filterRepo"
          class="findings-view__repo"
          clearable
          :placeholder="t('findings.repoPlaceholder')"
          @keyup.enter="load"
          @clear="load"
        />
        <el-input
          v-model="filterRule"
          class="findings-view__rule"
          clearable
          :placeholder="t('findings.rulePlaceholder')"
          @keyup.enter="load"
          @clear="load"
        />
        <el-input
          v-model="filterOwner"
          class="findings-view__owner"
          clearable
          :placeholder="t('findings.ownerPlaceholder')"
          @keyup.enter="load"
          @clear="load"
        />
        <el-select v-model="filterLevel" class="findings-view__level" @change="load">
          <el-option value="" :label="t('findings.levelAll')" />
          <el-option value="blocking" :label="t('findings.levelBlocking')" />
          <el-option value="debt" :label="t('findings.levelDebt')" />
        </el-select>
        <el-select v-model="filterStatus" class="findings-view__status" @change="load">
          <el-option value="" :label="t('findings.statusAll')" />
          <el-option value="open" :label="t('findings.status.open')" />
          <el-option value="acknowledged" :label="t('findings.status.acknowledged')" />
          <el-option value="wontfix" :label="t('findings.status.wontfix')" />
          <el-option value="fixed" :label="t('findings.status.fixed')" />
          <el-option value="stale" :label="t('findings.status.stale')" />
        </el-select>
        <el-button @click="load">
          <el-icon><Search /></el-icon>
          <span class="btn-label">{{ t('common.search') }}</span>
        </el-button>
      </div>
    </el-card>

    <!-- Blocking: louder, and no deferral buttons at all. -->
    <el-card v-if="showBlocking" class="findings-view__board findings-view__board--blocking" shadow="never">
      <template #header>
        <div class="findings-view__board-header">
          <span class="card-title">
            <el-tag type="danger" effect="dark">{{ t('findings.levelBlocking') }}</el-tag>
            <span class="findings-view__board-name">{{ t('findings.boardBlockingTitle') }}</span>
          </span>
          <span class="findings-view__board-count">{{ t('common.total', { count: blockingTotal }) }}</span>
        </div>
      </template>

      <el-alert
        class="findings-view__alert"
        type="error"
        show-icon
        :closable="false"
        :title="t('findings.blockingWarning')"
      />

      <el-table
        v-loading="loading"
        :data="blockingRows"
        :row-class-name="rowClassName"
        :empty-text="t('findings.empty')"
      >
        <el-table-column :label="t('findings.colRepo')" min-width="170">
          <template #default="{ row }">
            <router-link v-if="repoHref(row)" class="finding-repo__link" :to="repoHref(row)">
              {{ repoLabel(row) }}
            </router-link>
            <span v-else class="mono">{{ repoLabel(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colRule')" min-width="220">
          <template #default="{ row }">
            <div class="finding-rule">
              <span class="finding-rule__id mono">{{ row.rule_id }}</span>
              <span v-if="row.title" class="finding-rule__title">{{ row.title }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colLocation')" min-width="240">
          <template #default="{ row }">
            <div class="finding-location">
              <span class="mono">{{ row.file_path || '—' }}</span>
              <span class="finding-location__symbol mono">
                {{ row.symbol || '—' }}<template v-if="row.line_hint">:{{ row.line_hint }}</template>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colSeverity')" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ severityLabel(row.severity) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colOwner')" width="130">
          <template #default="{ row }">{{ row.owner || '—' }}</template>
        </el-table-column>

        <el-table-column :label="t('findings.colDue')" width="130">
          <template #default="{ row }">
            <span :class="{ 'finding-due--overdue': isOverdue(row) }">
              {{ row.due ? formatDateOnly(row.due) : '—' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colSeen')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusBadgeType(row)" effect="plain">
              {{ row.seen_count ?? 0 }}
            </el-tag>
            <el-tooltip v-if="isOverdue(row)" :content="t('findings.overdue')" placement="top">
              <el-icon class="finding-warn"><Warning /></el-icon>
            </el-tooltip>
            <el-tooltip
              v-else-if="isRepeated(row)"
              :content="t('findings.repeated', { count: row.seen_count ?? 0 })"
              placement="top"
            >
              <el-icon class="finding-warn"><Bell /></el-icon>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colEvidence')" min-width="200">
          <template #default="{ row }">
            <router-link class="finding-evidence__link" :to="detailRoute(row)">
              {{ t('findings.evidenceDetail') }}
            </router-link>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colStatus')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('common.actions')" width="320" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="$router.push(detailRoute(row))">
              {{ t('findings.actionDetail') }}
            </el-button>
            <template v-if="canDecide">
              <el-button link type="primary" @click="openDecision(row, 'fix')">
                {{ t('findings.actionFix') }}
              </el-button>
              <el-button link type="danger" @click="openDecision(row, 'false_positive')">
                {{ t('findings.actionFalsePositive') }}
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="blockingPage"
        v-model:page-size="blockingPageSize"
        :page-sizes="blockingPageSizes"
        :total="blockingTotal"
      />
    </el-card>

    <!-- Debt: all four decisions, but deferral needs owner + due. -->
    <el-card v-if="showDebt" class="findings-view__board findings-view__board--debt" shadow="never">
      <template #header>
        <div class="findings-view__board-header">
          <span class="card-title">
            <el-tag type="warning" effect="plain">{{ t('findings.levelDebt') }}</el-tag>
            <span class="findings-view__board-name">{{ t('findings.boardDebtTitle') }}</span>
          </span>
          <span class="findings-view__board-count">{{ t('common.total', { count: debtTotal }) }}</span>
        </div>
      </template>
      <p class="findings-view__hint">{{ t('findings.boardDebtDesc') }}</p>

      <el-table
        v-loading="loading"
        :data="debtRows"
        :row-class-name="rowClassName"
        :empty-text="t('findings.empty')"
      >
        <el-table-column :label="t('findings.colRepo')" min-width="170">
          <template #default="{ row }">
            <router-link v-if="repoHref(row)" class="finding-repo__link" :to="repoHref(row)">
              {{ repoLabel(row) }}
            </router-link>
            <span v-else class="mono">{{ repoLabel(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colRule')" min-width="220">
          <template #default="{ row }">
            <div class="finding-rule">
              <span class="finding-rule__id mono">{{ row.rule_id }}</span>
              <span v-if="row.title" class="finding-rule__title">{{ row.title }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colLocation')" min-width="240">
          <template #default="{ row }">
            <div class="finding-location">
              <span class="mono">{{ row.file_path || '—' }}</span>
              <span class="finding-location__symbol mono">
                {{ row.symbol || '—' }}<template v-if="row.line_hint">:{{ row.line_hint }}</template>
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colSeverity')" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ severityLabel(row.severity) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colOwner')" width="130">
          <template #default="{ row }">{{ row.owner || '—' }}</template>
        </el-table-column>

        <el-table-column :label="t('findings.colDue')" width="130">
          <template #default="{ row }">
            <span :class="{ 'finding-due--overdue': isOverdue(row) }">
              {{ row.due ? formatDateOnly(row.due) : '—' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colSeen')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusBadgeType(row)" effect="plain">
              {{ row.seen_count ?? 0 }}
            </el-tag>
            <el-tooltip v-if="isOverdue(row)" :content="t('findings.overdue')" placement="top">
              <el-icon class="finding-warn"><Warning /></el-icon>
            </el-tooltip>
            <el-tooltip
              v-else-if="isRepeated(row)"
              :content="t('findings.repeated', { count: row.seen_count ?? 0 })"
              placement="top"
            >
              <el-icon class="finding-warn"><Bell /></el-icon>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colEvidence')" min-width="200">
          <template #default="{ row }">
            <router-link class="finding-evidence__link" :to="detailRoute(row)">
              {{ t('findings.evidenceDetail') }}
            </router-link>
          </template>
        </el-table-column>

        <el-table-column :label="t('findings.colStatus')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('common.actions')" width="360" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="$router.push(detailRoute(row))">
              {{ t('findings.actionDetail') }}
            </el-button>
            <template v-if="canDecide">
              <el-button link type="primary" @click="openDecision(row, 'fix')">
                {{ t('findings.actionFix') }}
              </el-button>
              <el-button link @click="openDecision(row, 'acknowledge')">
                {{ t('findings.actionAcknowledge') }}
              </el-button>
              <el-button link type="warning" @click="openDecision(row, 'wontfix')">
                {{ t('findings.actionWontfix') }}
              </el-button>
              <el-button link type="danger" @click="openDecision(row, 'false_positive')">
                {{ t('findings.actionFalsePositive') }}
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="debtPage"
        v-model:page-size="debtPageSize"
        :page-sizes="debtPageSizes"
        :total="debtTotal"
      />
    </el-card>

    <!-- Decision dialog ─────────────────────────────────────────────── -->
    <el-dialog
      v-model="decisionVisible"
      :title="t('findings.decisionTitle', { rule: active?.rule_id ?? '' })"
      width="520px"
    >
      <el-alert
        v-if="isBlocking(active)"
        class="findings-view__alert"
        type="error"
        show-icon
        :closable="false"
        :title="t('findings.blockingWarning')"
      />

      <el-form label-position="top" @submit.prevent="submitDecision">
        <el-form-item :label="t('findings.decisionAction')">
          <el-radio-group v-model="decision.action">
            <el-radio v-for="option in availableActions" :key="option" :value="option">
              {{ actionLabel(option) }}
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="needsOwnerDue">
          <el-form-item :label="t('findings.decisionOwner')" required>
            <el-input
              v-model="decision.owner"
              :placeholder="t('findings.decisionOwnerPlaceholder')"
              clearable
            />
          </el-form-item>
          <el-form-item :label="t('findings.decisionDue')" required>
            <el-date-picker
              v-model="decision.due"
              type="date"
              value-format="YYYY-MM-DD"
              :disabled-date="disablePast"
              :placeholder="t('findings.decisionDuePlaceholder')"
            />
          </el-form-item>
        </template>

        <el-form-item v-if="needsConfirmer" :label="t('findings.decisionConfirmedBy')" required>
          <el-input
            v-model="decision.confirmedBy"
            :placeholder="t('findings.decisionConfirmedByPlaceholder')"
            clearable
          />
        </el-form-item>

        <el-form-item :label="t('findings.decisionReason')" :required="needsReason">
          <el-input
            v-model="decision.reason"
            type="textarea"
            :rows="3"
            :placeholder="t('findings.decisionReasonPlaceholder')"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="decisionVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="deciding" @click="submitDecision">
          {{ t('common.confirm') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.findings-view__stats {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.findings-view__filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.findings-view__repo {
  width: 200px;
}

.findings-view__rule {
  width: 200px;
}

.findings-view__owner {
  width: 150px;
}

.findings-view__level,
.findings-view__status {
  width: 140px;
}

.findings-view__board--blocking {
  border-left: 3px solid var(--el-color-danger);
}

.findings-view__board--debt {
  border-left: 3px solid var(--el-color-warning);
}

.findings-view__board-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.findings-view__board-name {
  margin-left: 8px;
  font-weight: 600;
}

.findings-view__board-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.findings-view__alert {
  margin-bottom: 12px;
}

.findings-view__hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.finding-rule {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.finding-repo__link {
  color: var(--el-color-primary);
  text-decoration: none;
  word-break: break-all;
}

.finding-repo__link:hover {
  text-decoration: underline;
}

.finding-rule__id {
  font-weight: 500;
}

.finding-rule__title,
.finding-location__symbol {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.finding-location {
  display: flex;
  flex-direction: column;
  gap: 2px;
  word-break: break-all;
}

.finding-due--overdue {
  color: var(--el-color-danger);
  font-weight: 600;
}

.finding-warn {
  margin-left: 4px;
  vertical-align: middle;
  color: var(--el-color-danger);
}

.finding-evidence__link {
  color: var(--el-color-primary);
  text-decoration: none;
  font-size: 12px;
  word-break: break-all;
}

.finding-evidence__link:hover {
  text-decoration: underline;
}

.findings-view :deep(.findings-view__row--blocking) {
  --el-table-tr-bg-color: var(--el-color-danger-light-9);
  font-weight: 500;
}

.findings-view :deep(.findings-view__row--overdue) td {
  border-bottom-color: var(--el-color-danger-light-5);
}
</style>
