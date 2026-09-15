import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'

/** The slice of the `el-table` sort-change payload this composable acts on. */
export interface SortPayload {
  prop: string | null
  order: 'ascending' | 'descending' | null
}

export interface UsePaginationOptions {
  /** Rows per page on first render; defaults to the second entry of `pageSizes`. */
  pageSize?: number
  /** Choices offered by the size selector. */
  pageSizes?: number[]
}

/** A list the caller owns — either a plain ref or a computed projection. */
export type PaginationSource<T> = Ref<T[]> | ComputedRef<T[]>

/** Default size choices — 20 matches the row count a desktop card shows comfortably. */
export const DEFAULT_PAGE_SIZES = [10, 20, 50, 100]

/**
 * Client-side pagination (and optional sorting) for an `el-table`.
 *
 * The `/api/v1` catalog endpoints hand back the whole list, so paging happens
 * in the browser: `rows` is the current slice and `total` the full count.  That
 * is deliberate — it keeps the API contract unchanged and stops `el-table` from
 * rendering thousands of DOM rows at once, which is what made the big catalogs
 * sluggish.
 *
 * `sortable="custom"` columns must call `onSortChange` from `@sort-change`;
 * sorting the source before slicing keeps a sort global instead of "sort only
 * the rows currently on screen".  Call `reset()` whenever a filter changes so
 * the user lands back on page one.
 */
export function usePagination<T>(source: PaginationSource<T>, options: UsePaginationOptions = {}) {
  const pageSizes = options.pageSizes ?? DEFAULT_PAGE_SIZES
  const pageSize = ref(options.pageSize ?? pageSizes[1] ?? pageSizes[0] ?? 20)
  const page = ref(1)
  const sortProp = ref<string | null>(null)
  const sortOrder = ref<'ascending' | 'descending' | null>(null)

  const sorted = computed<T[]>(() => {
    const prop = sortProp.value
    const order = sortOrder.value
    if (!prop || !order) return source.value
    const direction = order === 'ascending' ? 1 : -1
    return [...source.value].sort(
      (a, b) =>
        direction *
        compareValues((a as Record<string, unknown>)[prop], (b as Record<string, unknown>)[prop]),
    )
  })

  const total = computed(() => sorted.value.length)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
  const rows = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return sorted.value.slice(start, start + pageSize.value)
  })

  // A shrinking list (filter, refresh, page-size change) must not strand the
  // user on a page that no longer exists.
  watch([total, pageSize], () => {
    if (page.value > pageCount.value) page.value = pageCount.value
    if (page.value < 1) page.value = 1
  })

  function onSortChange(payload: SortPayload): void {
    sortProp.value = payload.prop
    sortOrder.value = payload.order
    page.value = 1
  }

  function reset(): void {
    page.value = 1
  }

  return { page, pageSize, pageSizes, total, pageCount, rows, onSortChange, reset }
}

/**
 * Numbers sort numerically even when they arrive as strings (`"12"` beats
 * `"9"`), ISO timestamps sort lexicographically and everything else falls back
 * to a locale-aware, digit-aware string compare.  Empty values sink to the
 * bottom in both directions.
 */
function compareValues(a: unknown, b: unknown): number {
  if (a === b) return 0
  if (a === null || a === undefined || a === '') return 1
  if (b === null || b === undefined || b === '') return -1

  if (typeof a === 'number' && typeof b === 'number') return a - b

  const na = toNumber(a)
  const nb = toNumber(b)
  if (na !== null && nb !== null) return na - nb

  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
