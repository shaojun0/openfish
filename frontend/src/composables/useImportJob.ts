import { onScopeDispose, ref } from 'vue'

import { fetchImportJob } from '@/api/agentHub'

/**
 * The `ImportJob` fields the two progress panels read.  Kept here (rather than
 * inferred from the `.js` API module) so the composable and both views share
 * one declaration.
 */
export interface ImportJob {
  id: number | string
  repo_slug?: string | null
  mode?: string | null
  status?: string | null
  /** migrate | poll | mirror_issues | index_commits | build_search | done */
  phase?: string | null
  /** 0-100, when the backend can compute a percentage. */
  progress?: number | null
  total?: number | null
  done?: number | null
  cursor?: string | null
  /**
   * §8.2 big-repo guard: the issue mirror stopped at `IMPORT_MAX_ISSUES`.
   * The API must expose this — the panel turns it into a loud warning because
   * a silent truncation would make every later search look complete.
   */
  partial?: boolean | null
  error?: string | null
  started_at?: string | null
  finished_at?: string | null
}

const TERMINAL_STATUSES = ['done', 'error', 'failed', 'cancelled', 'dead']

export function isImportJobTerminal(job: ImportJob | null | undefined): boolean {
  return TERMINAL_STATUSES.includes(String(job?.status ?? '').toLowerCase())
}

/**
 * Poll one import job until it settles.
 *
 * Polling starts only from a user action (an import or sync submit), so an SSR
 * render never opens a timer — this is what keeps the smoke test quiet.  The
 * scope tear-down stops the timer, and overlapping ticks are impossible
 * because a tick that is still in flight is skipped.
 */
export function useImportJob(intervalMs = 2000) {
  const job = ref<ImportJob | null>(null)
  const polling = ref(false)
  const error = ref('')
  let timer: ReturnType<typeof setInterval> | undefined
  let inFlight = false

  function stop(): void {
    if (timer !== undefined) {
      clearInterval(timer)
      timer = undefined
    }
    polling.value = false
  }

  async function refresh(id: number | string): Promise<void> {
    if (inFlight) return
    inFlight = true
    try {
      job.value = (await fetchImportJob(id)) as ImportJob
      error.value = ''
      if (isImportJobTerminal(job.value)) stop()
    } catch (e) {
      // One failed poll is not fatal — keep the last known state and retry.
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      inFlight = false
    }
  }

  /**
   * Begin tracking.  Accepts the `ImportJob` the create/sync call already
   * answered with (no wasted first request) or a bare job id.
   */
  function track(source: ImportJob | number | string): void {
    stop()
    error.value = ''
    if (source && typeof source === 'object') {
      job.value = source
    } else {
      job.value = { id: source }
    }
    if (isImportJobTerminal(job.value)) return
    polling.value = true
    const id = job.value.id
    timer = setInterval(() => {
      void refresh(id)
    }, intervalMs)
  }

  onScopeDispose(stop)

  return { job, polling, error, track, stop, refresh }
}
