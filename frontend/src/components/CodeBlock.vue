<script setup lang="ts">
import { useClipboard } from '@/composables/useClipboard'

defineProps<{
  /** The literal text that gets copied. */
  code: string
  /** Optional label rendered above the block. */
  label?: string
}>()

const { copied, copy } = useClipboard()
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
        @click="copy(code)"
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
