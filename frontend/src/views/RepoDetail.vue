<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import {
  asImportJob,
  asList,
  asTotal,
  fetchRepo,
  fetchRepoIssue,
  fetchRepoIssues,
  searchRepoContext,
  syncRepo,
} from '@/api/agentHub'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import ImportJobPanel from '@/components/ImportJobPanel.vue'
import TablePager from '@/components/TablePager.vue'
import { useImportJob, type ImportJob } from '@/composables/useImportJob'
import { useSessionStore } from '@/stores/session'
import { formatDate } from '@/utils/format'

/**
 * Repo detail (`/repos/<owner>/<name>`).
 *
 * The page answers the three questions a mirrored repo has to answer:
 *   * what am I looking at — basic info and the materialised counts;
 *   * how do I get the code — the `git clone` command plus the auth note
 *     (API-key as the HTTP Basic password, §5.2);
 *   * what does history say — the issue browser and the §8.3 context search.
 *
 * Issue bodies are rendered as **escaped text**, never `v-html`: §8.4 treats
 * everything imported from an upstream tracker as untrusted data, and a
 * mirrored issue is exactly the place a prompt-injection payload would sit.
 * The warning above the body says so out loud.
 */
interface Repo {
  id?: number
  slug: string
  source?: string | null
  source_url?: string | null
  default_branch?: string | null
  forgejo_repo?: string | null
  kind?: string | null
  sync_state?: string | null
  synced_at?: string | null
  issue_count?: number | null
  commit_count?: number | null
  partial?: boolean | null
  created_at?: string | null
}

interface IssueComment {
  author?: string | null
  body?: string | null
  created_at?: string | null
}

interface Issue {
  number: number
  is_pull_request?: boolean
  title?: string | null
  body?: string | null
  /** Server-rendered Markdown (`services.markdown`, raw HTML escaped). */
  body_html?: string | null
  state?: string | null
  author?: string | null
  labels?: string[] | string | null
  milestone?: string | null
  url?: string | null
  created_at?: string | null
  updated_at?: string | null
  closed_at?: string | null
  comments?: IssueComment[]
}

/**
 * A §8.3 mixed-search hit — issue, commit or finding.  The field names follow
 * `services.repo_context._ITEM_SCHEMA`, which returns metadata only and never a
 * raw body (the bodies live inside the wrapped `text` block).
 */
interface ContextHit {
  kind?: string | null
  id?: number | null
  number?: number | null
  is_pull_request?: boolean | null
  sha?: string | null
  title?: string | null
  message?: string | null
  state?: string | null
  status?: string | null
  level?: string | null
  severity?: string | null
  rule_id?: string | null
  file_path?: string | null
  symbol?: string | null
  author?: string | null
  labels?: string[] | null
  url?: string | null
  pr_url?: string | null
  score?: number | null
  /** `mentions` / `duplicate_of` / `fixed_by` on a deterministic evidence hit. */
  relation?: string | null
  /** `evidence` (linked through `finding_evidence`) or `keyword`. */
  origin?: string | null
}

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const slug = computed(() => {
  const owner = route.params.owner
  const name = route.params.name
  if (typeof owner === 'string' && typeof name === 'string') return `${owner}/${name}`
  return String(route.params.slug ?? '')
})

const repo = ref<Repo | null>(null)
const loading = ref(true)
const loadError = ref('')

const canWrite = computed(() => session.can('repo:write'))
const { job, polling, track } = useImportJob()

// ── Issues ───────────────────────────────────────────────────────────

const issues = ref<Issue[]>([])
const issuesLoading = ref(false)
const issueTotal = ref(0)
const issuePage = ref(1)
const issuePageSize = ref(20)
const issuePageSizes = [10, 20, 50, 100]
const issueState = ref('')
const issueLabel = ref('')
const issueQuery = ref('')

const issueVisible = ref(false)
const issueLoading = ref(false)
const activeIssue = ref<Issue | null>(null)

// ── Context search (§8.3) ────────────────────────────────────────────

const searchVisible = ref(false)
const searchLoading = ref(false)
const searchQuery = ref('')
const searchResults = ref<ContextHit[]>([])
const searchTruncated = ref(false)
/** Matches the top-k/budget cut dropped — §8.3 says the cut is never silent. */
const searchOmitted = ref(0)

// ── Clone commands (§5.2) ────────────────────────────────────────────

