<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import { appUrl } from '@/base'
import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const session = useSessionStore()

/**
 * Optional custom brand logo.
 *
 * `docker/branding/` is bind-mounted into the frontend container at
 * `/usr/share/nginx/html/branding/` (see docker-compose.yml), so a deployment
 * can drop `logo-dark.svg` / `logo-light.svg` (or their `.png` twins, or a
 * single theme-neutral `logo.svg` / `logo.png`) there — no image rebuild.
 * There is no manifest endpoint to ask, so the candidates are probed in order
 * and the first 404 falls through to the next one; once the list is exhausted
 * the built-in icon + `SERVER_NAME` render again.  Theme-specific names come
 * first so a light-background logo is never shown on the dark sidebar.
 */
function logoCandidates(isDark: boolean): string[] {
  const theme = isDark ? 'dark' : 'light'
  return [
    appUrl(`/branding/logo-${theme}.svg`),
    appUrl(`/branding/logo-${theme}.png`),
    appUrl('/branding/logo.svg'),
    appUrl('/branding/logo.png'),
  ]
}

const logoSources = computed(() => logoCandidates(appStore.isDark))
const logoIndex = ref(0)
const logoLoaded = ref(false)
const logoSrc = computed(() => logoSources.value[logoIndex.value])

watch(
  () => appStore.isDark,
  () => {
    // A different file per theme: restart the probe instead of reusing the
    // previous theme's result.
    logoIndex.value = 0
    logoLoaded.value = false
  },
)

function onLogoLoad(): void {
  logoLoaded.value = true
}

function onLogoError(): void {
  // 404 / decode failure: try the next candidate; past the list `logoSrc`
  // becomes undefined and the built-in brand renders again.
  logoLoaded.value = false
  logoIndex.value += 1
}

interface NavItem {
  /** Unique menu index: an SPA path, or the href for an external link. */
  index: string
  titleKey: string
  icon: string
  /** Set for machine-facing / static index links — opened in a new tab. */
  href?: string
  /** When set, the item is hidden unless the caller holds this permission. */
  permission?: string
}

interface NavGroup {
  key: string
  titleKey: string
  icon: string
  /** When set, the group is hidden unless the caller holds this permission. */
  permission?: string
  items: NavItem[]
}

/**
 * Sidebar taxonomy
 * ----------------
 * The menu is grouped by ecosystem, and **every entry that belongs to an
 * ecosystem lives inside its group** — the rich SPA page, the machine-facing
 * index elements and the group's own documentation leaf alike:
 *
 *   Python 生态 → 包管理 (SPA) · Python 索引 (/simple/) · Python 构建 · 文档
 *   npm 生态    → npm 目录 (SPA) · npm 静态索引 (/npm/) · Node 构建 · 文档
 *   Docker 生态 → Docker 目录 (SPA) · Docker 静态索引 (/docker/) · 文档
 *   Debian 生态 → Debian 目录 (SPA) · Debian 静态索引 (/debian/) · 文档
 *   工具        → 工具目录 (SPA) · 工具静态索引 (/tools/) · 文档
 *   模型路由    → 模型路由 (SPA) · 路由表 JSON (/api/v1/models) · 文档
 *   系统        → API 密钥 · 系统统计 · 访问控制 · 接口文档
 *
 * "文档" is a **leaf node inside each ecosystem group**, not one shared entry at
 * the top of the menu: each ecosystem owns its own Markdown documentation
 * (`/documentation/<ecosystem>`) and the server stores it under `DOCS_DIR/<ecosystem>/`.
 * Reading needs `doc:read`; uploading to change the content needs `doc:upload`.
 *
 * Items with an `href` are machine-facing (no JavaScript) and open in a new
 * tab; the rest are SPA routes.
 */
const home: NavItem = { index: '/', titleKey: 'nav.home', icon: 'Odometer' }

/** The documentation leaf every ecosystem group carries. */
function docItem(ecosystem: string): NavItem {
  return {
    index: `/documentation/${ecosystem}`,
    titleKey: 'nav.doc',
    icon: 'Document',
    permission: 'doc:read',
  }
}

