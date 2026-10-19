/**
 * The client-visible path prefix this bundle was built for — one answer.
 *
 * `__BASE_PATH__` is injected by `define` in `vite.config.ts` from the
 * `VITE_BASE_PATH` build environment variable (docker-compose passes
 * `ROUTE_PREFIX` to it).  Empty (the default) means "served at the root",
 * which is byte-for-byte the pre-existing behaviour.
 *
 * Everything that builds a URL the *browser* will request through the edge
 * proxy has to start here: vite's `base` handles the asset URLs at build time,
 * and the router history / API base / raw `href`s below use the same value.
 * Do not re-derive the prefix at a call site — a second implementation is how
 * the console once linked to `/simple/...` while the deployment lived under
 * `/pypi`.
 */
export const BASE_PATH: string = __BASE_PATH__

/** `…/api/v1` — axios' baseURL; the edge nginx strips it back to `/api/v1`. */
export const API_BASE_URL = `${BASE_PATH}/api/v1`

/** Prefix an absolute same-origin path with the deployment prefix. */
export function appUrl(path: string): string {
  return `${BASE_PATH}${path.startsWith('/') ? path : `/${path}`}`
}

/**
 * The absolute form of :func:`appUrl`, for text a user copies out (a clone URL,
 * a ``twine`` invocation).  ``""`` for the origin in SSR, where there is none.
 */
export function absoluteUrl(path: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return `${origin}${appUrl(path)}`
}
