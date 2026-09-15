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

export interface ModelRoute {
  name: string
  provider: string
  base_url: string
  model: string
  aliases: string[]
  path: string
  enabled: boolean
  description: string | null
  tags: string[]
}

export interface ModelRoutes {
  source: string
  exists: boolean
  error: string | null
  version?: number | null
  routes: ModelRoute[]
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
// downloading needs `doc:read`; uploading or deleting a Markdown file needs
// `doc:upload`, which only the built-in admin role holds.

/** One Markdown document in an ecosystem's documentation leaf. */
export interface DocEntry {
  /** Filename on disk — the key used to read/download/delete. */
  name: string
  /** The document's first `#` heading, or the filename stem. */
  title: string
  filename: string
  size: number
  size_human: string
  modified: string | null
  /** Raw `.md` download (an attachment), served by Flask. */
  download_url: string
  /** Raw `.md` served inline as `text/markdown`. */
  raw_url: string
}

export interface DocCatalog {
  ecosystem: string
  root: string
  exists: boolean
  url_prefix: string
  doc_count: number
  documents: DocEntry[]
}

/** A document plus its source and server-rendered, HTML-escaped body. */
export interface DocDetail extends DocEntry {
  content: string
  html: string
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

export async function fetchNpmCatalog(): Promise<NpmCatalog> {
  const { data } = await http.get<NpmCatalog>('/npm')
  return data
}

export async function fetchModelRoutes(): Promise<ModelRoutes> {
  const { data } = await http.get<ModelRoutes>('/models')
  return data
}

export async function fetchDockerCatalog(): Promise<DockerCatalog> {
  const { data } = await http.get<DockerCatalog>('/docker')
  return data
}

export async function fetchDebianCatalog(): Promise<DebianCatalog> {
  const { data } = await http.get<DebianCatalog>('/debian')
  return data
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

export async function fetchDoc(ecosystem: string, name: string): Promise<DocDetail> {
  const { data } = await http.get<DocDetail>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(name)}`,
  )
  return data
}

/**
 * Publish (or replace) one Markdown document.  Admin-only: the server enforces
 * `doc:upload`, so a non-admin gets a 403 here.  Passing the same filename
 * again is how an administrator changes a document's content.
 */
export async function uploadDoc(
  ecosystem: string,
  file: File,
  name?: string,
): Promise<DocEntry> {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  // Let the browser set the multipart boundary — do not set Content-Type.
  const { data } = await http.post<DocEntry>(
    `/docs/${encodeURIComponent(ecosystem)}`,
    form,
  )
  return data
}

export async function deleteDoc(ecosystem: string, name: string): Promise<DocEntry> {
  const { data } = await http.delete<DocEntry>(
    `/docs/${encodeURIComponent(ecosystem)}/${encodeURIComponent(name)}`,
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