const groups = computed<NavGroup[]>(() => {
  const groups: NavGroup[] = [
    {
      key: 'group-python',
      titleKey: 'nav.groupPython',
      icon: 'Box',
      permission: 'package:read',
      items: [
        { index: '/packages', titleKey: 'nav.packages', icon: 'Box' },
        { index: '/simple/', href: '/simple/', titleKey: 'nav.index', icon: 'Link' },
        {
          index: '/python-builds/',
          href: '/python-builds/',
          titleKey: 'nav.builds',
          icon: 'Download',
          // Listing CPython builds is `build:read`, which `package:read` does
          // not imply — without this the item shows and then 403s.
          permission: 'build:read',
        },
        docItem('python'),
      ],
    },
    {
      key: 'group-npm',
      titleKey: 'nav.groupNpm',
      icon: 'ShoppingBag',
      permission: 'npm:read',
      items: [
        { index: '/npm', titleKey: 'nav.npm', icon: 'ShoppingBag' },
        { index: '/npm/', href: '/npm/', titleKey: 'nav.npmIndex', icon: 'Link' },
        {
          index: '/node-builds/',
          href: '/node-builds/',
          titleKey: 'nav.nodeBuilds',
          icon: 'Download',
          // `nodebuild:read`, not `npm:read` — see the CPython item above.
          permission: 'nodebuild:read',
        },
        docItem('npm'),
      ],
    },
    {
      key: 'group-docker',
      titleKey: 'nav.groupDocker',
      icon: 'Ship',
      permission: 'docker:read',
      items: [
        { index: '/docker', titleKey: 'nav.docker', icon: 'Ship' },
        { index: '/docker/', href: '/docker/', titleKey: 'nav.dockerIndex', icon: 'Link' },
        docItem('docker'),
      ],
    },
    {
      key: 'group-debian',
      titleKey: 'nav.groupDebian',
      icon: 'Monitor',
      permission: 'debian:read',
      items: [
        { index: '/debian', titleKey: 'nav.debian', icon: 'Monitor' },
        { index: '/debian/', href: '/debian/', titleKey: 'nav.debianIndex', icon: 'Link' },
        docItem('debian'),
      ],
    },
    {
      key: 'group-tools',
      titleKey: 'nav.groupTools',
      icon: 'Tools',
      permission: 'tool:read',
      items: [
        { index: '/tools', titleKey: 'nav.tools', icon: 'Tools' },
        { index: '/tools/', href: '/tools/', titleKey: 'nav.toolsIndex', icon: 'Link' },
        docItem('tools'),
      ],
    },
    {
      key: 'group-models',
      titleKey: 'nav.groupModels',
      icon: 'Cpu',
      permission: 'model:read',
      items: [
        { index: '/models', titleKey: 'nav.models', icon: 'Cpu' },
        {
          index: '/api/v1/models',
          href: '/api/v1/models',
          titleKey: 'nav.modelsJson',
          icon: 'Document',
        },
        docItem('models'),
      ],
    },
    {
      key: 'group-agent',
      titleKey: 'nav.groupAgent',
      icon: 'Collection',
      permission: 'repo:read',
      items: [
        { index: '/repos', titleKey: 'nav.repos', icon: 'Folder' },
        {
          // Preview leaf: the git access story (ticket exchange + the repo's
          // logical runner).  Labelled as a preview so nobody treats it as a
          // settled surface.
          index: '/repos/git-preview',
          titleKey: 'nav.gitPreview',
          icon: 'Connection',
        },
        {
          index: '/findings',
          titleKey: 'nav.findings',
          icon: 'Warning',
          permission: 'finding:read',
        },
      ],
    },
    {
      key: 'group-system',
      titleKey: 'nav.groupSystem',
      icon: 'Setting',
      items: [
        { index: '/api-keys', titleKey: 'nav.apiKeys', icon: 'Key', permission: 'key:list' },
        { index: '/docs', href: '/docs', titleKey: 'nav.docs', icon: 'Document' },
      ],
    },
  ]

  const system = groups[groups.length - 1]
  if (session.isAdmin) {
    system.items.push({ index: '/admin', titleKey: 'nav.admin', icon: 'DataAnalysis' })
  }
  if (session.can('admin:roles')) {
    system.items.push({ index: '/access', titleKey: 'nav.access', icon: 'Lock' })
  }
  return groups
    .filter((group) => !group.permission || session.can(group.permission))
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => !item.permission || session.can(item.permission)),
    }))
    .filter((group) => group.items.length > 0)
})

/** Every visible item, for turning a menu selection back into an action. */
const allItems = computed<NavItem[]>(() => [home, ...groups.value.flatMap((g) => g.items)])

/**
 * Highlight the SPA item that matches the current route.
 *
 * A detail page (`/repos/owner/name`, `/findings/42`) has no menu entry of its
 * own, so a prefix match keeps the parent item lit instead of leaving the whole
 * menu unselected.  The home entry is excluded — every path starts with `/`.
 */
