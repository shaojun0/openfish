import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Human-readable build stamp, shown in the sidebar footer.  It exists so
// "am I looking at the new bundle or a cached one?" is answerable at a glance —
// pass BUILD_ID to pin it from CI, otherwise it is the build time.
const buildId =
  process.env.BUILD_ID || `${new Date().toISOString().replace('T', ' ').slice(0, 16)}Z`

/**
 * The client-visible path prefix this bundle is built for (e.g. `/pypi`),
 * taken from `VITE_BASE_PATH` (docker-compose passes `ROUTE_PREFIX` to it).
 * Normalised once here and injected as `__BASE_PATH__`, so `src/base.ts` is
 * the only module that ever reads it.  Empty = served at the root.
 */
function normaliseBasePath(raw: string | undefined): string {
  const trimmed = (raw ?? '').trim().replace(/^\/+|\/+$/g, '')
  return trimmed ? `/${trimmed}` : ''
}

const basePath = normaliseBasePath(process.env.VITE_BASE_PATH)

// `npm run dev` proxies the backend on one origin.  Under a prefix the browser
// requests `${basePath}/api/...`, so the keys carry it and the rewrite strips it
// back before Flask sees the path — the same contract the edge nginx has.
const backend = 'http://127.0.0.1:9090'
const backendRoutes = [
  '/api', '/auth', '/health', '/simple', '/packages', '/python-builds',
  '/node-builds', '/tools', '/npm', '/docker', '/debian', '/docs', '/certs',
  '/device', '/openapi.json', '/llms.txt',
]
const proxy = Object.fromEntries(
  backendRoutes.map((path) => [
    `${basePath}${path}`,
    { target: backend, rewrite: (url: string) => url.slice(basePath.length) },
  ]),
)

// The built bundle is served under `${basePath}/static/dist/` by whichever
// process owns it — the frontend nginx container in the split deployment, or
// Flask (via FRONTEND_DIST_DIR) when developing without Docker.  `base` fixes
// the asset URLs, so the output can be dropped behind any server that maps
// that prefix onto the dist directory.
export default defineConfig(({ isSsrBuild }) => ({
  plugins: [vue()],

  define: {
    __BUILD_ID__: JSON.stringify(buildId),
    __BASE_PATH__: JSON.stringify(basePath),
  },

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  // Emitted into frontend/dist.  `npm run build` is the only build step the
  // frontend container runs; nothing is written outside this directory.
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,

    // `--ssr` is used only by `npm run smoke`, whose entry lives in
    // `.agent/scripts/frontend/` — outside this package, where Vite's
    // "package.json dependencies are external" heuristic stops applying and
    // Rolldown tries (and fails) to resolve `jsdom` from the entry's own
    // directory.  Externalising every bare specifier keeps `import … from
    // 'jsdom'` in the emitted bundle, which Node then resolves from
    // `frontend/node_modules` — that is why `--outDir .smoke` stays in here.
    ...(isSsrBuild
      ? {
          rolldownOptions: {
            external: (id: string) =>
              !id.startsWith('.') && !id.startsWith('/') && !id.startsWith('@/'),
          },
        }
      : {}),
  },

  base: `${basePath}/static/dist/`,

  server: {
    port: 5173,
    // `npm run dev` proxies API and machine-facing endpoints to Flask so the
    // whole app can be developed with HMR on a single origin.  Start the
    // backend with `cd backend && python app.py` (port 9090).  `proxy` above
    // already carries the prefix and strips it before forwarding.
    proxy,
  },
}))
