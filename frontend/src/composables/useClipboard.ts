import { ElMessage } from 'element-plus'
import { onScopeDispose, ref } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * Copy text to the clipboard, reporting the outcome the one way the app does:
 * `common.copied` on success, `common.copyFailed` on failure.
 *
 * `copied` flips back after `resetAfterMs` so a caller can swap its icon for a
 * moment without keeping its own timer.  Every copy button in the SPA goes
 * through here — the raw `navigator.clipboard` dance lives in exactly one place.
 */
export function useClipboard(resetAfterMs = 1500) {
  const { t } = useI18n()
  const copied = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined

  async function copy(text: string): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text)
      copied.value = true
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => (copied.value = false), resetAfterMs)
      ElMessage.success(t('common.copied'))
      return true
    } catch {
      ElMessage.warning(t('common.copyFailed'))
      return false
    }
  }

  onScopeDispose(() => {
    if (timer) clearTimeout(timer)
  })

  return { copied, copy }
}
