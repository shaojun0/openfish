import { http } from './client'

// ── Types ────────────────────────────────────────────────────────────

export interface SessionInfo {
  authenticated: boolean
  auth_enabled: boolean
  user: string | null
  role: string
  permissions: string[]
  server_name: string
  is_admin: boolean
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
