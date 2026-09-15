import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Human-readable build stamp, shown in the sidebar footer.  It exists so
// "am I looking at the new bundle or a cached one?" is answerable at a glance —
// pass BUILD_ID to pin it from CI, otherwise it is the build time.
const buildId =
  process.env.BUILD_ID || `${new Date().toISOString().replace('T', ' ').slice(0, 16)}Z`

// Flask serves the built bundle from /static/dist/ and the SPA shell at the
// application root, so `base` (asset URLs) and the router history base differ.
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

  // Emitted straight into the Flask static folder — no copy step needed.
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },

  base: '/static/dist/',

  server: {
    port: 5173,
    // `npm run dev` proxies API and machine-facing endpoints to Flask so the
    // whole app can be developed with HMR on a single origin.
    proxy: {
      '/api': 'http://127.0.0.1:9090',
      '/auth': 'http://127.0.0.1:9090',
      '/health': 'http://127.0.0.1:9090',
      '/simple': 'http://127.0.0.1:9090',
      '/packages': 'http://127.0.0.1:9090',
      '/python-builds': 'http://127.0.0.1:9090',
      '/node-builds': 'http://127.0.0.1:9090',
    },
  },
})
