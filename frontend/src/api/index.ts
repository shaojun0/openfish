import { http } from './client'

// ── Types ────────────────────────────────────────────────────────────

export interface SessionInfo {
  authenticated: boolean
  auth_enabled: boolean
  user: string | null
  display_name: string | null
  role: string
  roles: string[]
  permissions: string[]
  server_name: string
  is_admin: boolean
  is_superuser: boolean
}

export interface PermissionInfo {
  id: number
  code: string
  /** Human label — administrators may rename it. */
  name: string
  /** Grouping bucket: 'package' | 'build' | 'key' | 'admin' (or null). */
  module: string | null
  description: string | null
  /** Roles holding this point. 0 means no role holds it — usually a typo. */
  role_count: number
  /**
   * In the database but no guard declares it any more — a row a rename or a
   * removed feature left behind. Granting it changes nothing.
   */
  stale: boolean
  /** The code seeds this point to the `authenticated` role. */
  expected_for_authenticated: boolean
  /** The `authenticated` role actually holds it right now. */
  held_by_authenticated: boolean
  /**
   * Seeded for ordinary users but not granted to them yet — what an upgraded
   * deployment looks like after a new point ships. `admin` is topped up
   * automatically, so this is invisible in `role_count`.
   */
  authenticated_pending: boolean
}

export interface RoleInfo {
  id: number
  code: string
  name: string
  description: string | null
  /** Built-in roles cannot be deleted. */
  is_builtin: boolean
  /** Applies to requests that never authenticated. */
  is_anonymous_default: boolean
  /** Handed to every new account automatically. */
  auto_grant: boolean
  permissions: string[]
  user_count: number
}

export interface UserInfo {
  id: number
  provider: string
  /** Stable identity — this is what roles attach to. */
  external_id: string
  display_name: string | null
  email: string | null
  is_active: boolean
  is_superuser: boolean
  roles: string[]
  last_login_at: string | null
  created_at: string | null
}

export interface ApiKey {
  id: string
  name: string
  prefix: string
  created_by: string
  created_at: string
  expires_at: string | null
  is_permanent: boolean
  is_expired: boolean
  last_used: string | null
  download_count: number
  upload_count: number
  stats_detail?: KeyStats
}

export interface CreatedApiKey extends ApiKey {
  /** Raw key — returned exactly once, never recoverable afterwards. */
  key: string
}

export interface KeyStatRow {
  package_name: string
  event_type: 'download' | 'upload'
  count: number
}

export interface KeyStats {
  key_id: string
  total_downloads: number
  total_uploads: number
  per_package: KeyStatRow[]
}

export interface PackageSummary {
  name: string
  file_count: number
  total_size: number
  total_size_human: string
  download_count: number
  upload_count: number
}

export interface AdminStats {
  overview: {
    package_count: number
    file_count: number
    total_storage: number
    total_storage_human: string
    total_keys: number
    active_keys: number
    total_downloads: number
    total_uploads: number
  }
  packages: PackageSummary[]
  keys: Array<{
    id: string
    name: string
    prefix: string
    created_by: string
    download_count: number
    upload_count: number
    last_used: string
    created_at: string
    expires_at: string
    is_expired: boolean
    is_permanent: boolean
  }>
}

export interface HealthInfo {
  status: string
  server: string
  package_count: number
  packages_dir: string
}

// ── Artifact hub: tools / npm / model routing ────────────────────────

export interface ToolEntry {
  name: string
  filename: string
  /** Path relative to the tools root — the download URL's tail. */
  relative_path: string
  download_url: string
  size: number | null
  size_human: string
  sha256: string | null
  modified: string | null
  description: string | null
  tags: string[]
}

export interface ToolCategory {
  /** Directory name, or `root` for files sitting at the tools root. */
  key: string
  /** Overlay display name; null means "use the category key". */
  name: string | null
  description: string | null
  icon: string | null
  tools: ToolEntry[]
}

export interface ToolCatalog {
  root: string
  exists: boolean
  url_prefix: string
  tool_count: number
  categories: ToolCategory[]
}

export interface NpmPackage {
  name: string
  version: string
  filename: string | null
  size: number | null
  size_human: string
  modified: string | null
  /** Null means "metadata only" — no tarball on disk to serve yet. */
  download_url: string | null
  description: string | null
  tags: string[]
}

