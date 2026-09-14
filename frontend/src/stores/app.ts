import { defineStore } from 'pinia'
import { computed, ref, watchEffect } from 'vue'

import { i18n, type AppLocale } from '@/locales'

export type AppTheme = 'dark' | 'light'

const THEME_KEY = 'cpypi.theme'
const LOCALE_KEY = 'cpypi.locale'
const SIDEBAR_KEY = 'cpypi.sidebar'

function readTheme(): AppTheme {
  return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
}

function readLocale(): AppLocale {
  return localStorage.getItem(LOCALE_KEY) === 'en-US' ? 'en-US' : 'zh-CN'
}

/** UI preferences that outlive a page reload. */
export const useAppStore = defineStore('app', () => {
  const theme = ref<AppTheme>(readTheme())
  const locale = ref<AppLocale>(readLocale())
  const sidebarCollapsed = ref(localStorage.getItem(SIDEBAR_KEY) === '1')

  const isDark = computed(() => theme.value === 'dark')

  watchEffect(() => {
    const value = theme.value
    localStorage.setItem(THEME_KEY, value)
    document.documentElement.setAttribute('data-theme', value)
    document.documentElement.classList.toggle('dark', value === 'dark')
  })

  watchEffect(() => {
    const value = locale.value
    localStorage.setItem(LOCALE_KEY, value)
    i18n.global.locale.value = value
    document.documentElement.setAttribute('lang', value)
  })

  watchEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed.value ? '1' : '0')
  })

  function toggleTheme(): void {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function setLocale(value: AppLocale): void {
    locale.value = value
  }

  return { theme, locale, sidebarCollapsed, isDark, toggleTheme, toggleSidebar, setLocale }
})
