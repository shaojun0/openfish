import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Human-readable build stamp, shown in the sidebar footer.  It exists so
// "am I looking at the new bundle or a cached one?" is answerable at a glance —
// pass BUILD_ID to pin it from CI, otherwise it is the build time.
const buildId =
  process.env.BUILD_ID || `${new Date().toISOString().replace('T', ' ').slice(0, 16)}Z`

// The built bundle is served under the /static/dist/ URL prefix by whichever
// process owns it — the frontend nginx container in the split deployment, or
// Flask (via FRONTEND_DIST_DIR) when developing without Docker.  `base` fixes
// the asset URLs, so the output can be dropped behind any server that maps
// that prefix onto the dist directory.
export default defineConfig({
  plugins: [vue()],

  define: {
    __BUILD_ID__: JSON.stringify(buildId),
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
  },

  base: '/static/dist/',

  server: {
    port: 5173,
    // `npm run dev` proxies API and machine-facing endpoints to Flask so the
    // whole app can be developed with HMR on a single origin.  Start the
    // backend with `cd backend && python app.py` (port 9090).
    proxy: {
      '/api': 'http://127.0.0.1:9090',
      '/auth': 'http://127.0.0.1:9090',
      '/health': 'http://127.0.0.1:9090',
      '/simple': 'http://127.0.0.1:9090',
      '/packages': 'http://127.0.0.1:9090',
      '/python-builds': 'http://127.0.0.1:9090',
      '/node-builds': 'http://127.0.0.1:9090',
      '/tools': 'http://127.0.0.1:9090',
      '/npm': 'http://127.0.0.1:9090',
      '/docker': 'http://127.0.0.1:9090',
      '/debian': 'http://127.0.0.1:9090',
      '/docs': 'http://127.0.0.1:9090',
      '/certs': 'http://127.0.0.1:9090',
      '/device': 'http://127.0.0.1:9090',
      '/openapi.json': 'http://127.0.0.1:9090',
      '/llms.txt': 'http://127.0.0.1:9090',
    },
  },
})
