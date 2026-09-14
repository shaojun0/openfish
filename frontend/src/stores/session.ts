import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchSession, type SessionInfo } from '@/api'
import { setAuthEnabled } from '@/api/client'

/**
 * Current user + permissions, resolved from `/api/v1/session`.
 *
 * The backend answers this endpoint with 200 even when unauthenticated, so the
 * SPA can render a signed-out state without tripping the 401 interceptor.
 */
export const useSessionStore = defineStore('session', () => {
  const info = ref<SessionInfo | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const loaded = computed(() => info.value !== null)
  const authenticated = computed(() => info.value?.authenticated ?? false)
  const user = computed(() => info.value?.user ?? null)
  const displayName = computed(() => info.value?.display_name ?? null)
  const serverName = computed(() => info.value?.server_name ?? 'cpypiserver')
  const authEnabled = computed(() => info.value?.auth_enabled ?? true)
  const isAdmin = computed(() => info.value?.is_admin ?? false)
  const isSuperuser = computed(() => info.value?.is_superuser ?? false)
  const roles = computed(() => info.value?.roles ?? [])
  const permissions = computed(() => new Set(info.value?.permissions ?? []))

  function can(permission: string): boolean {
    return permissions.value.has(permission)
  }

  async function load(force = false): Promise<void> {
    if (loading.value) return
    if (loaded.value && !force) return
    loading.value = true
    error.value = null
    try {
      const data = await fetchSession()
      info.value = data
      setAuthEnabled(data.auth_enabled)
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  return {
    info,
    loading,
    error,
    loaded,
    authenticated,
    user,
    displayName,
    serverName,
    authEnabled,
    isAdmin,
    isSuperuser,
    roles,
    permissions,
    can,
    load,
  }
})
