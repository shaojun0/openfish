import { http } from './client'

/**
 * Agent Hub API — the `docs/agent-hub/DEVELOPMENT.md` §5.3 REST surface.
 *
 * This module is deliberately thin, exactly like `./index.ts`: every function
 * hits the shared axios instance (`baseURL: /api/v1`, session cookie, the
 * common 401 redirect) and hands the raw JSON back.  Shaping the response for
 * a table is the view's job; normalising it here would hide a contract change
 * from the page that has to deal with it.
 *
 * The one piece of logic that does live here is URL building for a repo
 * `slug`.  A slug is `"<owner>/<name>"`, so it is escaped **segment by
 * segment** — `encodeURIComponent` on the whole string would turn the slash
 * into `%2F`, which the Flask route would no longer match.
 */

// ── Shared shapes (JSDoc) ───────────────────────────────────────────
//
// Field names mirror the tables in §4.2.  Anything the UI reads from a
// response is declared here once so each view does not invent its own copy.

/**
 * @typedef {Object} Repo
 * @property {number} [id]
 * @property {string} slug                `"<owner>/<name>"`
 * @property {'import'|'local'} [source]
 * @property {string|null} [source_url]
 * @property {string} [default_branch]
 * @property {string|null} [forgejo_repo]
 * @property {'upstream'|'workspace'} [kind]
 * @property {string} [sync_state]        pending|cloning|issues|indexing|ready|error
 * @property {string|null} [synced_at]
 * @property {number} [issue_count]
 * @property {number} [commit_count]
 * @property {boolean} [partial]          last import hit IMPORT_MAX_ISSUES
 * @property {string|null} [created_at]
 */

/**
 * @typedef {Object} ImportJob
 * @property {number|string} id
 * @property {string|null} [repo_slug]
 * @property {string|null} [mode]
 * @property {string} [status]            queued|running|done|error|cancelled
 * @property {string|null} [phase]        migrate|poll|mirror_issues|index_commits|build_search|done
 * @property {number|null} [progress]     0-100
 * @property {number|null} [total]
 * @property {number|null} [done]
 * @property {string|null} [cursor]
 * @property {boolean} [partial]          §8.2 big-repo guard: the issue mirror was capped
 * @property {string|null} [error]
 * @property {string|null} [started_at]
 * @property {string|null} [finished_at]
 */

/**
 * @typedef {Object} RepoIssue
 * @property {number} number              source-system issue number (PRs often negative)
 * @property {boolean} [is_pull_request]
 * @property {string} [title]
 * @property {string} [body]
 * @property {'open'|'closed'|string} [state]
 * @property {string} [author]
 * @property {string[]} [labels]
 * @property {string|null} [milestone]
 * @property {string|null} [url]          permanent link back to the source system
 * @property {string|null} [created_at]
 * @property {string|null} [updated_at]
 * @property {string|null} [closed_at]
 */

/**
 * @typedef {Object} FindingEvidence
 * `GET /findings/<id>` does **not** inline evidence (§13.2); the linked issues
 * come from `fetchFindingEvidence()`, which reads the context-search route
 * keyed by `finding_id`.  The aliases below cover both that shape (`number`,
 * `url`, `title`, `origin`) and the older inline guess.
 * @property {number} [issue_number]
 * @property {number} [number]
 * @property {{number?: number}|null} [issue]
 * @property {string|null} [relation]     mentions|duplicate_of|fixed_by, null on keyword recall
 * @property {'evidence'|'keyword'} [origin]
 * @property {string|null} [url]
 * @property {string|null} [title]
 * @property {'open'|'closed'|string} [state]
 * @property {string|null} [author]
 * @property {number} [chars]             characters this entry occupies in `text`
 */

/**
 * @typedef {Object} Finding
 * @property {number|string} id
 * @property {number} [repo_id]           S3 serialises the numeric id, not a slug
 * @property {string} [repo_slug]         only when the backend joins it in
 * @property {string} [rule_id]
 * @property {'blocking'|'debt'} [level]
 * @property {'critical'|'high'|'medium'|'low'} [severity]
 * @property {'open'|'acknowledged'|'wontfix'|'fixed'|'stale'} [status]
 * @property {string} [file_path]
 * @property {string} [symbol]
 * @property {number|null} [line_hint]
 * @property {string} [title]
 * @property {string} [detail]
 * @property {string} [fingerprint]
 * @property {number} [seen_count]
 * @property {string|null} [owner]
 * @property {string|null} [due]
 * @property {string|null} [decided_by]
 * @property {string|null} [decided_at]
 * @property {string|null} [pr_url]
 * @property {boolean} [reactivated]
 * @property {FindingEvidence[]} [evidence]  fetched separately via `fetchFindingEvidence`
 * @property {string|null} [created_at]
 * @property {string|null} [updated_at]
 */

