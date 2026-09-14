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

const items = computed<NavItem[]>(() => {
  const base: NavItem[] = [
    { index: '/', titleKey: 'nav.home', icon: 'Odometer' },
    { index: '/packages', titleKey: 'nav.packages', icon: 'Box' },
    { index: '/api-keys', titleKey: 'nav.apiKeys', icon: 'Key' },
  ]
  if (session.isAdmin) {
    base.push({ index: '/admin', titleKey: 'nav.admin', icon: 'DataAnalysis' })
  }
  return base
})

/** Machine-facing endpoints, opened in a new tab — not part of the SPA. */
const machineLinks = [
  { href: '/simple/', labelKey: 'nav.builds', icon: 'Link' },
]

const activeIndex = computed(() => route.path)
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
      <el-menu-item v-for="item in items" :key="item.index" :index="item.index">
        <el-icon><component :is="item.icon" /></el-icon>
        <template #title>{{ t(item.titleKey) }}</template>
      </el-menu-item>
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

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
