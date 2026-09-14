/// <reference types="vite/client" />

/** Injected by `define` in vite.config.ts — the bundle's build stamp. */
declare const __BUILD_ID__: string

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