/**
 * @typedef {Object} AgentTask
 * @property {number|string} id
 * @property {string|null} [repo_slug]
 * @property {'review'|'fix'|'import'|'backfill'|string} [kind]
 * @property {string} [status]            queued|leased|running|done|failed|dead
 * @property {unknown} [payload]
 * @property {number} [attempts]
 * @property {number} [max_attempts]
 * @property {string|null} [result_ref]
 * @property {string|null} [log_ref]
 * @property {string|null} [created_at]
 * @property {string|null} [started_at]
 * @property {string|null} [finished_at]
 */

// ── Helpers ─────────────────────────────────────────────────────────

/**
 * Escape a `"<owner>/<name>"` slug for use inside a path, keeping the slash
 * a real path separator.
 * @param {string} slug
 * @returns {string}
 */
export function repoPath(slug) {
  return String(slug ?? '')
    .split('/')
    .filter((part) => part.length > 0)
    .map((part) => encodeURIComponent(part))
    .join('/')
}

/**
 * Pull a list out of a response that may be a bare array or an envelope
 * (`{items: []}`, `{findings: []}`, …).  The contract does not pin the
 * envelope down, and a view that guessed wrong would render an empty page, so
 * every list view goes through here.
 * @param {unknown} data
 * @param {string[]} keys
 * @returns {unknown[]}
 */
export function asList(data, keys = ['items']) {
  if (Array.isArray(data)) return data
  for (const key of keys) {
    const value = /** @type {Record<string, unknown>|null} */ (data)?.[key]
    if (Array.isArray(value)) return value
  }
  return []
}

/** The envelope's `total`, falling back to the list length. */
export function asTotal(data, fallback = 0) {
  const total = /** @type {Record<string, unknown>|null} */ (data)?.total
  return typeof total === 'number' ? total : fallback
}

/**
 * Normalise whatever a create/import/sync call answers with into an
 * `ImportJob`.  Backends tend to answer either the job itself, `{job: …}` or
 * `{job_id: …}`; the panel must not care which.
 *
 * S1 answers `services.repo_import.job_payload()`, i.e. a flat object keyed
 * `job_id` with the repo slug under `repo` — both aliases are mapped here so
 * every caller can rely on `id`.
 * @param {unknown} data
 * @returns {import('./agentHub.js').ImportJob}
 */
export function asImportJob(data) {
  if (data === null || typeof data !== 'object') return { id: String(data) }
  const record = /** @type {Record<string, unknown>} */ (data)
  let source = record
  for (const key of ['job', 'import_job']) {
    const nested = record[key]
    if (nested && typeof nested === 'object') {
      source = /** @type {Record<string, unknown>} */ (nested)
      break
    }
  }
  const out = { ...source }
  if (out.id === undefined) out.id = out.job_id ?? out.import_job_id
  if (out.repo_slug === undefined && typeof out.repo === 'string') {
    out.repo_slug = out.repo
  }
  return /** @type {any} */ (out)
}

/** Drop empty filter values so the query string carries only real filters. */
function clean(params) {
  const out = {}
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === '') continue
    out[key] = value
  }
  return out
}

// ── Repos (§5.3) ────────────────────────────────────────────────────

/**
 * Repo list.  `q` and `kind` are server-side filters; S1 also pages the result
 * (`page` / `per_page`, first page by default) and answers
 * `{items, total, page, per_page, pages}`.  The page is small, so the console
 * asks for one generous page and pages it in the browser.
 * @param {{q?: string, kind?: string, page?: number, per_page?: number}} [params]
 * @returns {Promise<Repo[]|{items?: Repo[], repos?: Repo[], total?: number}>}
 */
export async function fetchRepos(params = {}) {
  const { data } = await http.get('/repos', { params: clean(params) })
  return data
}

/**
 * Create a local workspace repo (`repo:write`) — §8.1's writable mode.  Nothing
 * is cloned; the Forgejo repo is created by the first sync.  S1 requires
 * `slug` and accepts `kind` (`upstream` | `workspace`), `default_branch` and
 * `description`.
 * @param {{slug?: string, name?: string, default_branch?: string, kind?: string, description?: string}} payload
 */
export async function createRepo(payload) {
  const { data } = await http.post('/repos', payload)
  return data
}

/**
 * Start an import.  §5.3 names the mode `mirror` / `workspace`, but S1's import
 * endpoint takes `code | code+issues | issues` (what to pull) and always creates
 * a `kind="upstream"` repo; a writable `workspace` is created with `createRepo`
 * instead.  The console therefore sends `code+issues` when the user wants the
 * issue history, and books the mirror/workspace choice as §8.1 intended.
 * Answers with the `ImportJob` to poll at `fetchImportJob`.
 * @param {{source_url: string, mode?: 'code'|'code+issues'|'issues', include_prs?: boolean}} payload
 * @returns {Promise<ImportJob>}
 */