export interface NpmCatalog {
  root: string
  exists: boolean
  upstream: string
  package_count: number
  packages: NpmPackage[]
}

/**
 * Result of one connectivity probe against a route's URL.
 *
 * The server only checks that the endpoint answers; it never sends an
 * inference request. `status` classifies the answer so the table can colour
 * it: `ok` (2xx/3xx), `auth` (401/403), `method` (405 — the URL exists but
 * does not answer GET), `not_found` (404), `client_error`, `server_error`,
 * `unreachable` (no answer at all).
 */
export interface ModelRouteHealth {
  reachable: boolean
  ok: boolean
  status:
    | 'ok'
    | 'auth'
    | 'method'
    | 'not_found'
    | 'client_error'
    | 'server_error'
    | 'unreachable'
  http_status: number | null
  latency_ms: number | null
  /** The exact URL that was probed. */
  url: string
  error: string | null
  checked_at: string | null
}

export interface ModelRoute {
  name: string
  /** Wire format: `openai` | `mineru` | `anthropic`. */
  provider: string
  /**
   * Model function, orthogonal to the wire format: `chat` | `completion` |
   * `embedding` | `rerank` | `ocr` | `asr` | `tts`. The server fills in the
   * protocol default (`chat`, or `ocr` for `mineru`) when a route omits it.
   */
  kind: string
  base_url: string
  /** Always null — the stored API key is never returned. */
  api_key: null
  has_api_key: boolean
  /** Non-secret hint (last four characters), or null. */
  api_key_hint: string | null
  model: string
  aliases: string[]
  path: string
  enabled: boolean
  description: string | null
  /** The last probe, when one has run. */
  health: ModelRouteHealth | null
}

export interface ModelRoutes {
  source: string
  exists: boolean
  error: string | null
  version?: number | null
  /** The wire formats an administrator may choose. */
  providers: string[]
  /** The model functions an administrator may choose. */
  kinds: string[]
  /** Provider -> endpoint path used when a route omits one. */
  default_paths: Record<string, string>
  routes: ModelRoute[]
}

/** Body of a create/update/probe request. */
export interface ModelRoutePayload {
  name: string
  provider: string
  /** Omit to keep the stored kind (or take the protocol default on create). */
  kind?: string
  base_url: string
  description: string
  /** Omit (or null) to keep the stored key on update; `''` clears it. */
  api_key?: string | null
  model?: string
  aliases?: string[]
  path?: string
  enabled?: boolean
}

/** One entry of a flat artifact catalog (docker images, .deb packages, …). */
export interface FlatArtifact {
  name: string
  version: string
  /** Debian only — empty for docker artifacts. */
  arch: string
  /** `image` | `compose` | `dockerfile` | `deb` | `config` | `file`. */
  kind: string
  filename: string | null
  size: number | null
  size_human: string
  sha256: string | null
  modified: string | null
  /** Null means "metadata only" — no file on disk behind this entry. */
  download_url: string | null
  description: string | null
  tags: string[]
}

export interface DockerCatalog {
  root: string
  exists: boolean
  /** Optional intranet registry advertised in the UI. */
  registry: string
  artifact_count: number
  artifacts: FlatArtifact[]
}

export interface DebianCatalog {
  root: string
  exists: boolean
  /** Optional intranet apt mirror advertised in the UI. */
  mirror: string
  artifact_count: number
  artifacts: FlatArtifact[]
}

// ── Per-ecosystem documentation ──────────────────────────────────────
//
// Each ecosystem group in the sidebar owns a documentation leaf.  Reading and
// downloading needs `doc:read`; creating, editing, deleting a document or
// uploading one of its assets needs `doc:upload`, which only the built-in
// admin role holds.  A document is a folder: its Markdown plus its own
// `assets/` directory.

/** One file inside a document project's `assets/` directory. */
export interface DocAsset {
  name: string
  size: number
  size_human: string
  modified: string | null
  /** Images are served inline; anything else downloads as an attachment. */
  is_image: boolean
  /** Absolute URL that serves the bytes. */
  url: string
}