const repoGitPath = computed(() => {
  const full = repo.value?.forgejo_repo || repo.value?.slug || slug.value
  return String(full)
    .split('/')
    .filter(Boolean)
    .map(encodeURIComponent)
    .join('/')
})

const cloneUrl = computed(() => {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return `${origin}/git/${repoGitPath.value}.git`
})

const cloneCommand = computed(() => `git clone ${cloneUrl.value}`)

/**
 * The authenticated form.  The password is the API key (username arbitrary);
 * the placeholder is deliberate — a real key must come from `/api-keys` and
 * never be pasted into a doc or a script.
 */
const cloneAuthCommand = computed(() => {
  const host = typeof window !== 'undefined' ? window.location.host : '<host>'
  return `git clone http://<any-user>:<API-KEY>@${host}/git/${repoGitPath.value}.git`
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    repo.value = (await fetchRepo(slug.value)) as Repo
  } catch (e) {
    repo.value = null
    loadError.value = apiError(e) || t('repoDetail.loadFailed')
  } finally {
    loading.value = false
  }
}

async function loadIssues(): Promise<void> {
  issuesLoading.value = true
  try {
    const data = await fetchRepoIssues(slug.value, {
      state: issueState.value,
      label: issueLabel.value.trim(),
      q: issueQuery.value.trim(),
      page: issuePage.value,
      per_page: issuePageSize.value,
    })
    issues.value = asList(data, ['items', 'issues']) as Issue[]
    issueTotal.value = asTotal(data, issues.value.length)
  } catch (e) {
    issues.value = []
    ElMessage.error(apiError(e) || t('repoDetail.issueLoadFailed'))
  } finally {
    issuesLoading.value = false
  }
}

function applyIssueFilters(): void {
  issuePage.value = 1
  void loadIssues()
}

async function openIssue(row: Issue | number): Promise<void> {
  const number = typeof row === 'number' ? row : row.number
  issueVisible.value = true
  issueLoading.value = true
  activeIssue.value = null
  try {
    activeIssue.value = (await fetchRepoIssue(slug.value, number)) as Issue
  } catch (e) {
    ElMessage.error(apiError(e) || t('repoDetail.issueLoadFailed'))
  } finally {
    issueLoading.value = false
  }
}

async function runContextSearch(): Promise<void> {
  const q = searchQuery.value.trim()
  if (!q) {
    ElMessage.warning(t('repoDetail.contextNeedQuery'))
    return
  }
  searchVisible.value = true
  searchLoading.value = true
  try {
    const data = await searchRepoContext(slug.value, { q, limit: 20 })
    searchResults.value = asList(data, ['items', 'results']) as ContextHit[]
    if (searchResults.value.length === 0) {
      // The contract does not fix the envelope: results may arrive grouped by
      // kind instead of as one flat list.  Accept both.
      const grouped = data as {
        issues?: ContextHit[]
        commits?: ContextHit[]
        findings?: ContextHit[]
      } | null
      const merged: ContextHit[] = []
      const groups: Array<[string, ContextHit[] | undefined]> = [
        ['issue', grouped?.issues],
        ['commit', grouped?.commits],
        ['finding', grouped?.findings],
      ]
      for (const [kind, list] of groups) {
        if (Array.isArray(list)) merged.push(...list.map((hit) => ({ kind, ...hit })))
      }
      searchResults.value = merged
    }
    searchTruncated.value = Boolean(
      (data as { truncated?: boolean } | null)?.truncated,
    )
    const omitted = (data as { omitted?: number } | null)?.omitted
    searchOmitted.value = typeof omitted === 'number' ? omitted : 0
  } catch (e) {
    searchResults.value = []
    searchOmitted.value = 0
    ElMessage.error(apiError(e) || t('repoDetail.contextFailed'))
  } finally {
    searchLoading.value = false
  }
}

async function sync(): Promise<void> {
  try {
    const result = await syncRepo(slug.value)
    track(asImportJob(result) as ImportJob)
    ElMessage.success(t('repos.syncStarted', { slug: slug.value }))
  } catch (e) {
    ElMessage.error(apiError(e) || t('repos.syncFailed'))
  }
}

function openSource(url: string | null | undefined): void {
  if (url) window.open(url, '_blank', 'noopener')
}

function labelsOf(issue: Issue | null): string[] {
  if (!issue) return []
  const raw = issue.labels
  if (Array.isArray(raw)) return raw.map(String)
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) return parsed.map(String)
    } catch {
      return raw.split(',').map((part) => part.trim()).filter(Boolean)
    }
  }
  return []
}

