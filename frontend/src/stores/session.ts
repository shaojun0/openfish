import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchSession, type SessionInfo } from '@/api'
import { redirectToLogin, setAuthEnabled } from '@/api/client'

/**
 * Current user + permissions, resolved from `/api/v1/session`.
 *
 * The backend answers this endpoint with 200 even when unauthenticated, so an
 * anonymous caller gets a body to read instead of an error.  When that body
 * says authentication is on, the store turns it into the login round trip: the
 * shell is a static file in the split deployment, so this answer — not a failed
 * API call — is the only thing that reliably says "nobody is signed in".
 */
export const useSessionStore = defineStore('session', () => {
  const info = ref<SessionInfo | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  // The SPA is a static shell: the first `/api/v1/session` call can reach the
  // edge proxy while the backend worker is still importing (that is what a 16 s
  // first load looks like in the Network panel — the handler itself is fast).
  // Retry a few times so the header does not stay anonymous until a reload.
  const RETRY_DELAY_MS = 3000
  const MAX_RETRIES = 5
  let retries = 0

  // App.vue, the router guard and HomeView all ask for the identity during
  // boot.  They share one request: a second `/api/v1/session` issued while the
  // first is still in flight used to return immediately, so a caller could read
  // an empty permission set (or fire a guarded API) before the identity was
  // known.  Every caller now awaits the same promise.
  let inflight: Promise<void> | null = null

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
    if (inflight) return inflight
    if (loaded.value && !force) return
    const run = (async () => {
      loading.value = true
      error.value = null
      try {
        const data = await fetchSession()
        info.value = data
        retries = 0
        setAuthEnabled(data.auth_enabled)
        // Order matters: `redirectToLogin()` reads the flag set on the line above.
        if (data.auth_enabled && !data.authenticated) {
          redirectToLogin()
        }
      } catch (e) {
        error.value = e instanceof Error ? e.message : String(e)
        if (info.value === null && retries < MAX_RETRIES) {
          retries += 1
          setTimeout(() => {
            void load()
          }, RETRY_DELAY_MS)
        }
      } finally {
        loading.value = false
      }
    })()
    inflight = run
    try {
      await run
    } finally {
      if (inflight === run) inflight = null
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