/** One documentation document (a folder project). */
export interface DocEntry {
  /** Folder id — the key used to read/edit/delete and to address assets. */
  id: string
  /** Display title: the stored title, the first `#` heading, or the id. */
  title: string
  filename: string
  size: number
  size_human: string
  modified: string | null
  created: string | null
  /** Number of files in the document's own `assets/` directory. */
  asset_count: number
  /** Raw `.md` download (an attachment), served by Flask. */
  download_url: string
  /** Raw `.md` served inline as `text/markdown`. */
  raw_url: string
  /** `GET` endpoint that lists the document's assets. */
  assets_url: string
}

export interface DocCatalog {
  ecosystem: string
  root: string
  exists: boolean
  url_prefix: string
  doc_count: number
  documents: DocEntry[]
}

/** A document plus its source, server-rendered body and asset list. */
export interface DocDetail extends DocEntry {
  content: string
  html: string
  assets: DocAsset[]
}

export interface DocsOverview {
  root: string
  url_prefix: string
  ecosystems: Array<{ key: string; exists: boolean; doc_count: number }>
}

// ── Prebuilt interpreter mirrors (CPython / Node.js) ─────────────────
//
// `/api/v1/python-builds` and `/api/v1/node-builds` describe the same thing
// with different filenames, so both pages render the one `BuildCatalogView`
// off this shape.

/** One downloadable archive in a build mirror. */
export interface BuildFile {
  filename: string
  /** The release directory it lives in — a date tag, or `v20.11.0`. */
  release_tag: string
  version: string
  /** Human label the server prepared, e.g. `cpython 3.12.13` / `node 20.11.0`. */
  label: string
  /** Target triple for CPython, `linux-x64` for Node. */
  platform: string
  /** `install_only_stripped` for CPython, the archive format for Node. */
  variant: string
  extension: string
  size: number | null
  size_human: string
  download_url: string
  /** Endpoint that computes the digest on demand. */
  sha256_url: string
  /** Only present once the digest has been computed and cached. */
  sha256: string | null
}

export interface BuildRelease {
  release_tag: string
  file_count: number
  total_size: number
  total_size_human: string
  files: BuildFile[]
}

export interface BuildCatalog {
  kind: 'python' | 'node'
  root: string
  exists: boolean
  url_prefix: string
  /** What a client points its mirror env var at. */
  mirror_url: string
  /** The machine-facing index document (uv's listing, or `index.json`). */
  index_url: string
  env_var: string
  client: string
  release_count: number
  file_count: number
  total_size: number
  total_size_human: string
  releases: BuildRelease[]
}

// ── Endpoints ────────────────────────────────────────────────────────

export async function fetchSession(): Promise<SessionInfo> {
  const { data } = await http.get<SessionInfo>('/session')
  return data
}

export async function fetchKeys(): Promise<ApiKey[]> {
  const { data } = await http.get<ApiKey[]>('/keys')
  return data
}

export async function createKey(payload: {
  name: string
  expires_in_days: number | null
}): Promise<CreatedApiKey> {
  const { data } = await http.post<CreatedApiKey>('/keys', payload)
  return data
}

export async function deleteKey(id: string): Promise<void> {
  await http.delete(`/keys/${encodeURIComponent(id)}`)
}

export async function fetchKeyStats(id: string): Promise<KeyStats> {
  const { data } = await http.get<KeyStats>(`/keys/${encodeURIComponent(id)}/stats`)
  return data
}

export async function fetchPackages(): Promise<PackageSummary[]> {
  const { data } = await http.get<PackageSummary[]>('/packages')
  return data
}

export async function fetchAdminStats(): Promise<AdminStats> {
  const { data } = await http.get<AdminStats>('/admin/stats')
  return data
}

export async function refreshAdminStats(): Promise<void> {
  await http.post('/admin/refresh-stats')
}

export async function fetchHealth(): Promise<HealthInfo> {
  const { data } = await http.get<HealthInfo>('/health')
  return data
}

// ── Artifact hub ─────────────────────────────────────────────────────

export async function fetchToolCatalog(): Promise<ToolCatalog> {
  const { data } = await http.get<ToolCatalog>('/tools')
  return data
}

/**
 * Upload one tool into an optional category directory.  Admin-only
 * (`tool:upload`).  The server validates the name and extension, streams the
 * body to disk and answers with the stored entry in the catalog's own shape.
 */
