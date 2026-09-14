import axios, { type AxiosError } from 'axios'

/**
 * Whether the deployment has authentication switched on.  The session store
 * updates this once `/api/v1/session` has been fetched, so a 401 only triggers
 * an OAuth redirect when there is actually a login flow to send the user to.
 */
const flags = { authEnabled: true }

export function setAuthEnabled(value: boolean): void {
  flags.authEnabled = value
}

export const http = axios.create({
  baseURL: '/api/v1',
  withCredentials: true,
  timeout: 30_000,
})

/** Shape returned by the Flask error handlers. */
export interface ApiErrorBody {
  error?: string
  message?: string
  detail?: unknown
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status
    if (status === 401 && flags.authEnabled && !window.location.pathname.startsWith('/auth')) {
      const next = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.href = `/auth/login?next=${next}`
    }
    return Promise.reject(error)
  },
)

/** Best-effort human readable message for any thrown value. */
export function apiError(error: unknown): string {
  const err = error as AxiosError<ApiErrorBody>
  return (
    err?.response?.data?.error ||
    err?.response?.data?.message ||
    err?.message ||
    String(error)
  )
}
