<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import {
  asImportJob,
  asList,
  asTotal,
  createRepo,
  fetchRepos,
  importRepo,
  syncRepo,
} from '@/api/agentHub'
import { apiError } from '@/api/client'
import ImportJobPanel from '@/components/ImportJobPanel.vue'
import TablePager from '@/components/TablePager.vue'
import { useImportJob, type ImportJob } from '@/composables/useImportJob'
import { usePagination } from '@/composables/usePagination'
import { useSessionStore } from '@/stores/session'
import { formatDate } from '@/utils/format'

/**
 * Repo list (`/repos`) — the front door of the agent hub.
 *
 * Two jobs live on this page:
 *   1. show every imported/local repo with its materialised issue and commit
 *      counts, so "was that import worth it?" is answerable at a glance;
 *   2. start an import and then *watch it*, because an import is minutes of
 *      work, not a request.  The form maps onto §8.1's two modes and the
 *      progress panel polls `/api/v1/imports/<job_id>`.
 *
 * `mirror` is the vllm-style context source: read-only, no push, issues are
 * pulled so findings can cite history.  `workspace` is a writable repo the
 * agent may branch and open PRs on.  The choice is a radio group, not a
 * checkbox, because it changes what the repo *is* — see §8.1.
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
  /** Set by §8.2 when the last import stopped at the issue-mirror cap. */
  partial?: boolean | null
  created_at?: string | null
}

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()

const repos = ref<Repo[]>([])
const loading = ref(true)
const query = ref('')
const kindFilter = ref('')
/** Server-reported total — `repos` may hold fewer when the page cap bites. */
const repoTotal = ref(0)

const { job, polling, track } = useImportJob()

const canWrite = computed(() => session.can('repo:write'))

const { page, pageSize, pageSizes, total, rows } = usePagination(repos)

