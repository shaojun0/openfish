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
  '/': ['stat-grid', 'shortcut__title'],
  '/packages': ['page__title', 'toolbar__search'],
  '/npm': ['page__title', 'card-title'],
  '/docker': ['page__title', 'card-title'],
  '/debian': ['page__title', 'card-title'],
  '/tools': ['page__title', 'toolbar__search'],
  '/models': ['page__title', 'stat-row'],
  '/documentation/npm': ['page__title', 'docs-view'],
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
      'doc:read',
      'doc:upload',
      'docker:download',
      'docker:read',
      'docker:upload',
      'key:create',
      'key:delete',
      'key:list',
      'key:stats',
      'model:read',
      'model:write',
      'npm:read',
      'package:read',
      'package:write',
      'tool:download',
      'tool:read',
      'tool:upload',
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

// ── Build sub-views ─────────────────────────────────────────────────
// The Python and npm pages swap in `BuildCatalogView` only when their dropdown
// is on "builds", a state `renderToString` cannot reach (it never runs
// `onMounted`).  Render the component directly for both mirrors so a template
// error in that sub-element still fails this smoke run.
const { h } = await import('vue')
const { default: BuildCatalogView } = await import('../src/components/BuildCatalogView.vue')
for (const kind of ['python', 'node'] as const) {
  const subApp = createSSRApp({ render: () => h(BuildCatalogView, { kind }) })
  const subPinia = createPinia()
  subApp.use(subPinia)
  setActivePinia(subPinia)
  subApp.use(i18n)
  subApp.use(ElementPlus)
  for (const [name, component] of Object.entries(ElementPlusIcons)) {
    if (name !== 'default') subApp.component(name, component)
  }
  const html = await renderToString(subApp)
  // The setup card is gone (the docs page owns the how-to), so assert on the
  // list card both mirrors still render.
  const missing = ['card-title'].filter((m) => !html.includes(m))
  results.push({
    path: `build:${kind}`,
    route: kind,
    bytes: html.length,
    missing,
    ok: html.length > 400 && missing.length === 0,
  })
}

// ── Documentation editor ────────────────────────────────────────────
// The editor and its asset manager only appear once a document is selected and
// the dialog is open — states the route renders above cannot reach.  Render
// them directly with a fake document so a broken tooltip expression, an
// unresolved icon or a missing i18n key fails this run instead of the user's
// first click.
const { default: DocEditor } = await import('../src/components/DocEditor.vue')
const { default: DocAssetManager } = await import('../src/components/DocAssetManager.vue')
const fakeAsset = {
  name: 'diagram.png',
  size: 2048,
  size_human: '2.0 KB',
  modified: null,
  is_image: true,
  url: '/docs/python/getting-started/assets/diagram.png',
}
const fakeDoc = {
  id: 'getting-started',
  title: 'Getting started',
  filename: 'document.md',
  size: 1234,
  size_human: '1.2 KB',
  modified: '2026-09-15T00:00:00+00:00',
  created: '2026-09-14T00:00:00+00:00',
  asset_count: 1,
  download_url: '/docs/python/getting-started?download=1',
  raw_url: '/docs/python/getting-started',
  assets_url: '/api/v1/docs/python/getting-started/assets',
  content: '# Getting started\n\n![diagram](assets/diagram.png)\n',
  html: '<h1>Getting started</h1>',
  assets: [fakeAsset],
}
{
  const subApp = createSSRApp({
    render: () =>
      h('div', [
        h(DocEditor, { visible: true, ecosystem: 'python', doc: fakeDoc }),
        h(DocAssetManager, {
          ecosystem: 'python',
          docId: 'getting-started',
          assets: [fakeAsset],
        }),
      ]),
  })
  const subPinia = createPinia()
  subApp.use(subPinia)
  setActivePinia(subPinia)
  subApp.use(i18n)
  subApp.use(ElementPlus)
  for (const [name, component] of Object.entries(ElementPlusIcons)) {
    if (name !== 'default') subApp.component(name, component)
  }
  const html = await renderToString(subApp)
  // `el-dialog` renders its body lazily (only after mount), so the editor's
  // toolbar/source are not in SSR output; the asset manager is a plain
  // component and is.  The editor template itself is covered by the build and
  // by the i18n key audit in the repo's checks.
  const missing = ['asset-manager__list'].filter((m) => !html.includes(m))
  results.push({
    path: 'doc:editor',
    route: 'docs',
    bytes: html.length,
    missing,
    ok: html.length > 400 && missing.length === 0,
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
