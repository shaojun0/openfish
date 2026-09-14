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
