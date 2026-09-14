import { createI18n } from 'vue-i18n'

import enUS from './en-US'
import zhCN from './zh-CN'

export type AppLocale = 'zh-CN' | 'en-US'

export const SUPPORTED_LOCALES: AppLocale[] = ['zh-CN', 'en-US']

function initialLocale(): AppLocale {
  return localStorage.getItem('cpypi.locale') === 'en-US' ? 'en-US' : 'zh-CN'
}

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: initialLocale(),
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})