function openIssueInPanel(number: number | null | undefined): void {
  if (number === null || number === undefined) return
  // Close the search drawer first so the issue drawer is not stacked on it.
  searchVisible.value = false
  void openIssue(number)
}

function contextKindLabel(kind: string | null | undefined): string {
  switch (kind) {
    case 'issue':
      return t('repoDetail.contextKindIssue')
    case 'commit':
      return t('repoDetail.contextKindCommit')
    case 'finding':
      return t('repoDetail.contextKindFinding')
    default:
      return kind || t('common.unknown')
  }
}

watch(issuePage, () => void loadIssues())
watch(issuePageSize, () => {
  issuePage.value = 1
  void loadIssues()
})
// A different repo in the same component instance (sidebar navigation) must
// not keep showing the previous repo's issues.
watch(slug, () => {
  void load()
  issuePage.value = 1
  void loadIssues()
})

onMounted(() => {
  void load()
  void loadIssues()
  const wanted = Number(route.query.issue ?? '')
  if (Number.isFinite(wanted) && wanted > 0) void openIssue(wanted)
})
</script>

<template>
  <div class="page repo-detail">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('repoDetail.title', { slug }) }}</h1>
        <p class="page__description">{{ t('repoDetail.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button @click="router.push('/repos')">
          <el-icon><Back /></el-icon>
          <span class="btn-label">{{ t('repoDetail.back') }}</span>
        </el-button>
        <el-button v-if="canWrite" type="primary" @click="sync">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('repos.sync') }}</span>
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
      :title="t('repoDetail.loadFailed')"
      :description="loadError"
    />

    <ImportJobPanel v-if="job" :job="job" :polling="polling" />

    <el-card v-loading="loading" shadow="never">
      <template #header>
        <span class="card-title">{{ t('repoDetail.infoTitle') }}</span>
      </template>
      <el-descriptions v-if="repo" :column="2" border>
        <el-descriptions-item :label="t('repoDetail.slug')">
          <span class="mono">{{ repo.slug }}</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.kind')">
          {{ repo.kind || t('common.unknown') }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.state')">
          {{ repo.sync_state || t('common.unknown') }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.defaultBranch')">
          <span class="mono">{{ repo.default_branch || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.forgejoRepo')">
          <span class="mono">{{ repo.forgejo_repo || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.sourceUrl')">
          <el-link
            v-if="repo.source_url"
            type="primary"
            :href="repo.source_url"
            target="_blank"
            rel="noopener"
          >
            {{ repo.source_url }}
          </el-link>
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.issueCount')">
          {{ repo.issue_count ?? 0 }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.commitCount')">
          {{ repo.commit_count ?? 0 }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.syncedAt')">
          {{ formatDate(repo.synced_at) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoDetail.createdAt')">
          {{ formatDate(repo.created_at) }}
        </el-descriptions-item>
      </el-descriptions>
      <el-empty v-else :description="t('common.empty')" />

      <el-alert
        v-if="repo?.partial"
        class="repo-detail__alert"
        type="warning"
        show-icon
        :closable="false"
        :title="t('repoDetail.partialTitle')"
        :description="t('repoDetail.partialDesc')"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('repoDetail.cloneTitle') }}</span>
      </template>
      <p class="repo-detail__hint">{{ t('repoDetail.cloneDesc') }}</p>
      <CodeBlock :label="t('repoDetail.cloneCommandLabel')" :code="cloneCommand" />
      <el-alert
        class="repo-detail__alert"
        type="warning"
        show-icon
        :closable="false"
        :title="t('repoDetail.authTitle')"
        :description="t('repoDetail.authDesc')"
      />
      <CodeBlock :label="t('repoDetail.cloneAuthLabel')" :code="cloneAuthCommand" />
      <p class="repo-detail__hint">
        {{ t('repoDetail.authNote') }}
        <el-link type="primary" @click="router.push('/api-keys')">
          {{ t('repoDetail.keyLink') }}
        </el-link>
      </p>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="repo-detail__card-header">
          <span class="card-title">{{ t('repoDetail.issuesTitle') }}</span>
          <span class="repo-detail__count">{{ t('common.total', { count: issueTotal }) }}</span>
        </div>
      </template>

      <div class="repo-detail__filters">
        <el-select v-model="issueState" class="repo-detail__state" @change="applyIssueFilters">
          <el-option value="" :label="t('repoDetail.stateAll')" />
          <el-option value="open" :label="t('repoDetail.stateOpen')" />
          <el-option value="closed" :label="t('repoDetail.stateClosed')" />
        </el-select>
        <el-input
          v-model="issueLabel"
          class="repo-detail__label"
          clearable
          :placeholder="t('repoDetail.labelPlaceholder')"
          @keyup.enter="applyIssueFilters"
          @clear="applyIssueFilters"
        />
        <el-input
          v-model="issueQuery"
          class="toolbar__search"
          clearable
          :placeholder="t('repoDetail.issueSearchPlaceholder')"
          @keyup.enter="applyIssueFilters"
          @clear="applyIssueFilters"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button @click="applyIssueFilters">
          <el-icon><Search /></el-icon>
          <span class="btn-label">{{ t('repoDetail.searchIssues') }}</span>
        </el-button>
        <el-button @click="runContextSearch">
          <el-icon><Connection /></el-icon>
          <span class="btn-label">{{ t('repoDetail.contextSearch') }}</span>
        </el-button>
      </div>

      <el-table
        v-loading="issuesLoading"
        :data="issues"
        stripe
        :empty-text="t('repoDetail.issuesEmpty')"
        @row-click="openIssue"
      >
        <el-table-column :label="t('repoDetail.issueNumber')" width="90">
          <template #default="{ row }">
            <span class="mono">#{{ row.number }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('repoDetail.issueTitleCol')" min-width="280">
          <template #default="{ row }">
            <span>{{ row.title || t('common.unknown') }}</span>
            <el-tag v-if="row.is_pull_request" class="repo-detail__pr" size="small" effect="plain">
              PR
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('repoDetail.issueState')" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.state === 'open' ? 'success' : 'info'"
              effect="plain"
            >
              {{ row.state || t('common.unknown') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('repoDetail.issueAuthor')" width="140">
          <template #default="{ row }">{{ row.author || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('repoDetail.issueLabels')" min-width="180">
          <template #default="{ row }">
            <el-tag
              v-for="label in labelsOf(row)"
              :key="label"
              class="repo-detail__label-tag"
              size="small"
              effect="plain"
            >
              {{ label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('repoDetail.issueUpdated')" width="160">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="120" align="right">
          <template #default="{ row }">
            <el-button size="small" @click.stop="openIssue(row)">
              <el-icon><View /></el-icon>
              <span class="btn-label">{{ t('common.detail') }}</span>
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="issuePage"
        v-model:page-size="issuePageSize"
        :page-sizes="issuePageSizes"
        :total="issueTotal"
      />
    </el-card>

    <!-- Issue body + comments.  Body is plain text, never v-html (§8.4). -->
    <el-drawer
      v-model="issueVisible"
      :title="activeIssue ? `#${activeIssue.number} ${activeIssue.title ?? ''}` : t('repoDetail.issueDetailTitle')"
      size="640px"
    >
      <div v-loading="issueLoading" class="repo-detail__issue">
        <template v-if="activeIssue">
          <div class="repo-detail__issue-meta">
            <el-tag size="small" effect="plain">{{ activeIssue.state || t('common.unknown') }}</el-tag>
            <span>{{ activeIssue.author || t('common.unknown') }}</span>
            <span>{{ formatDate(activeIssue.created_at) }}</span>
            <el-link
              v-if="activeIssue.url"
              type="primary"
              :href="activeIssue.url"
              target="_blank"
              rel="noopener"
            >
              {{ t('repoDetail.openSource') }}
            </el-link>
          </div>
          <div class="repo-detail__issue-labels">
            <el-tag
              v-for="label in labelsOf(activeIssue)"
              :key="label"
              size="small"
              effect="plain"
            >
              {{ label }}
            </el-tag>
          </div>

          <el-alert
            type="warning"
            show-icon
            :closable="false"
            :title="t('repoDetail.untrustedNotice')"
          />

          <h3 class="repo-detail__issue-heading">{{ t('repoDetail.issueBody') }}</h3>
          <!-- The body is upstream data, never an instruction (§8.4 / I6).
               `body_html` is rendered server-side by `services.markdown`, which
               runs with `html=False` — raw HTML arrives escaped, the same trust
               level the docs reader relies on.  Raw Markdown is the fallback. -->
          <div v-if="activeIssue.body_html" class="markdown" v-html="activeIssue.body_html" />
          <pre v-else-if="activeIssue.body" class="issue-body">{{ activeIssue.body }}</pre>
          <el-empty v-else :description="t('common.empty')" />

          <h3 class="repo-detail__issue-heading">
            {{ t('repoDetail.issueComments') }} ({{ activeIssue.comments?.length ?? 0 }})
          </h3>
          <div v-if="activeIssue.comments?.length" class="repo-detail__comments">
            <div
              v-for="(comment, index) in activeIssue.comments"
              :key="index"
              class="repo-detail__comment"
            >
              <div class="repo-detail__comment-meta">
                {{ comment.author || t('common.unknown') }} · {{ formatDate(comment.created_at) }}
              </div>
              <pre class="issue-body">{{ comment.body }}</pre>
            </div>
          </div>
          <el-empty v-else :description="t('repoDetail.noComments')" />
        </template>
      </div>
    </el-drawer>

    <!-- §8.3 mixed context search ──────────────────────────────────── -->
    <el-drawer v-model="searchVisible" :title="t('repoDetail.contextTitle')" size="640px">
      <div class="repo-detail__context">
        <el-input
          v-model="searchQuery"
          :placeholder="t('repoDetail.contextPlaceholder')"
          clearable
          @keyup.enter="runContextSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" :loading="searchLoading" @click="runContextSearch">
          {{ t('repoDetail.contextSearch') }}
        </el-button>
      </div>
      <p class="repo-detail__hint">{{ t('repoDetail.contextDesc') }}</p>

      <el-alert
        v-if="searchTruncated"
        type="info"
        show-icon
        :closable="false"
        :title="t('repoDetail.contextTruncated')"
        :description="searchOmitted ? t('repoDetail.contextOmitted', { count: searchOmitted }) : ''"
      />

      <ul v-loading="searchLoading" class="repo-detail__hits">
        <li v-for="(hit, index) in searchResults" :key="index" class="repo-detail__hit">
          <el-tag size="small" effect="plain">{{ contextKindLabel(hit.kind) }}</el-tag>
          <el-tag v-if="hit.origin === 'evidence'" size="small" type="warning" effect="plain">
            {{ t('repoDetail.contextEvidence') }}
          </el-tag>
          <div class="repo-detail__hit-body">
            <span class="repo-detail__hit-title">
              {{ hit.title || hit.message || hit.file_path || t('common.unknown') }}
            </span>
            <span v-if="hit.message && hit.title" class="repo-detail__hit-snippet">
              {{ hit.message }}
            </span>
            <span class="repo-detail__hit-meta mono">
              {{ [hit.file_path, hit.symbol, hit.status || hit.state, hit.relation].filter(Boolean).join(' · ') }}
            </span>
          </div>
          <el-button
            v-if="hit.kind === 'issue' && hit.number"
            size="small"
            @click="openIssueInPanel(hit.number)"
          >
            {{ t('repoDetail.contextOpenIssue', { number: hit.number }) }}
          </el-button>
          <el-button v-else-if="hit.url" size="small" @click="openSource(hit.url)">
            {{ t('repoDetail.openSource') }}
          </el-button>
        </li>
      </ul>
      <el-empty
        v-if="!searchLoading && searchResults.length === 0"
        :description="t('repoDetail.contextEmpty')"
      />
    </el-drawer>
  </div>
</template>

<style scoped>
.repo-detail__alert {
  margin-top: 12px;
}

.repo-detail__hint {
  margin: 8px 0 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.repo-detail__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.repo-detail__count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.repo-detail__filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.repo-detail__state {
  width: 130px;
}

.repo-detail__label {
  width: 180px;
}

.toolbar__search {
  width: 240px;
}

.repo-detail__pr {
  margin-left: 6px;
}

.repo-detail__label-tag {
  margin: 0 4px 2px 0;
}

.repo-detail__issue {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 120px;
}

.repo-detail__issue-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.repo-detail__issue-labels {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.repo-detail__issue-heading {
  margin: 8px 0 0;
  font-size: 14px;
}

.issue-body {
  margin: 0;
  padding: 12px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 420px;
  overflow: auto;
}

.repo-detail__comments {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.repo-detail__comment-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}

.repo-detail__context {
  display: flex;
  align-items: center;
  gap: 8px;
}

.repo-detail__hits {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.repo-detail__hit {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.repo-detail__hit-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.repo-detail__hit-title {
  font-weight: 500;
  word-break: break-word;
}

.repo-detail__hit-snippet,
.repo-detail__hit-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-word;
}
</style>
