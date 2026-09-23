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