export async function importRepo(payload) {
  const { data } = await http.post('/repos/import', payload)
  return asImportJob(data)
}

/**
 * Repo detail plus its materialised counts.
 * @param {string} slug
 * @returns {Promise<Repo>}
 */
export async function fetchRepo(slug) {
  const { data } = await http.get(`/repos/${repoPath(slug)}`)
  return data
}

/**
 * The repository's logical per-repo runner (`repo:read`).
 *
 * Reading is pure: a repository nobody configured answers the platform
 * defaults with `id: null` and no row is created.  The sealed credential is
 * never returned — `has_credential` says only whether one exists.
 * @param {string} slug
 * @returns {Promise<object>}
 */
export async function fetchRepoRunner(slug) {
  const { data } = await http.get(`/repos/${repoPath(slug)}/runner`)
  return data
}

/**
 * Exchange the platform credential for a **Forgejo** git ticket (`repo:push`).
 *
 * The response carries a plaintext `password` (a Forgejo access token).  It is
 * shown once and must never be stored, logged or put in a URL: the caller keeps
 * it in memory for exactly as long as the user needs to copy it.  The ticket is
 * **account-wide** (`read:repository` / `write:repository`), not bound to
 * `slug`; whether a push succeeds is decided by that account's Forgejo ACL.
 * @param {string} slug
 * @returns {Promise<object>}
 */
export async function fetchGitCredential(slug) {
  const { data } = await http.get(`/repos/${repoPath(slug)}/git-credential`)
  return data
}

/**
 * One page of mirrored issues.
 * @param {string} slug
 * @param {{state?: string, label?: string, author?: string, q?: string, page?: number, per_page?: number, since?: string, until?: string}} [params]
 * @returns {Promise<RepoIssue[]|{items?: RepoIssue[], issues?: RepoIssue[], total?: number, page?: number, per_page?: number}>}
 */
export async function fetchRepoIssues(slug, params = {}) {
  const { data } = await http.get(`/repos/${repoPath(slug)}/issues`, {
    params: clean(params),
  })
  return data
}

/**
 * Issue body plus comments, when the issue has been mirrored.
 * @param {string} slug
 * @param {number|string} number
 * @returns {Promise<RepoIssue & {comments?: Array<{author?: string, body?: string, created_at?: string}>}>}
 */
export async function fetchRepoIssue(slug, number) {
  const { data } = await http.get(
    `/repos/${repoPath(slug)}/issues/${encodeURIComponent(String(number))}`,
  )
  return data
}

/**
 * Trigger an incremental sync; answers with the `ImportJob` to poll.
 * @param {string} slug
 * @returns {Promise<ImportJob>}
 */
export async function syncRepo(slug) {
  const { data } = await http.post(`/repos/${repoPath(slug)}/sync`)
  return data
}

/**
 * Import progress — this is the endpoint the progress panel polls.  S1 answers
 * `job_payload()`, which keys the id as `job_id`; it goes through
 * `asImportJob()` so the panel always sees `id`.
 * @param {number|string} jobId
 * @returns {Promise<ImportJob>}
 */
export async function fetchImportJob(jobId) {
  const { data } = await http.get(`/imports/${encodeURIComponent(String(jobId))}`)
  return asImportJob(data)
}

// ── Context search (§8.3) ───────────────────────────────────────────

/**
 * Mixed issue/commit/finding keyword search with a server-side top-k and
 * character budget.  `kind` is an optional `issue|commit|finding` filter.
 * @param {string} slug
 * @param {{q: string, kind?: string, state?: string, label?: string, limit?: number}} params
 * @returns {Promise<{items?: unknown[], results?: unknown[], truncated?: boolean}>}
 */
export async function searchRepoContext(slug, params) {
  const { data } = await http.get(`/repos/${repoPath(slug)}/context/search`, {
    params: clean(params),
  })
  return data
}

/**
 * The historical issues already linked to one finding, plus keyword recall from
 * its anchors.  §13.2's "historical issue evidence" has exactly one HTTP exit:
 * the context-search route with `finding_id` (mutually exclusive with `q`, so a
 * keyword can never be mixed in).  The route answers the same bounded shape as
 * `searchRepoContext`, so it is reused rather than duplicated.
 * @param {string} slug
 * @param {number|string} findingId
 * @param {{limit?: number, offset?: number, budget?: number}} [params]
 * @returns {Promise<{items?: FindingEvidence[], text?: string, truncated?: boolean, omitted?: number, budget_used?: number}>}
 */
export async function fetchFindingEvidence(slug, findingId, params = {}) {
  return searchRepoContext(slug, { ...params, finding_id: findingId })
}

// ── Findings (§5.3, §6) ─────────────────────────────────────────────