const activeIndex = computed(() => {
  const exact = allItems.value.find((item) => !item.href && item.index === route.path)
  if (exact) return exact.index
  const parent = allItems.value.find(
    (item) =>
      !item.href &&
      item.index !== '/' &&
      route.path.startsWith(`${item.index}/`),
  )
  return parent?.index ?? route.path
})

/** Build stamp injected by vite.config.ts; makes stale bundles obvious. */
const buildId = __BUILD_ID__

/**
 * The menu is deliberately *not* in `router` mode: items mix SPA routes with
 * machine-facing index links, and the latter must open in a new tab rather than
 * be pushed into the history router.
 */
function onSelect(index: string): void {
  const item = allItems.value.find((candidate) => candidate.index === index)
  // Items with an `href` are real links in the template, so the browser already
  // handled the click.  Navigating here as well would open a second tab.
  if (item?.href) return
  router.push(index)
}
</script>

<template>
  <div class="sidebar">
    <div class="sidebar__brand">
      <img
        v-if="logoSrc"
        v-show="logoLoaded && !appStore.sidebarCollapsed"
        class="sidebar__brand-logo"
        :src="logoSrc"
        :alt="session.serverName"
        @load="onLogoLoad"
        @error="onLogoError"
      />
      <!-- Until a custom logo proves it loads (and whenever the sidebar is
           collapsed, where a wide lockup would be unreadable), keep the
           built-in mark + brand name. -->
      <template v-if="!logoLoaded || appStore.sidebarCollapsed">
        <el-icon class="sidebar__logo" :size="22"><FolderOpened /></el-icon>
        <transition name="fade">
          <span v-if="!appStore.sidebarCollapsed" class="sidebar__name">
            {{ session.serverName }}
          </span>
        </transition>
      </template>
    </div>

    <el-menu
      class="sidebar__menu"
      :default-active="activeIndex"
      :collapse="appStore.sidebarCollapsed"
      :collapse-transition="false"
      @select="onSelect"
    >
      <el-menu-item :index="home.index">
        <el-icon><component :is="home.icon" /></el-icon>
        <template #title>{{ t(home.titleKey) }}</template>
      </el-menu-item>

      <el-sub-menu v-for="group in groups" :key="group.key" :index="group.key">
        <template #title>
          <el-icon><component :is="group.icon" /></el-icon>
          <span>{{ t(group.titleKey) }}</span>
        </template>
        <el-menu-item v-for="item in group.items" :key="item.index" :index="item.index">
          <!-- Collapsed, el-menu moves `#title` into a tooltip and only the icon
               stays visible, so a machine-facing index page has to be a link in
               both slots -- otherwise its click reaches `onSelect` and does
               nothing.  Real links keep the browser in charge of the new tab. -->
          <a
            v-if="item.href"
            class="sidebar__external-link"
            :href="item.href ? appUrl(item.href) : undefined"
            target="_blank"
            rel="noopener"
          >
            <el-icon><component :is="item.icon" /></el-icon>
          </a>
          <el-icon v-else><component :is="item.icon" /></el-icon>
          <template #title>
            <a
              v-if="item.href"
              class="sidebar__external-link"
              :href="item.href ? appUrl(item.href) : undefined"
              target="_blank"
              rel="noopener"
            >
              {{ t(item.titleKey) }}
              <el-icon class="sidebar__external"><TopRight /></el-icon>
            </a>
            <span v-else>{{ t(item.titleKey) }}</span>
          </template>
        </el-menu-item>
      </el-sub-menu>
    </el-menu>

    <div class="sidebar__footer">
      <el-divider class="sidebar__divider" />
      <div v-if="!appStore.sidebarCollapsed" class="sidebar__build">build {{ buildId }}</div>
    </div>
  </div>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 20px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--el-border-color-light);
  white-space: nowrap;
  overflow: hidden;
}

.sidebar__logo {
  color: var(--el-color-primary);
  flex-shrink: 0;
}

/* The custom lockup replaces the icon + name, so it may use the full content
   width (220px sidebar - 2×20px padding); `object-fit` keeps a wide image from
   distorting. 30px tall keeps it inside the 56px brand row. */
.sidebar__brand-logo {
  display: block;
  height: 30px;
  max-width: 100%;
  object-fit: contain;
  object-position: left center;
}

.sidebar__name {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.2px;
}

.sidebar__menu {
  flex: 1;
  border-right: none;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar__external {
  margin-left: 6px;
  color: var(--el-text-color-placeholder);
}

.sidebar__external-link {
  display: inline-flex;
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.sidebar__footer {
  flex-shrink: 0;
  padding: 0 8px 12px;
}

.sidebar__divider {
  margin: 8px 0;
}

.sidebar__build {
  padding: 0 12px;
  font-family: 'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
