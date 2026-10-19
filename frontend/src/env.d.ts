/// <reference types="vite/client" />

/** Injected by `define` in vite.config.ts — the bundle's build stamp. */
declare const __BUILD_ID__: string

/**
 * Injected by `define` in vite.config.ts — the client-visible path prefix
 * (e.g. `/pypi`) taken from the `VITE_BASE_PATH` build argument. Empty = root.
 * Consumed once, in `src/base.ts`; do not use it directly elsewhere.
 */
declare const __BASE_PATH__: string

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