export async function uploadTool(
  category: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<ToolEntry> {
  const form = new FormData()
  if (category) form.append('category', category)
  form.append('file', file)
  // Let the browser set the multipart boundary — do not set Content-Type.
  const { data } = await http.post<ToolEntry>('/tools', form, {
    onUploadProgress: (event) => {
      if (onProgress && event.total) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    },
  })
  return data
}

export async function fetchNpmCatalog(): Promise<NpmCatalog> {
  const { data } = await http.get<NpmCatalog>('/npm')
  return data
}

export async function fetchModelRoutes(): Promise<ModelRoutes> {
  const { data } = await http.get<ModelRoutes>('/models')
  return data
}

/**
 * Add a route.  Admin-only (`model:write`); the server validates it, writes it
 * to `MODELS_FILE` and probes the URL, returning both.
 */
export async function createModelRoute(
  payload: ModelRoutePayload,
): Promise<{ route: ModelRoute; health: ModelRouteHealth }> {
  const { data } = await http.post<{ route: ModelRoute; health: ModelRouteHealth }>(
    '/models',
    payload,
  )
  return data
}

/** Edit a route by its current name.  Admin-only (`model:write`). */
export async function updateModelRoute(
  name: string,
  payload: ModelRoutePayload,
): Promise<{ route: ModelRoute; health: ModelRouteHealth }> {
  const { data } = await http.put<{ route: ModelRoute; health: ModelRouteHealth }>(
    `/models/${encodeURIComponent(name)}`,
    payload,
  )
  return data
}

/** Remove a route.  Admin-only (`model:write`). */
export async function deleteModelRoute(name: string): Promise<ModelRoute> {
  const { data } = await http.delete<ModelRoute>(`/models/${encodeURIComponent(name)}`)
  return data
}

/** Probe a draft route that has not been saved yet.  Admin-only. */
export async function probeModelRoute(payload: {
  provider: string
  base_url: string
  api_key?: string | null
  path?: string
}): Promise<ModelRouteHealth> {
  const { data } = await http.post<ModelRouteHealth>('/models/probe', payload)
  return data
}

/** Re-probe a saved route and remember the result.  Admin-only. */
export async function checkModelRoute(name: string): Promise<ModelRouteHealth> {
  const { data } = await http.post<ModelRouteHealth>(
    `/models/${encodeURIComponent(name)}/check`,
  )
  return data
}

export async function fetchDockerCatalog(): Promise<DockerCatalog> {
  const { data } = await http.get<DockerCatalog>('/docker')
  return data
}

/**
 * Upload one offline Docker artifact — a `docker save` tarball, a compose file
 * or a Dockerfile snippet.  Admin-only (`docker:upload`); answers with the
 * stored entry in the catalog's own shape.
 */
export async function uploadDockerArtifact(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<FlatArtifact> {
  const form = new FormData()
  form.append('file', file)
  // Let the browser set the multipart boundary — do not set Content-Type.
  const { data } = await http.post<FlatArtifact>('/docker', form, {
    onUploadProgress: (event) => {
      if (onProgress && event.total) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    },
  })
  return data
}

export async function fetchDebianCatalog(): Promise<DebianCatalog> {
  const { data } = await http.get<DebianCatalog>('/debian')
  return data
}

// ── Debian offline relay ─────────────────────────────────────────────
//
// The relay lives in the protocol namespace at `/debian/offline/*`, not under
// `/api/v1`, because it is a machine surface the CLI and `curl` drive as well
// as the console.  Every call below therefore overrides axios' `baseURL` with
// `''` so the request goes to the protocol path.  A plan or bundle build can
// legitimately take minutes (it may walk a whole apt archive), so those calls
// disable the default 30 s timeout.

export interface DebianOfflineBundleInfo {
  filename: string
  size: number
  size_human: string
  modified: string
}

export interface DebianOfflineStatus {
  configured: boolean
  upstream: string
  root: string
  offline_dir: string
  suites: string[]
  components: string[]
  arches: string[]
  recommends: boolean
  max_mb: number
  bundle_count: number
  bundles: DebianOfflineBundleInfo[]
}

/** A downloaded text artifact plus its server-declared digest. */
export interface DebianTextArtifact {
  blob: Blob
  filename: string
  sha256: string
}

export interface DebianPlanOptions {
  /** Space/comma list of package names to target directly; '' means full sync. */
  only?: string
  allowDowngrade?: boolean
  verifyHashes?: boolean
  /** null/undefined = server default; false forces Recommends out. */
  recommends?: boolean | null
}

export interface DebianOfflineBundle {
  filename: string
  download_url: string
  sha256: string
  size: number
  size_human: string
  packages: number
  skipped: number
  total_bytes: number
  total_size_human: string
  plan_sha256: string
  skipped_packages: Array<Record<string, string>>
}

export interface DebianOfflineImport {
  imported: number
  skipped: number
  failed: number
  total_bytes: number
  total_size_human: string
  manifest_sha256: string
  generated: string
  imported_packages: Array<Record<string, string>>
  failed_packages: Array<Record<string, string>>
}

function contentDispositionName(header: unknown, fallback: string): string {
  const value = typeof header === 'string' ? header : ''
  const match = /filename="?([^";]+)"?/.exec(value)
  return match?.[1] || fallback
}

