/** Formatting helpers shared across views. */
import dayjs from 'dayjs'

/** Human readable byte size — mirrors the backend's `human_size`. */
export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[unit]}`
}

/** Render an ISO-8601 UTC timestamp in the browser's local timezone. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = dayjs(value)
  return date.isValid() ? date.format('YYYY-MM-DD HH:mm') : value
}

/** The date part of a timestamp, for the compact metadata lines. */
export function formatDateOnly(value: string | null | undefined): string {
  if (!value) return '—'
  const date = dayjs(value)
  return date.isValid() ? date.format('YYYY-MM-DD') : value
}

/**
 * Build the pip/twine snippet for an API key.
 * A leading `__token__` username is the twine convention.
 */
export function tokenCredentials(apiKey: string): string {
  return `__token__:${apiKey}`
}