const importVisible = ref(false)
const submitting = ref(false)
const form = reactive({
  /** §8.1's choice: read-only upstream mirror vs. writable local workspace. */
  mode: 'mirror' as 'mirror' | 'workspace',
  source_url: '',
  slug: '',
  default_branch: 'main',
  include_issues: true,
  include_prs: false,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    // S1 pages this endpoint (`per_page` max 200).  A deployment with more repos
    // than one page is unlikely, but the page must not pretend the list is whole
    // — `repoTotal` turns the cut into the alert under the toolbar.
    const data = await fetchRepos({
      q: query.value.trim(),
      kind: kindFilter.value,
      per_page: 200,
    })
    repos.value = asList(data, ['items', 'repos']) as Repo[]
    repoTotal.value = asTotal(data, repos.value.length)
    page.value = 1
  } catch (e) {
    ElMessage.error(apiError(e) || t('repos.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openImport(): void {
  form.mode = 'mirror'
  form.source_url = ''
  form.slug = ''
  form.default_branch = 'main'
  form.include_issues = true
  form.include_prs = false
  importVisible.value = true
}

function validSource(url: string): boolean {
  return /^(https?:\/\/\S+|git@\S+:\S+)$/i.test(url)
}

/**
 * One dialog, two endpoints — because the two modes are genuinely different
 * acts, not two settings of one call:
 *
 *   * `mirror` → `POST /repos/import`.  S1's endpoint takes `mode`
 *     (`code` / `code+issues` / `issues`) rather than §5.3's `include_issues`,
 *     and always creates a `kind="upstream"` repo.  The checkbox maps onto that
 *     enum here; the server rejects `include_issues` silently otherwise.
 *   * `workspace` → `POST /repos`, which registers a local repo ("nothing is
 *     cloned"): §8.1's writable mode, where a source URL has no meaning.
 */
async function submitImport(): Promise<void> {
  submitting.value = true
  try {
    if (form.mode === 'workspace') {
      const slug = form.slug.trim().replace(/^\/+|\/+$/g, '')
      if (!slug || !slug.includes('/')) {
        ElMessage.warning(t('repos.needSlug'))
        return
      }
      const created = await createRepo({
        slug,
        kind: 'workspace',
        default_branch: form.default_branch.trim() || 'main',
      })
      importVisible.value = false
      ElMessage.success(t('repos.workspaceCreated', { slug: created?.slug ?? slug }))
      await load()
      return
    }

    const url = form.source_url.trim()
    if (!url) {
      ElMessage.warning(t('repos.needUrl'))
      return
    }
    if (!validSource(url)) {
      ElMessage.warning(t('repos.invalidUrl'))
      return
    }
    const result = await importRepo({
      source_url: url,
      mode: form.include_issues ? 'code+issues' : 'code',
      include_prs: form.include_prs,
    })
    track(result as ImportJob)
    importVisible.value = false
    ElMessage.success(t('repos.importStarted'))
  } catch (e) {
    ElMessage.error(apiError(e) || t('repos.importFailed'))
  } finally {
    submitting.value = false
  }
}

async function sync(repo: Repo): Promise<void> {
  try {
    const result = await syncRepo(repo.slug)
    track(asImportJob(result) as ImportJob)
    ElMessage.success(t('repos.syncStarted', { slug: repo.slug }))
  } catch (e) {
    ElMessage.error(apiError(e) || t('repos.syncFailed'))
  }
}

function openDetail(repo: Repo): void {
  router.push(repoLink(repo.slug))
}

/** A slug is `owner/name`; each segment is escaped but the slash stays real. */
function repoLink(slug: string): string {
  return `/repos/${String(slug).split('/').map(encodeURIComponent).join('/')}`
}

function kindType(kind: string | null | undefined): 'warning' | 'success' | 'info' {
  if (kind === 'upstream') return 'warning'
  if (kind === 'workspace') return 'success'
  return 'info'
}

function kindLabel(kind: string | null | undefined): string {
  if (kind === 'upstream') return t('repos.kindUpstream')
  if (kind === 'workspace') return t('repos.kindWorkspace')
  return kind || t('common.unknown')
}

function stateType(state: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' {
  switch (state) {
    case 'ready':
      return 'success'
    case 'error':
      return 'danger'
    case 'pending':
      return 'info'
    default:
      return 'warning'
  }
}

function stateLabel(state: string | null | undefined): string {
  const known = ['pending', 'cloning', 'issues', 'indexing', 'ready', 'error']
  const key = String(state ?? '')
  return known.includes(key) ? t(`repos.state.${key}`) : t('repos.state.unknown')
}

onMounted(load)
</script>

<template>
  <div class="page repos-view">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('repos.title') }}</h1>
        <p class="page__description">{{ t('repos.description') }}</p>
      </div>
      <div class="toolbar">
        <el-button v-if="canWrite" type="primary" @click="openImport">
          <el-icon><Download /></el-icon>
          <span class="btn-label">{{ t('repos.importRepo') }}</span>
        </el-button>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <ImportJobPanel v-if="job" :job="job" :polling="polling" />

    <el-card shadow="never">
      <template #header>
        <div class="repos-view__card-header">
          <span class="card-title">
            {{ t('repos.listTitle') }}
            <span class="repos-view__total">{{ t('common.total', { count: repoTotal }) }}</span>
          </span>
          <div class="toolbar">
            <el-input
              v-model="query"
              class="toolbar__search"
              clearable
              :placeholder="t('repos.searchPlaceholder')"
              @keyup.enter="load"
              @clear="load"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
            <el-select v-model="kindFilter" class="toolbar__kind" @change="load">
              <el-option value="" :label="t('repos.kindAll')" />
              <el-option value="upstream" :label="t('repos.kindUpstream')" />
              <el-option value="workspace" :label="t('repos.kindWorkspace')" />
            </el-select>
            <el-button @click="load">
              <el-icon><Search /></el-icon>
              <span class="btn-label">{{ t('common.search') }}</span>
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="repoTotal > repos.length"
        class="repos-view__alert"
        type="info"
        show-icon
        :closable="false"
        :title="t('repos.truncated', { shown: repos.length, total: repoTotal })"
      />

      <el-table
        v-loading="loading"
        :data="rows"
        stripe
        :empty-text="t('repos.empty')"
        @row-click="openDetail"
      >
        <el-table-column :label="t('repos.colSlug')" min-width="260">
          <template #default="{ row }">
            <div class="repo">
              <span class="repo__slug mono">{{ row.slug }}</span>
              <span v-if="row.source_url" class="repo__source">{{ row.source_url }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('repos.colKind')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="kindType(row.kind)" effect="plain">
              {{ kindLabel(row.kind) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('repos.colState')" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="stateType(row.sync_state)" effect="plain">
              {{ stateLabel(row.sync_state) }}
            </el-tag>
            <el-tooltip v-if="row.partial" :content="t('repos.partialTooltip')" placement="top">
              <el-tag class="repo__partial" size="small" type="warning" effect="dark">
                {{ t('repos.partialBadge') }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('repos.colIssues')" width="110" align="right">
          <template #default="{ row }">{{ row.issue_count ?? 0 }}</template>
        </el-table-column>

        <el-table-column :label="t('repos.colCommits')" width="110" align="right">
          <template #default="{ row }">{{ row.commit_count ?? 0 }}</template>
        </el-table-column>

        <el-table-column :label="t('repos.colSyncedAt')" width="160">
          <template #default="{ row }">{{ formatDate(row.synced_at) }}</template>
        </el-table-column>

        <el-table-column :label="t('common.actions')" width="210" align="right">
          <template #default="{ row }">
            <el-button size="small" @click.stop="openDetail(row)">
              <el-icon><View /></el-icon>
              <span class="btn-label">{{ t('common.detail') }}</span>
            </el-button>
            <el-button
              v-if="canWrite"
              size="small"
              type="primary"
              :disabled="polling"
              @click.stop="sync(row)"
            >
              <el-icon><Refresh /></el-icon>
              <span class="btn-label">{{ t('repos.sync') }}</span>
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

    <!-- Import an upstream mirror, or create a local workspace (§8.1) ── -->
    <el-dialog
      v-model="importVisible"
      :title="form.mode === 'mirror' ? t('repos.importTitle') : t('repos.workspaceTitle')"
      width="560px"
    >
      <el-form :model="form" label-position="top" @submit.prevent="submitImport">
        <el-form-item :label="t('repos.mode')">
          <el-radio-group v-model="form.mode" class="repos-view__modes">
            <el-radio value="mirror" class="repos-view__mode">
              <span class="repos-view__mode-title">{{ t('repos.modeMirror') }}</span>
              <span class="repos-view__mode-desc">{{ t('repos.modeMirrorDesc') }}</span>
            </el-radio>
            <el-radio value="workspace" class="repos-view__mode">
              <span class="repos-view__mode-title">{{ t('repos.modeWorkspace') }}</span>
              <span class="repos-view__mode-desc">{{ t('repos.modeWorkspaceDesc') }}</span>
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.mode === 'mirror'">
          <el-form-item :label="t('repos.sourceUrl')" required>
            <el-input
              v-model="form.source_url"
              :placeholder="t('repos.sourceUrlPlaceholder')"
              clearable
              @keyup.enter="submitImport"
            />
            <p class="hint">{{ t('repos.sourceUrlHint') }}</p>
          </el-form-item>

          <el-form-item :label="t('repos.include')">
            <el-checkbox v-model="form.include_issues">{{ t('repos.includeIssues') }}</el-checkbox>
            <el-checkbox v-model="form.include_prs">{{ t('repos.includePrs') }}</el-checkbox>
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item :label="t('repos.workspaceSlug')" required>
            <el-input
              v-model="form.slug"
              :placeholder="t('repos.workspaceSlugPlaceholder')"
              clearable
              @keyup.enter="submitImport"
            />
            <p class="hint">{{ t('repos.workspaceSlugHint') }}</p>
          </el-form-item>
          <el-form-item :label="t('repoDetail.defaultBranch')">
            <el-input v-model="form.default_branch" clearable />
          </el-form-item>
        </template>

        <el-alert
          type="info"
          show-icon
          :closable="false"
          :title="t('repos.importHintTitle')"
          :description="form.mode === 'mirror' ? t('repos.importHint') : t('repos.workspaceHint')"
        />
      </el-form>
      <template #footer>
        <el-button @click="importVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitImport">
          {{ form.mode === 'mirror' ? t('repos.importSubmit') : t('repos.workspaceSubmit') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.repos-view__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.repos-view__total {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.repos-view__alert {
  margin-bottom: 12px;
}

.toolbar__search {
  width: 240px;
}

.toolbar__kind {
  width: 140px;
}

.repo {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.repo__slug {
  font-weight: 500;
}

.repo__source {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.repo__partial {
  margin-left: 6px;
}

.repos-view__modes {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.repos-view__mode {
  height: auto;
  align-items: flex-start;
  margin-right: 0;
}

.repos-view__mode-title {
  display: block;
  font-weight: 500;
}

.repos-view__mode-desc {
  display: block;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: normal;
}

.hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
