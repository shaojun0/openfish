<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import type { AppLocale } from '@/locales'
import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const session = useSessionStore()

/** Breadcrumb built from the matched route chain. */
const crumbs = computed(() =>
  route.matched
    .filter((record) => Boolean(record.meta?.titleKey))
    .map((record) => ({
      key: String(record.meta.titleKey),
      path: record.path,
    })),
)

const roleLabelKey = computed(() => {
  if (!session.authenticated) return 'session.roleAnonymous'
  return session.isAdmin ? 'session.roleAdmin' : 'session.roleAuthenticated'
})

function switchLocale(locale: AppLocale): void {
  appStore.setLocale(locale)
}

function handleCommand(command: string): void {
  if (command === 'logout') {
    window.location.href = '/auth/logout'
  } else if (command === 'refresh') {
    void session.load(true)
    router.go(0)
  }
}
</script>

<template>
  <div class="header">
    <el-button class="header__collapse" text @click="appStore.toggleSidebar()">
      <el-icon :size="18">
        <component :is="appStore.sidebarCollapsed ? 'Expand' : 'Fold'" />
      </el-icon>
    </el-button>

    <el-breadcrumb class="header__crumbs" separator="/">
      <el-breadcrumb-item v-for="crumb in crumbs" :key="crumb.key">
        {{ t(crumb.key) }}
      </el-breadcrumb-item>
    </el-breadcrumb>

    <div class="header__spacer" />

    <el-dropdown trigger="click" @command="switchLocale">
      <el-button text>
        <el-icon><Connection /></el-icon>
        <span class="header__label">{{ appStore.locale === 'zh-CN' ? '中文' : 'EN' }}</span>
      </el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="zh-CN" :disabled="appStore.locale === 'zh-CN'">
            中文
          </el-dropdown-item>
          <el-dropdown-item command="en-US" :disabled="appStore.locale === 'en-US'">
            English
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <el-tooltip :content="t(appStore.isDark ? 'theme.toLight' : 'theme.toDark')">
      <el-button text @click="appStore.toggleTheme()">
        <el-icon :size="17">
          <component :is="appStore.isDark ? 'Sunny' : 'Moon'" />
        </el-icon>
      </el-button>
    </el-tooltip>

    <el-dropdown trigger="click" @command="handleCommand">
      <el-button text class="header__user">
        <el-icon><UserFilled /></el-icon>
        <span class="header__label">{{ session.user ?? t('session.anonymous') }}</span>
        <el-tag size="small" type="info" effect="plain">{{ t(roleLabelKey) }}</el-tag>
      </el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="refresh">
            <el-icon><Refresh /></el-icon>{{ t('common.refresh') }}
          </el-dropdown-item>
          <el-dropdown-item v-if="session.authEnabled" command="logout" divided>
            <el-icon><SwitchButton /></el-icon>{{ t('session.signOut') }}
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 100%;
  padding: 0 16px 0 8px;
}

.header__collapse {
  flex-shrink: 0;
}

.header__crumbs {
  margin-left: 4px;
  white-space: nowrap;
}

.header__spacer {
  flex: 1;
}

.header__label {
  margin-left: 4px;
}

.header__user :deep(span) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
