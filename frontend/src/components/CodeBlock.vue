<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  /** The literal text that gets copied. */
  code: string
  /** Optional label rendered above the block. */
  label?: string
}>()

const { t } = useI18n()
const copied = ref(false)

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.code)
    copied.value = true
    ElMessage.success(t('common.copied'))
    window.setTimeout(() => (copied.value = false), 1500)
  } catch {
    ElMessage.warning(t('common.copyFailed'))
  }
}
</script>

<template>
  <div class="code">
    <div v-if="label" class="code__label">{{ label }}</div>
    <pre class="code-block"><code>{{ code }}</code>
      <el-button
        class="code-block__copy"
        size="small"
        text
        :type="copied ? 'success' : 'default'"
        @click="copy"
      >
        <el-icon><component :is="copied ? 'Select' : 'CopyDocument'" /></el-icon>
      </el-button>
    </pre>
  </div>
</template>

<style scoped>
.code {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.code__label {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-regular);
}
</style>
