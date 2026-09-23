<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { asList, fetchGitCredential, fetchRepoRunner, fetchRepos, repoPath } from '@/api/agentHub'
import { apiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import { formatDate } from '@/utils/format'

/**
 * Git access preview (`/repos/git-preview`) — **preview**, not a stable page.
 *
 * It answers the two questions the git story raises in practice, using only
 * the existing REST surface:
 *
 *   1. *What do I hand to `git`?*  `/repos/<slug>/git-credential` exchanges the
 *      platform credential (`repo:push`) for a short-lived Forgejo ticket.  The
 *      returned `password` is a plaintext token, so it is held in memory only,
 *      never logged, never put in a URL and never persisted.
 *   2. *How is this repository executed?*  `/repos/<slug>/runner` is the
 *      logical per-repo runner: credential source, workspace, concurrency and
 *      the (declared-only) egress policy.
 *
 * Two facts this page states deliberately, because the code made them
 * misleading before the review: the minted ticket is **account-wide** (Forgejo
 * scopes are not repository-scoped), and `egress_policy` is **persisted but not
 * enforced** by the platform — network segmentation is a deployment duty.
 */
interface Repo {
  id?: number
  slug: string
  kind?: string | null
  default_branch?: string | null
  forgejo_repo?: string | null
  sync_state?: string | null
  partial?: boolean | null
}

interface Runner {
  id?: number | null
  name?: string
  enabled?: boolean
  max_concurrency?: number
  workspace_subdir?: string
  egress_policy?: string
  egress_allowlist?: string | null
  credential_kind?: string
  credential_username?: string | null
  has_credential?: boolean
  credential_expires_at?: string | null
  credential_rotated_at?: string | null
  last_task_at?: string | null
}

interface Ticket {
  slug?: string
  clone_url?: string
  username?: string
  password?: string
  expires_at?: string | null
  read_only?: boolean
  kind?: string
}

const { t } = useI18n()
const session = useSessionStore()

const repos = ref<Repo[]>([])
const selected = ref('')
const runner = ref<Runner | null>(null)
const loading = ref(true)
const runnerLoading = ref(false)
const ticket = ref<Ticket | null>(null)
const minting = ref(false)

/** `repo:push` is a global point; without it the mint answers 403. */
const canPush = computed(() => session.can('repo:push'))
const currentRepo = computed(() => repos.value.find((repo) => repo.slug === selected.value) ?? null)

/** The runner's effective settings as label/value pairs for the summary table. */
const runnerRows = computed(() => {
  const value = runner.value
  if (value === null) return []
  return [
    { key: 'runner.enabled', text: value.enabled === false ? t('repoGitPreview.runnerDisabled') : t('repoGitPreview.runnerEnabled') },
    { key: 'runner.credential', text: credentialText(value) },
    { key: 'runner.concurrency', text: concurrencyText(value) },
    { key: 'runner.egress', text: value.egress_policy || 'inherit' },
    { key: 'runner.allowlist', text: value.egress_allowlist || t('repoGitPreview.none') },
    { key: 'runner.workspace', text: value.workspace_subdir || t('repoGitPreview.workspaceDefault') },
    { key: 'runner.lastTask', text: value.last_task_at ? formatDate(value.last_task_at) : t('repoGitPreview.never') },
  ]
})

function credentialText(value: Runner): string {
  if (value.credential_kind === 'repo') {
    return value.credential_username
      ? t('repoGitPreview.credentialRepoNamed', { user: value.credential_username })
      : t('repoGitPreview.credentialRepo')
  }
  return t('repoGitPreview.credentialShared')
}

function concurrencyText(value: Runner): string {
  const limit = Number(value.max_concurrency ?? 0)
  return limit > 0
    ? t('repoGitPreview.concurrencyExplicit', { count: limit })
    : t('repoGitPreview.concurrencyInherit')
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await fetchRepos({ per_page: 200 })
    repos.value = asList<Repo>(data, ['items', 'repos'])
    if (!repos.value.some((repo) => repo.slug === selected.value)) {
      selected.value = repos.value[0]?.slug ?? ''
    }
    await loadRunner()
  } catch (error) {
    ElMessage.error(apiError(error))
  } finally {
    loading.value = false
  }
}

