<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

const { t } = useI18n()
const router = useRouter()
</script>

<template>
  <div class="not-found">
    <el-result icon="warning" :title="t('notFound.title')" :sub-title="t('notFound.description')">
      <template #extra>
        <el-button type="primary" @click="router.push('/')">{{ t('notFound.back') }}</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--el-bg-color-page);
}
</style>
