import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Flask serves the built bundle from /static/dist/ and the SPA shell at the
// application root, so `base` (asset URLs) and the router history base differ.
export default defineConfig({
  plugins: [vue()],

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
    },
  },
})