function headerText(header: unknown): string {
  return typeof header === 'string' ? header : ''
}

export async function fetchDebianOfflineStatus(): Promise<DebianOfflineStatus> {
  const { data } = await http.get<DebianOfflineStatus>('/debian/offline', { baseURL: '' })
  return data
}

export async function exportDebianSnapshot(params: {
  suites?: string
  components?: string
  arches?: string
  fresh?: boolean
} = {}): Promise<DebianTextArtifact> {
  const response = await http.get('/debian/offline/snapshot', {
    baseURL: '',
    responseType: 'blob',
    timeout: 0,
    params: {
      ...(params.suites ? { suites: params.suites } : {}),
      ...(params.components ? { components: params.components } : {}),
      ...(params.arches ? { arches: params.arches } : {}),
      ...(params.fresh ? { fresh: '1' } : {}),
    },
  })
  return {
    blob: response.data as Blob,
    filename: contentDispositionName(
      response.headers['content-disposition'],
      'openfish-debian-snapshot.txt',
    ),
    sha256: headerText(response.headers['x-openfish-sha256']),
  }
}

export async function computeDebianPlan(
  snapshot: File,
  options: DebianPlanOptions = {},
): Promise<DebianTextArtifact> {
  const form = new FormData()
  form.append('snapshot', snapshot)
  if (options.only) form.append('only', options.only)
  if (options.allowDowngrade) form.append('allow_downgrade', '1')
  if (options.verifyHashes) form.append('verify_hashes', '1')
  if (options.recommends !== null && options.recommends !== undefined) {
    form.append('recommends', options.recommends ? '1' : '0')
  }
  const response = await http.post('/debian/offline/plan', form, {
    baseURL: '',
    responseType: 'blob',
    timeout: 0,
  })
  return {
    blob: response.data as Blob,
    filename: contentDispositionName(
      response.headers['content-disposition'],
      'openfish-debian-plan.txt',
    ),
    sha256: headerText(response.headers['x-openfish-sha256']),
  }
}

export async function buildDebianBundle(plan: File): Promise<DebianOfflineBundle> {
  const form = new FormData()
  form.append('plan', plan)
  const { data } = await http.post<DebianOfflineBundle>('/debian/offline/bundle', form, {
    baseURL: '',
    timeout: 0,
  })
  return data
}

export async function importDebianBundle(bundle: File): Promise<DebianOfflineImport> {
  const form = new FormData()
  form.append('bundle', bundle)
  const { data } = await http.post<DebianOfflineImport>('/debian/offline/import', form, {
    baseURL: '',
    timeout: 0,
  })
  return data
}

/** Trigger a browser download for a Blob produced by the relay. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

// ── Per-ecosystem documentation ──────────────────────────────────────

export async function fetchDocsOverview(): Promise<DocsOverview> {
  const { data } = await http.get<DocsOverview>('/docs')
  return data
}

export async function fetchDocCatalog(ecosystem: string): Promise<DocCatalog> {
  const { data } = await http.get<DocCatalog>(`/docs/${encodeURIComponent(ecosystem)}`)
  return data
}

export async function fetchDoc(ecosystem: string, docId: string): Promise<DocDetail> {
  const { data } = await http.get<DocDetail>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}`,
  )
  return data
}

/**
 * Create a document project from a title, optionally seeded with a `.md` file.
 *
 * Admin-only.  If the derived folder id already exists the server *replaces*
 * that document's content instead of creating a duplicate, and says so through
 * `replaced`.
 */
