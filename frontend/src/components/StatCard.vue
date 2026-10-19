<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  icon: string
  tone?: 'primary' | 'success' | 'warning' | 'info' | 'danger'
  /**
   * Render a skeleton instead of the value.  The overview page paints before
   * the numbers it reads are available, so the card must not blank the page
   * while a slow package list / stats aggregation is in flight.
   */
  loading?: boolean
}>()
</script>

<template>
  <el-card class="stat" shadow="hover" :body-style="{ padding: '16px' }">
    <div class="stat__row">
      <div class="stat__text">
        <div class="stat__label">{{ label }}</div>
        <div class="stat__value">
          <el-skeleton v-if="loading" animated>
            <template #template>
              <el-skeleton-item variant="text" style="width: 72px; height: 29px" />
            </template>
          </el-skeleton>
          <template v-else>{{ value }}</template>
        </div>
      </div>
      <el-icon class="stat__icon" :class="`stat__icon--${tone ?? 'primary'}`" :size="26">
        <component :is="icon" />
      </el-icon>
    </div>
  </el-card>
</template>

<style scoped>
.stat__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.stat__label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.stat__value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

.stat__icon {
  flex-shrink: 0;
}

.stat__icon--primary {
  color: var(--el-color-primary);
}
.stat__icon--success {
  color: var(--el-color-success);
}
.stat__icon--warning {
  color: var(--el-color-warning);
}
.stat__icon--danger {
  color: var(--el-color-danger);
}
.stat__icon--info {
  color: var(--el-color-info);
}
</style>
