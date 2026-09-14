<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const session = useSessionStore()

interface NavItem {
  /** Unique menu index: an SPA path, or the href for an external link. */
  index: string
  titleKey: string
  icon: string
  /** Set for machine-facing / static index links — opened in a new tab. */
  href?: string
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
 * ecosystem lives inside its group** — the rich SPA page and the machine-facing
 * index elements alike:
 *
 *   Python 生态 → 包管理 (SPA) · Python 索引 (/simple/) · Python 构建
 *   npm 生态    → npm 目录 (SPA) · npm 静态索引 (/npm/)
 *   工具        → 工具目录 (SPA) · 工具静态索引 (/tools/)
 *   模型路由    → 模型路由 (SPA) · 路由表 JSON (/api/v1/models)
 *   系统        → API 密钥 · 系统统计 · 访问控制 · 接口文档
 *
 * Items with an `href` are machine-facing (no JavaScript) and open in a new
 * tab; the rest are SPA routes.
 */
const home: NavItem = { index: '/', titleKey: 'nav.home', icon: 'Odometer' }

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
        },
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
      ],
    },
    {
      key: 'group-system',
      titleKey: 'nav.groupSystem',
      icon: 'Setting',
      items: [
        { index: '/api-keys', titleKey: 'nav.apiKeys', icon: 'Key' },
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
  return groups.filter((group) => !group.permission || session.can(group.permission))
})

/** Every visible item, for turning a menu selection back into an action. */
const allItems = computed<NavItem[]>(() => [home, ...groups.value.flatMap((g) => g.items)])

/** Highlight the SPA item that matches the current route. */
const activeIndex = computed(() => {
  const match = allItems.value.find((item) => !item.href && item.index === route.path)
  return match?.index ?? route.path
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
  if (item?.href) {
    window.open(item.href, '_blank', 'noopener')
    return
  }
  router.push(index)
}
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
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>
            <span>{{ t(item.titleKey) }}</span>
            <el-icon v-if="item.href" class="sidebar__external"><TopRight /></el-icon>
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