/**
 * Debt board query.  All filters are optional and server-side; S3 also accepts
 * `limit` (default 200) / `offset` and answers `{items, total, limit, offset}`.
 * @param {{repo?: string, status?: string, level?: string, rule?: string, owner?: string, q?: string, limit?: number, offset?: number}} [params]
 * @returns {Promise<Finding[]|{items?: Finding[], findings?: Finding[], total?: number}>}
 */
export async function fetchFindings(params = {}) {
  const { data } = await http.get('/findings', { params: clean(params) })
  return data
}

/**
 * One finding: the row plus (contract permitting) `events`, `evidence`,
 * `gates` and the originating `run`.
 * @param {number|string} id
 */
export async function fetchFinding(id) {
  const { data } = await http.get(`/findings/${encodeURIComponent(String(id))}`)
  return data
}

/**
 * Record a human decision.  `wontfix` and `acknowledge` need `owner` + `due`
 * (I2); `blocking` rejects both server-side with
 * `409 finding_decision_invalid` — the UI hides them, this guard is the
 * second line of defence.  `false_positive` needs `confirmed_by`, a second
 * account that is not the actor (§6.3).
 * @param {number|string} id
 * @param {{action: 'fix'|'acknowledge'|'wontfix'|'false_positive', owner?: string, due?: string, reason?: string, confirmed_by?: string}} payload
 */
export async function decideFinding(id, payload) {
  const { data } = await http.post(
    `/findings/${encodeURIComponent(String(id))}/decide`,
    payload,
  )
  return data
}

/**
 * Enqueue a fix task for a finding (`agent:run`).  S3 answers a small receipt,
 * **not** an `AgentTask`: `{finding_id, task_id, kind, queued}`.  `queued:
 * false` means the queue refused it (no runner configured) and the caller must
 * say so instead of pretending a task exists.
 * @param {number|string} id
 * @param {{note?: string, target_branch?: string, autofix?: boolean}} [payload]
 * @returns {Promise<{finding_id: number, task_id: number|null, kind: string, queued: boolean}>}
 */
export async function fixFinding(id, payload = {}) {
  const { data } = await http.post(
    `/findings/${encodeURIComponent(String(id))}/fix`,
    payload,
  )
  return data
}

// ── Agent tasks (§5.3) ──────────────────────────────────────────────

/**
 * @param {{repo?: string, status?: string, kind?: string}} [params]
 * @returns {Promise<AgentTask[]|{items?: AgentTask[], tasks?: AgentTask[], total?: number}>}
 */
export async function fetchAgentTasks(params = {}) {
  const { data } = await http.get('/agent/tasks', { params: clean(params) })
  return data
}

/**
 * Enqueue a task by hand (`agent:run`).
 * @param {{repo: string, kind: 'review'|'fix'|'import'|'backfill', payload?: unknown}} body
 * @returns {Promise<AgentTask>}
 */
export async function createAgentTask(body) {
  const { data } = await http.post('/agent/tasks', body)
  return data
}

/** @param {number|string} id @returns {Promise<AgentTask>} */
export async function retryAgentTask(id) {
  const { data } = await http.post(`/agent/tasks/${encodeURIComponent(String(id))}/retry`)
  return data
}

/** @param {number|string} id @returns {Promise<AgentTask>} */
export async function cancelAgentTask(id) {
  const { data } = await http.post(`/agent/tasks/${encodeURIComponent(String(id))}/cancel`)
  return data
}

/**
 * The task's log **reference**, not its bytes: S4 answers
 * `{task_id, status, result_ref, log_ref}` and the referenced file lives inside
 * the sandbox work directory (retained 24h, §9.2).  Administrative
 * (`agent:admin`), so the caller must gate the button on that permission.
 * @param {number|string} id
 * @returns {Promise<{task_id?: number, status?: string|null, result_ref?: string|null, log_ref?: string|null}>}
 */
export async function fetchAgentTaskLog(id) {
  const { data } = await http.get(`/agent/tasks/${encodeURIComponent(String(id))}/log`)
  return data
}

// ── Review policy (§7) ──────────────────────────────────────────────

/**
 * The cached policy plus `policy_hash` and `policy_source`
 * (`builtin-default` when the repo has no `.agent/review-policy.yml`).
 * @param {string} slug
 */
export async function fetchPolicy(slug) {
  const { data } = await http.get(`/policies/${repoPath(slug)}`)
  return data
}

/**
 * Overwrite the policy (`policy:write`).  The body is the policy document —
 * either the parsed object or `{content: "<yaml>"}`, whichever the backend
 * settles on; the view passes through what it holds.
 * @param {string} slug
 * @param {unknown} policy
 */
export async function savePolicy(slug, policy) {
  const { data } = await http.put(`/policies/${repoPath(slug)}`, policy)
  return data
}