export async function createDoc(
  ecosystem: string,
  title: string,
  file?: File | null,
): Promise<{ document: DocEntry; replaced: boolean }> {
  const form = new FormData()
  form.append('title', title)
  if (file) form.append('file', file)
  // Let the browser set the multipart boundary — do not set Content-Type.
  const { data } = await http.post<{ document: DocEntry; replaced: boolean }>(
    `/docs/${encodeURIComponent(ecosystem)}`,
    form,
  )
  return data
}

/** Save the in-browser editor's Markdown.  Admin-only. */
export async function saveDoc(
  ecosystem: string,
  docId: string,
  content: string,
): Promise<DocDetail> {
  const { data } = await http.put<DocDetail>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}`,
    { content },
  )
  return data
}

/** Render unsaved Markdown for the editor's live preview.  Admin-only. */
export async function renderDocPreview(
  ecosystem: string,
  docId: string,
  content: string,
): Promise<string> {
  const { data } = await http.post<{ html: string }>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}/preview`,
    { content },
  )
  return data.html
}

export async function deleteDoc(ecosystem: string, docId: string): Promise<DocEntry> {
  const { data } = await http.delete<DocEntry>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}`,
  )
  return data
}

/** The files that belong to one document project. */
export async function fetchDocAssets(
  ecosystem: string,
  docId: string,
): Promise<DocAsset[]> {
  const { data } = await http.get<{ assets: DocAsset[] }>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}/assets`,
  )
  return data.assets
}

/** Upload one image/file into a document's own `assets/` directory. */
export async function uploadDocAsset(
  ecosystem: string,
  docId: string,
  file: File,
): Promise<DocAsset> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post<DocAsset>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}/assets`,
    form,
  )
  return data
}

export async function deleteDocAsset(
  ecosystem: string,
  docId: string,
  name: string,
): Promise<DocAsset> {
  const { data } = await http.delete<DocAsset>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(docId)}/assets/${encodeURIComponent(name)}`,
  )
  return data
}

/** Prebuilt-interpreter mirror: `python` is CPython/uv, `node` is Node/nvm. */
export async function fetchBuildCatalog(kind: 'python' | 'node'): Promise<BuildCatalog> {
  const { data } = await http.get<BuildCatalog>(`/${kind}-builds`)
  return data
}

// ── Access control ───────────────────────────────────────────────────

export async function fetchPermissions(): Promise<PermissionInfo[]> {
  const { data } = await http.get<PermissionInfo[]>('/admin/permissions')
  return data
}

export async function fetchRoles(): Promise<RoleInfo[]> {
  const { data } = await http.get<RoleInfo[]>('/admin/roles')
  return data
}

export async function createRole(payload: {
  code: string
  name: string
  description: string | null
}): Promise<RoleInfo> {
  const { data } = await http.post<RoleInfo>('/admin/roles', payload)
  return data
}

export async function deleteRole(roleId: number): Promise<void> {
  await http.delete(`/admin/roles/${roleId}`)
}

/** Replaces the role's grants wholesale — anything omitted is revoked. */
export async function setRolePermissions(
  roleId: number,
  permissions: string[],
): Promise<RoleInfo> {
  const { data } = await http.put<RoleInfo>(`/admin/roles/${roleId}/permissions`, {
    permissions,
  })
  return data
}

export async function fetchUsers(limit = 200, offset = 0): Promise<UserInfo[]> {
  const { data } = await http.get<UserInfo[]>('/admin/users', {
    params: { limit, offset },
  })
  return data
}

export async function grantUserRole(
  userId: number,
  role: string,
): Promise<{ user_id: number; role: string; granted: boolean }> {
  const { data } = await http.post(`/admin/users/${userId}/roles`, { role })
  return data
}

export async function revokeUserRole(
  userId: number,
  roleCode: string,
): Promise<{ user_id: number; role: string; revoked: boolean }> {
  const { data } = await http.delete(
    `/admin/users/${userId}/roles/${encodeURIComponent(roleCode)}`,
  )
  return data
}

export async function setUserSuperuser(
  userId: number,
  superuser: boolean,
): Promise<{ user_id: number; is_superuser: boolean }> {
  const { data } = await http.put(`/admin/users/${userId}/superuser`, { superuser })
  return data
}
