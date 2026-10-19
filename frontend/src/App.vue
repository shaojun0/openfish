<script setup lang="ts">
import en from 'element-plus/es/locale/lang/en'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { ElConfigProvider } from 'element-plus'
import { computed, onMounted, watch } from 'vue'

import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const appStore = useAppStore()
const session = useSessionStore()

const elementLocale = computed(() => (appStore.locale === 'zh-CN' ? zhCn : en))

onMounted(() => {
  void session.load()
})

// The SPA is a static shell, so the tab title has to come from the backend's
// branding (`SERVER_NAME`, docker/.env) once the session resolves; a custom
// deployment therefore does not need to rebuild the image just to rename it.
watch(
  () => session.serverName,
  (name) => {
    document.title = name
  },
  { immediate: true },
)
</script>

<template>
  <el-config-provider :locale="elementLocale">
    <router-view />
  </el-config-provider>
</template>
