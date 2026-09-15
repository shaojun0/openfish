/**
 * Headless render smoke test.
 *
 * Renders the real SPA route by route in jsdom via `renderToString`, so
 * template and `setup()` errors surface without a browser.  The API is stubbed
 * out — this checks that the UI *renders*, not that the backend answers.
 *
 * Usage (the .vue imports need Vite, and the bundle must stay under `frontend/`
 * so Node can resolve `jsdom` from node_modules):
 *
 *     node ./node_modules/vite/bin/vite.js build --ssr scripts/smoke-render.ts \
 *          --outDir .smoke --emptyOutDir
 *     node .smoke/smoke-render.js
 */

// @ts-nocheck -- this script is intentionally loosely typed; it runs via Vite.
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', {
  url: 'http://127.0.0.1:9091/',
  pretendToBeVisual: true,
})

const win = dom.window
const g = globalThis

/** Console output captured during the run. */
const problems = []

for (const key of [
  'window',
  'document',
  'navigator',
  'location',
  'history',
  'localStorage',
  'sessionStorage',
  'HTMLElement',
  'Element',
  'Node',
  'SVGElement',
  'Event',
  'CustomEvent',
  'KeyboardEvent',
  'MouseEvent',
  'getComputedStyle',
  'requestAnimationFrame',
  'cancelAnimationFrame',
  'MutationObserver',
  'DOMParser',
]) {
  // Several of these are getter-only on modern Node, so plain assignment throws.
  try {
    Object.defineProperty(g, key, { value: win[key], writable: true, configurable: true })
  } catch (e) {
    problems.push(`WARN  could not install global ${key}: ${e}`)
  }
}

