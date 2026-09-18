import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import DefaultLayout from '@/layouts/DefaultLayout.vue'
import { useSessionStore } from '@/stores/session'

/**
 * Route ownership
 * ---------------
 * The SPA owns every URL a human opens in a browser.  The machine-facing
 * endpoints (`/simple/`, `/python-builds/`, `/api/v1/*`, `/health`) are served
 * by Flask and deliberately stay out of this table — `pip` and `uv` parse those
 * responses directly and never execute JavaScript.
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: DefaultLayout,
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/views/HomeView.vue'),
        meta: { titleKey: 'nav.home', icon: 'Odometer' },
      },
      {
        path: 'packages',
        name: 'packages',
        component: () => import('@/views/PackagesView.vue'),
        meta: { titleKey: 'nav.packages', icon: 'Box', requiresPermission: 'package:read' },
      },
      {
        path: 'npm',
        name: 'npm',
        component: () => import('@/views/NpmView.vue'),
        meta: { titleKey: 'nav.npm', icon: 'ShoppingBag', requiresPermission: 'npm:read' },
      },
      {
        path: 'tools',
        name: 'tools',
        component: () => import('@/views/ToolsView.vue'),
        meta: { titleKey: 'nav.tools', icon: 'Tools', requiresPermission: 'tool:read' },
      },
      {
        path: 'models',
        name: 'models',
        component: () => import('@/views/ModelsView.vue'),
        meta: { titleKey: 'nav.models', icon: 'Cpu', requiresPermission: 'model:read' },
      },
      {
        path: 'docker',
        name: 'docker',
        component: () => import('@/views/DockerView.vue'),
        meta: { titleKey: 'nav.docker', icon: 'Ship', requiresPermission: 'docker:read' },
      },
      {
        path: 'debian',
        name: 'debian',
        component: () => import('@/views/DebianView.vue'),
        meta: { titleKey: 'nav.debian', icon: 'Monitor', requiresPermission: 'debian:read' },
      },
      {
        // One documentation leaf per ecosystem — the sidebar adds
        // `/documentation/<ecosystem>` to every ecosystem group.  The SPA page
        // lives outside `/docs/*` on purpose: every guarded docs URL needs
        // `doc:read`, so a trailing slash can never flip the permission.
        path: 'documentation/:ecosystem',
        name: 'docs',
        component: () => import('@/views/DocsView.vue'),
        meta: { titleKey: 'nav.doc', icon: 'Document', requiresPermission: 'doc:read' },
      },
      {
        // Agent hub (S5).  The repo list needs `repo:read`; the detail page
        // accepts both `repos/<owner>/<name>` (a real slug) and `repos/<slug>`
        // (a local repo whose slug has no slash).  Two records keep the path
        // params simple — no regex param is needed for either shape.
        path: 'repos',
        name: 'repos',
        component: () => import('@/views/Repos.vue'),
        meta: { titleKey: 'nav.repos', icon: 'Folder', requiresPermission: 'repo:read' },
      },
      {
        path: 'repos/:owner/:name',
        name: 'repo-detail',
        component: () => import('@/views/RepoDetail.vue'),
        meta: { titleKey: 'nav.repos', icon: 'Folder', requiresPermission: 'repo:read' },
      },
      {
        path: 'repos/:slug',
        name: 'repo-detail-flat',
        component: () => import('@/views/RepoDetail.vue'),
        meta: { titleKey: 'nav.repos', icon: 'Folder', requiresPermission: 'repo:read' },
      },
      {
        path: 'findings',
        name: 'findings',
        component: () => import('@/views/Findings.vue'),
        meta: { titleKey: 'nav.findings', icon: 'Warning', requiresPermission: 'finding:read' },
      },
      {
        path: 'findings/:id',
        name: 'finding-detail',
        component: () => import('@/views/FindingDetail.vue'),
        meta: { titleKey: 'nav.findings', icon: 'Warning', requiresPermission: 'finding:read' },
      },
      {
        path: 'api-keys',
        name: 'api-keys',
        component: () => import('@/views/ApiKeysView.vue'),
        meta: { titleKey: 'nav.apiKeys', icon: 'Key', requiresPermission: 'key:list' },
      },
      {
        path: 'admin',
        name: 'admin',
        component: () => import('@/views/AdminView.vue'),
        meta: { titleKey: 'nav.admin', icon: 'DataAnalysis', requiresAdmin: true },
      },
      {
        path: 'access',
        name: 'access',
        component: () => import('@/views/AccessView.vue'),
        meta: { titleKey: 'nav.access', icon: 'Lock', requiresPermission: 'admin:roles' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory('/'),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/**
 * Resolve the session before the first guarded navigation, then keep
 * non-admins out of `/admin` and callers without the required permission out
 * of everything else.  The API enforces the same rules, so this is a UX guard
 * rather than a security boundary.
 */
router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (!session.loaded) {
    await session.load()
  }
  if (to.meta.requiresAdmin && !session.isAdmin) {
    return { name: 'home' }
  }
  const permission = to.meta.requiresPermission
  if (typeof permission === 'string' && !session.can(permission)) {
    return { name: 'home' }
  }
  return true
})

export default router
