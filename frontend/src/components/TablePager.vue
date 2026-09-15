<script setup lang="ts">
/**
 * Shared pagination footer for the SPA tables.
 *
 * The lists are paged client-side by `usePagination()`, so the parent hands the
 * four numbers over and binds `page` / `pageSize` with `v-model`.  Element Plus
 * picks the strings ("Total X", sizes, jumper) up from the `el-config-provider`
 * locale already mounted in `App.vue`, so no extra i18n keys are needed here.
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    total: number
    page: number
    pageSize: number
    pageSizes?: number[]
    /** Drop the size selector and jumper for narrow containers (drawers, …). */
    compact?: boolean
  }>(),
  {
    pageSizes: () => [10, 20, 50, 100],
    compact: false,
  },
)

const emit = defineEmits<{
  (e: 'update:page', value: number): void
  (e: 'update:pageSize', value: number): void
}>()

const layout = computed(() =>
  props.compact ? 'total, prev, pager, next' : 'total, sizes, prev, pager, next, jumper',
)
</script>

<template>
  <div v-if="total > 0" class="table-pager">
    <el-pagination
      background
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="pageSizes"
      :total="total"
      :layout="layout"
      @update:current-page="emit('update:page', $event)"
      @update:page-size="emit('update:pageSize', $event)"
    />
  </div>
</template>

<style scoped>
.table-pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 14px;
  overflow-x: auto;
}

.table-pager :deep(.el-pagination) {
  flex-wrap: wrap;
  row-gap: 6px;
}
</style>