// jsdom implements neither of these, and Element Plus expects them to exist.
g.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
g.IntersectionObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof win.matchMedia !== 'function') {
  g.matchMedia = () => ({
    matches: false,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
}

const realError = console.error
const realWarn = console.warn
console.error = (...args) => problems.push('ERROR ' + args.map(String).join(' '))
console.warn = (...args) => problems.push('WARN  ' + args.map(String).join(' '))

// Cut the network off before anything builds an axios instance.
const axios = (await import('axios')).default
axios.defaults.adapter = async () => {
  throw new Error('network disabled by smoke test')
}

const { createSSRApp } = await import('vue')
const { renderToString } = await import('vue/server-renderer')
const { createPinia, setActivePinia } = await import('pinia')
const { default: ElementPlus } = await import('element-plus')
const ElementPlusIcons = await import('@element-plus/icons-vue')
const { default: App } = await import('../src/App.vue')
const { default: router } = await import('../src/router')
const { i18n } = await import('../src/locales')
const { useSessionStore } = await import('../src/stores/session')

/** Marker substrings that must appear in the rendered markup for each route. */
const EXPECTED = {
  '/': ['stat-grid', 'shortcut__title', 'code-block'],
  '/packages': ['page__title', 'toolbar__search'],
  '/npm': ['page__title', 'card-title', 'setup'],
  '/docker': ['page__title', 'card-title', 'setup'],
  '/debian': ['page__title', 'card-title', 'setup'],
  '/tools': ['page__title', 'toolbar__search'],
  '/models': ['page__title', 'stat-row'],
  '/api-keys': ['create-row', 'card-title'],
  '/admin': ['stat-grid', 'card-title'],
  '/access': ['access-view', 'card-title'],
}

const results = []
let app
for (const [path, markers] of Object.entries(EXPECTED)) {
  app = createSSRApp(App)
  const pinia = createPinia()
  app.use(pinia)
  setActivePinia(pinia)

  // Pretend an admin is signed in so the /admin guard lets us through and the
  // views render their populated branch.
  const session = useSessionStore()
  session.info = {
    authenticated: true,
    auth_enabled: true,
    user: 'smoke-admin',
    display_name: 'Smoke Admin',
    role: 'admin',
    roles: ['admin'],
    permissions: [
      'admin:refresh',
      'admin:roles',
      'admin:view',
      'build:download',
      'build:read',
      'build:sha256',
      'debian:download',
      'debian:read',
      'docker:download',
      'docker:read',
      'key:create',
      'key:delete',
      'key:list',
      'key:stats',
      'model:read',
      'npm:read',
      'package:read',
      'package:write',
      'tool:download',
      'tool:read',
    ],
    server_name: 'cpypi-smoke',
    is_admin: true,
    is_superuser: true,
    auth_method: 'basic',
  }

  app.use(router)
  app.use(i18n)
  app.use(ElementPlus)

  // Mirror main.ts — icons are registered globally, so templates can use
  // `<el-icon><Box /></el-icon>`.
  for (const [name, component] of Object.entries(ElementPlusIcons)) {
    if (name !== 'default') app.component(name, component)
  }

  await router.push(path)
  await router.isReady()

  const html = await renderToString(app)
  const missing = markers.filter((m) => !html.includes(m))
  results.push({
    path,
    route: router.currentRoute.value.path,
    bytes: html.length,
    missing,
    ok: html.length > 800 && missing.length === 0,
  })
}

const realProblems = problems.filter(
  (p) =>
    !p.includes('network disabled by smoke test') &&
    // jsdom does not implement scrolling; harmless for a render check.
    !p.includes("Not implemented: Window's scrollTo"),
)

console.log('')
console.log('route        resolved      bytes   result')
console.log('─'.repeat(60))
let failed = 0
for (const r of results) {
  if (!r.ok) failed += 1
  console.log(
    `${r.path.padEnd(12)} ${String(r.route).padEnd(12)} ${String(r.bytes).padStart(7)}   ` +
      (r.ok ? '✅ rendered' : `❌ missing: ${r.missing.join(', ') || 'empty output'}`),
  )
}

// ── Pagination logic ────────────────────────────────────────────────
// `renderToString` never runs `onMounted`, so the route renders above cannot
// load data and exercise `usePagination`.  Assert the composable directly —
// it is the piece every table now depends on for its slicing and sorting.
const { usePagination } = await import('../src/composables/usePagination')
const { ref: vueRef, nextTick } = await import('vue')

const pagerSource = vueRef(
  Array.from({ length: 25 }, (_, i) => ({
    name: `item-${String(i).padStart(2, '0')}`,
    size: 25 - i,
  })),
)
const pager = usePagination(pagerSource, { pageSize: 10 })
const pagerChecks: Array<[string, boolean]> = [
  ['first page holds one page of rows', pager.rows.value.length === 10],
  ['page count is ceil(total / size)', pager.pageCount.value === 3],
  ['total mirrors the source length', pager.total.value === 25],
]

pager.page.value = 3
pagerChecks.push(['the last page holds the remainder', pager.rows.value.length === 5])

pager.page.value = 1
pager.onSortChange({ prop: 'size', order: 'ascending' })
pagerChecks.push(['custom sort orders the whole source', pager.rows.value[0].name === 'item-24'])

pager.pageSize.value = 20
await nextTick()
pagerChecks.push(['a larger page size clamps the current page', pager.page.value === 1])

pagerSource.value = pagerSource.value.slice(0, 3)
await nextTick()
pagerChecks.push([
  'a shrinking source clamps the current page',
  pager.page.value === 1 && pager.total.value === 3,
])

console.log('')
console.log('pagination   result')
console.log('─'.repeat(60))
let pagerFailed = 0
for (const [label, ok] of pagerChecks) {
  if (!ok) pagerFailed += 1
  console.log(`${label.padEnd(42)} ${ok ? '✅' : '❌'}`)
}
failed += pagerFailed

console.log('')
if (realProblems.length) {
  console.log(`❌ ${realProblems.length} console message(s) captured:`)
  for (const p of realProblems.slice(0, 25)) console.log('   ' + p.slice(0, 220))
} else {
  console.log('✅ no Vue warnings, no unresolved components, no runtime errors')
}

console.error = realError
console.warn = realWarn
process.exit(failed || realProblems.length ? 1 : 0)
