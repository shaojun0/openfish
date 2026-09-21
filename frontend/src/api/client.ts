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
      // The console's own deep links are not carried across as `?next=`: the
      // login route reduces that parameter to a closed set of landing keys (see
      // `routes/auth_routes.py`), so for an SPA path it selects nothing — and a
      // constant destination keeps this line a literal rather than a sink.
      window.location.href = '/auth/login'
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
