<script setup lang="ts">
import { computed } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import AppSidebar from '@/components/AppSidebar.vue'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const asideWidth = computed(() => (appStore.sidebarCollapsed ? '64px' : '220px'))
</script>

<template>
  <el-container class="layout">
    <el-aside class="layout__aside" :width="asideWidth">
      <AppSidebar />
    </el-aside>

    <el-container class="layout__body">
      <el-header class="layout__header" height="56px">
        <AppHeader />
      </el-header>

      <el-main class="layout__main">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}

.layout__aside {
  transition: width 0.2s ease;
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
  overflow: hidden;
}

.layout__body {
  min-width: 0;
}

.layout__header {
  padding: 0;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}

.layout__main {
  padding: 20px;
  background: var(--el-bg-color-page);
  overflow-y: auto;
}

.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