async function loadRunner(): Promise<void> {
  ticket.value = null
  runner.value = null
  if (!selected.value) return
  runnerLoading.value = true
  try {
    runner.value = await fetchRepoRunner(selected.value)
  } catch (error) {
    ElMessage.error(apiError(error))
  } finally {
    runnerLoading.value = false
  }
}

/**
 * Mint a ticket, keeping it in memory only.
 *
 * Nothing is written to storage or the URL, and the value is dropped again on
 * every repository change (``loadRunner`` clears it) so a stale token cannot be
 * copied by accident.
 */
async function mintTicket(): Promise<void> {
  if (!selected.value) return
  minting.value = true
  try {
    ticket.value = await fetchGitCredential(selected.value)
  } catch (error) {
    ElMessage.error(apiError(error))
  } finally {
    minting.value = false
  }
}

async function copy(text: string | undefined, label: string): Promise<void> {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(t('common.copied'))
  } catch {
    ElMessage.error(t('common.copyFailed', { label }))
  }
}

onMounted(load)
</script>

<template>
  <div class="page repo-git-preview">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('repoGitPreview.title') }}</h1>
        <p class="page__description">{{ t('repoGitPreview.description') }}</p>
        <el-tag class="repo-git-preview__badge" type="warning" effect="dark" size="small">
          {{ t('repoGitPreview.previewBadge') }}
        </el-tag>
      </div>
      <div class="toolbar">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>
    </div>

    <el-alert
      type="warning"
      show-icon
      :closable="false"
      :title="t('repoGitPreview.previewTitle')"
      :description="t('repoGitPreview.previewDesc')"
    />

    <el-card shadow="never">
      <template #header>
        <div class="card-title">{{ t('repoGitPreview.selectTitle') }}</div>
      </template>
      <div class="repo-git-preview__picker">
        <el-select
          v-model="selected"
          class="repo-git-preview__select"
          filterable
          :loading="loading"
          :placeholder="t('repoGitPreview.selectPlaceholder')"
          @change="loadRunner"
        >
          <el-option
            v-for="repo in repos"
            :key="repo.slug"
            :label="repo.slug"
            :value="repo.slug"
          >
            <span>{{ repo.slug }}</span>
            <span class="repo-git-preview__option-kind">{{ repo.kind || 'upstream' }}</span>
          </el-option>
        </el-select>
        <el-tag v-if="currentRepo" effect="plain">{{ currentRepo.kind || 'upstream' }}</el-tag>
        <el-tag v-if="currentRepo?.partial" type="warning" effect="plain">
          {{ t('repoDetail.partialTitle') }}
        </el-tag>
      </div>
      <p v-if="!loading && repos.length === 0" class="repo-git-preview__empty">
        {{ t('repoGitPreview.empty') }}
      </p>
    </el-card>

    <el-card v-if="selected" shadow="never">
      <template #header>
        <div class="card-title">{{ t('repoGitPreview.gitTitle') }}</div>
      </template>

      <el-alert
        type="info"
        show-icon
        :closable="false"
        :title="t('repoGitPreview.gitScopeTitle')"
        :description="t('repoGitPreview.gitScopeDesc')"
      />

      <div class="repo-git-preview__actions">
        <el-button
          type="primary"
          :disabled="!canPush"
          :loading="minting"
          @click="mintTicket"
        >
          <el-icon><Key /></el-icon>
          <span class="btn-label">{{ t('repoGitPreview.mint') }}</span>
        </el-button>
        <span class="repo-git-preview__hint">
          {{ canPush ? t('repoGitPreview.mintHint') : t('repoGitPreview.mintForbidden') }}
        </span>
      </div>

      <el-descriptions v-if="ticket" :column="1" border class="repo-git-preview__ticket">
        <el-descriptions-item :label="t('repoGitPreview.cloneUrl')">
          <div class="repo-git-preview__line">
            <code class="repo-git-preview__code">{{ ticket.clone_url }}</code>
            <el-button link type="primary" @click="copy(ticket?.clone_url, 'clone_url')">
              {{ t('common.copy') }}
            </el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoGitPreview.username')">
          <div class="repo-git-preview__line">
            <code class="repo-git-preview__code">{{ ticket.username }}</code>
            <el-button link type="primary" @click="copy(ticket?.username, 'username')">
              {{ t('common.copy') }}
            </el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoGitPreview.tokenLabel')">
          <div class="repo-git-preview__line">
            <code class="repo-git-preview__code">{{ ticket.password }}</code>
            <el-button link type="primary" @click="copy(ticket?.password, 'password')">
              {{ t('common.copy') }}
            </el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoGitPreview.ticketScope')">
          <el-tag :type="ticket.read_only ? 'info' : 'warning'" effect="plain" size="small">
            {{ ticket.read_only ? t('repoGitPreview.ticketRead') : t('repoGitPreview.ticketWrite') }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item :label="t('repoGitPreview.expiresAt')">
          {{ ticket.expires_at ? formatDate(ticket.expires_at) : t('repoGitPreview.never') }}
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="ticket"
        class="repo-git-preview__note"
        type="warning"
        show-icon
        :closable="false"
        :title="t('repoGitPreview.tokenOnceTitle')"
        :description="t('repoGitPreview.tokenOnceDesc')"
      />

      <template #footer>
        <div class="repo-git-preview__footer">
          <span>{{ t('repoGitPreview.cloneCmd') }}</span>
          <code class="repo-git-preview__code">git clone {{ ticket?.clone_url || '…' }}</code>
          <el-button
            link
            type="primary"
            :disabled="!ticket?.clone_url"
            @click="copy(`git clone ${ticket?.clone_url ?? ''}`, 'clone')"
          >
            {{ t('common.copy') }}
          </el-button>
          <el-button link type="primary" @click="copy(t('repoGitPreview.helperSnippet'), 'helper')">
            {{ t('repoGitPreview.copyHelper') }}
          </el-button>
        </div>
      </template>
    </el-card>

    <el-card v-if="selected" v-loading="runnerLoading" shadow="never">
      <template #header>
        <div class="card-title">{{ t('repoGitPreview.runnerTitle') }}</div>
      </template>

      <el-table :data="runnerRows" size="small" border>
        <el-table-column :label="t('repoGitPreview.runnerField')" prop="key" width="220">
          <template #default="{ row }">{{ t(`repoGitPreview.${row.key}`) }}</template>
        </el-table-column>
        <el-table-column :label="t('repoGitPreview.runnerValue')" prop="text" />
      </el-table>

      <el-alert
        class="repo-git-preview__note"
        type="info"
        show-icon
        :closable="false"
        :title="t('repoGitPreview.egressTitle')"
        :description="t('repoGitPreview.egressDesc')"
      />

      <p class="repo-git-preview__path">
        {{ t('repoGitPreview.pathHint', { slug: repoPath(selected) }) }}
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.repo-git-preview__badge {
  margin-top: 6px;
}

.repo-git-preview__picker {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.repo-git-preview__select {
  width: 420px;
  max-width: 100%;
}

.repo-git-preview__option-kind {
  float: right;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}

.repo-git-preview__actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0 8px;
  flex-wrap: wrap;
}

.repo-git-preview__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.repo-git-preview__ticket {
  margin-top: 12px;
}

.repo-git-preview__line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.repo-git-preview__code {
  font-family: 'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 12px;
  word-break: break-all;
  background: var(--el-fill-color-light);
  padding: 2px 6px;
  border-radius: 4px;
}

.repo-git-preview__note {
  margin-top: 12px;
}

.repo-git-preview__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.repo-git-preview__path {
  margin-top: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.repo-git-preview__empty {
  color: var(--el-text-color-secondary);
}
</style>
