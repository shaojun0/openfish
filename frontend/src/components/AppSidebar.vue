<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const route = useRoute()
const appStore = useAppStore()
const session = useSessionStore()

interface NavItem {
  index: string
  titleKey: string
  icon: string
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
 * The server is more than a Python index now, so the menu is grouped by
 * ecosystem rather than listed flat:
 *
 *   Python  → the original PEP 503 registry and the CPython mirror
 *   npm     → the npm catalog scaffold
 *   工具     → the downloadable tools directory
 *   模型路由 → the model routing table for downstream DSH
 *   系统     → keys, statistics and access control
 *
 * Adding a category is a `NavGroup` entry here plus a route in
 * `router/index.ts`; nothing else in the shell needs to change.
 */
const home: NavItem = { index: '/', titleKey: 'nav.home', icon: 'Odometer' }

const groups = computed<NavGroup[]>(() => {
  const groups: NavGroup[] = [
    {
      key: 'group-python',
      titleKey: 'nav.groupPython',
      icon: 'Box',
      permission: 'package:read',
      items: [{ index: '/packages', titleKey: 'nav.packages', icon: 'Box' }],
    },
    {
      key: 'group-npm',
      titleKey: 'nav.groupNpm',
      icon: 'ShoppingBag',
      permission: 'npm:read',
      items: [{ index: '/npm', titleKey: 'nav.npm', icon: 'ShoppingBag' }],
    },
    {
      key: 'group-tools',
      titleKey: 'nav.groupTools',
      icon: 'Tools',
      permission: 'tool:read',
      items: [{ index: '/tools', titleKey: 'nav.tools', icon: 'Tools' }],
    },
    {
      key: 'group-models',
      titleKey: 'nav.groupModels',
      icon: 'Cpu',
      permission: 'model:read',
      items: [{ index: '/models', titleKey: 'nav.models', icon: 'Cpu' }],
    },
    {
      key: 'group-system',
      titleKey: 'nav.groupSystem',
      icon: 'Setting',
      items: [{ index: '/api-keys', titleKey: 'nav.apiKeys', icon: 'Key' }],
    },
  ]

  const system = groups[groups.length - 1]
  if (session.isAdmin) {
    system.items.push({ index: '/admin', titleKey: 'nav.admin', icon: 'DataAnalysis' })
  }
  if (session.can('admin:roles')) {
    system.items.push({ index: '/access', titleKey: 'nav.access', icon: 'Lock' })
  }
  return groups.filter((group) => !group.permission || session.can(group.permission))
})

/** Machine-facing endpoints, opened in a new tab — not part of the SPA. */
const machineLinks = [
  { href: '/simple/', labelKey: 'nav.index', icon: 'Link' },
  { href: '/python-builds/', labelKey: 'nav.builds', icon: 'Download' },
  { href: '/docs', labelKey: 'nav.docs', icon: 'Document' },
]

const activeIndex = computed(() => route.path)

/** Build stamp injected by vite.config.ts; makes stale bundles obvious. */
const buildId = __BUILD_ID__
</script>

<template>
  <div class="sidebar">
    <div class="sidebar__brand">
      <el-icon class="sidebar__logo" :size="22"><FolderOpened /></el-icon>
      <transition name="fade">
        <span v-if="!appStore.sidebarCollapsed" class="sidebar__name">
          {{ session.serverName }}
        </span>
      </transition>
    </div>

    <el-menu
      class="sidebar__menu"
      :default-active="activeIndex"
      :collapse="appStore.sidebarCollapsed"
      :collapse-transition="false"
      router
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
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ t(item.titleKey) }}</template>
        </el-menu-item>
      </el-sub-menu>
    </el-menu>

    <div class="sidebar__footer">
      <el-divider class="sidebar__divider" />
      <el-tooltip
        v-for="link in machineLinks"
        :key="link.href"
        :content="link.href"
        placement="right"
        :disabled="!appStore.sidebarCollapsed"
      >
        <a class="sidebar__link" :href="link.href" target="_blank" rel="noopener">
          <el-icon><component :is="link.icon" /></el-icon>
          <span v-if="!appStore.sidebarCollapsed">{{ t(link.labelKey) }}</span>
        </a>
      </el-tooltip>
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

.sidebar__footer {
  flex-shrink: 0;
  padding: 0 8px 12px;
}

.sidebar__divider {
  margin: 8px 0;
}

.sidebar__link {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 40px;
  padding: 0 12px;
  border-radius: 6px;
  color: var(--el-text-color-regular);
  text-decoration: none;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
}

.sidebar__link:hover {
  background: var(--el-fill-color-light);
  color: var(--el-color-primary);
}

.sidebar__build {
  padding: 8px 12px 0;
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
