<script setup lang="ts">
import en from 'element-plus/es/locale/lang/en'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { ElConfigProvider } from 'element-plus'
import { computed, onMounted } from 'vue'

import { useAppStore } from '@/stores/app'
import { useSessionStore } from '@/stores/session'

const appStore = useAppStore()
const session = useSessionStore()

const elementLocale = computed(() => (appStore.locale === 'zh-CN' ? zhCn : en))

onMounted(() => {
  void session.load()
})
</script>

<template>
  <el-config-provider :locale="elementLocale">
    <router-view />
  </el-config-provider>
</template>
