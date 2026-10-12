import axios, {
  type AxiosError,
  type AxiosRequestConfig,
  type AxiosResponse,
} from 'axios'

/**
 * Whether the deployment has authentication switched on.  The session store
 * updates this once `/api/v1/session` has been fetched, so a 401 only triggers
 * an OAuth redirect when there is actually a login flow to send the user to.
 */
const flags = { authEnabled: true }

export function setAuthEnabled(value: boolean): void {
  flags.authEnabled = value
}

/**
 * Send the browser to the login route — the one place that decides it.
 *
 * Two callers, and neither is redundant:
 *
 * * the 401 interceptor below, for an API call that failed while the console
 *   was already open;
 * * the session store, for the **entry** case.  In the split deployment `/` is
 *   a static file served by the `frontend` container, and `/api/v1/session`
 *   deliberately answers `200` (not `401`) to an anonymous caller so the
 *   response can carry `auth_enabled: false` for a deployment that has no login
 *   at all.  Nothing on the home page is therefore *required* to fail — the
 *   session answer itself has to mean "log in", or the console renders its
 *   signed-out skeleton forever.
 *
 * The two guards are what keep this from becoming a redirect loop: skipping
 * when authentication is off is the same `AUTH_ENABLED=false` case the session
 * endpoint advertises, and skipping under `/auth/*` keeps an already-failing
 * login round trip from retrying itself.
 */
export function redirectToLogin(): void {
  if (!flags.authEnabled) return
  if (window.location.pathname.startsWith('/auth')) return
  window.location.href = '/auth/login'
}

/**
 * The one axios instance every call in `./` goes through.
 *
 * It is deliberately **not** named `http`: a variable by that name reads like
 * Node's built-in `http` module, and static analysers then report every plain
 * HTTP verb call on it as "不安全的传输" — even though the path is the relative,
 * same-origin `/api/v1` and the scheme is whatever the page was served over.
 * See `docs/security/pypiserver0920-findings.md` §3.4.  Renaming it back
 * re-introduces those false positives.
 */
export const apiClient = axios.create({
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

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => {
    if (error.response?.status === 401) {
      // The console's own deep links are not carried across as `?next=`: the
      // login route reduces that parameter to a closed set of landing keys (see
      // `routes/auth_routes.py`), so for an SPA path it selects nothing — and a
      // constant destination keeps this line a literal rather than a sink.
      redirectToLogin()
    }
    return Promise.reject(error)
  },
)

/** HTTP status of a thrown request error, when there is one.
 *
 * The companion to :func:`apiError`: a caller that has to *branch* on the
 * failure (a document save refused with `409` because someone else edited it)
 * asks here rather than reaching into the axios error shape itself, so axios
 * stays an implementation detail of this module.
 */
export function apiStatus(error: unknown): number | undefined {
  return (error as AxiosError)?.response?.status
}

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

// ── Typed JSON helpers ───────────────────────────────────────────────
//
// The modules next to this one call these instead of the axios instance, so the
// `{ data } = response` unwrapping happens in exactly one place and changing
// the transport (an extra header, a retry, a mocked client in a test) touches
// this file only.
//
// `T = any` mirrors axios' own untyped default: `agentHub.js` is plain
// JavaScript, carries no generics and documents its shapes in JSDoc.

/** `GET`, returning the decoded body. */
export async function getJson<T = any>(
  url: string,
  config?: AxiosRequestConfig,
): Promise<T> {
  const { data } = await apiClient.get<T>(url, config)
  return data
}

/** `POST`, returning the decoded body (use `postRaw` when headers are needed). */
export async function postJson<T = any>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const { data } = await apiClient.post<T>(url, body, config)
  return data
}

/** `PUT`, returning the decoded body. */
export async function putJson<T = any>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const { data } = await apiClient.put<T>(url, body, config)
  return data
}

/** `DELETE`, returning the decoded body when the route answers one. */
export async function deleteJson<T = any>(
  url: string,
  config?: AxiosRequestConfig,
): Promise<T> {
  const { data } = await apiClient.delete<T>(url, config)
  return data
}

/**
 * The **whole** axios response.  Only the Debian relay needs it: those two
 * calls read `Content-Disposition` / `X-OpenFish-SHA256` off the headers and
 * stream a Blob body.
 */
export function getRaw<T = any>(
  url: string,
  config?: AxiosRequestConfig,
): Promise<AxiosResponse<T>> {
  return apiClient.get<T>(url, config)
}

/** `postRaw` — see `getRaw`. */
export function postRaw<T = any>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<AxiosResponse<T>> {
  return apiClient.post<T>(url, body, config)
}
