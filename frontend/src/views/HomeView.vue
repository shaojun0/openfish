<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { fetchAdminStats, fetchStatsOverview, type AdminStats, type StatsOverview } from '@/api'
import { apiError, apiStatus } from '@/api/client'
import StatCard from '@/components/StatCard.vue'
import { useSessionStore } from '@/stores/session'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()

/**
 * The three counters come from `/api/v1/stats/overview` — a cache-first
 * endpoint that never `stat`s the package files on the request thread.  The
 * overview page used to call `/api/v1/packages` for them (admins used
 * `/api/v1/admin/stats`), which recomputed every size on every request and, on
 * a 1.5 TB registry, outlived the SPA's 30 s client timeout — the failure then
 * rendered as an empty registry.
 */
const stats = ref<StatsOverview | null>(null)
const admin = ref<AdminStats | null>(null)
const loading = ref(true)
const error = ref('')

// The first statistics pass after a cold boot walks every package file.  The
// counts are final immediately (`sizes_ready`); only storage is pending, so the
// page polls until the background refresh publishes it rather than showing 0 B.
const WARM_POLL_MS = 5000
const WARM_MAX_POLLS = 36
let warmPolls = 0
let warmTimer: ReturnType<typeof setTimeout> | null = null

// A first load right after `docker compose up -d` can hit the edge proxy while
// gunicorn is still importing the app (that is where the 16 s waits came from).
// Retry a few times before showing an error, so the page heals itself.
const BOOT_RETRY_MS = 3000
const BOOT_MAX_RETRIES = 10
let bootRetries = 0
let bootTimer: ReturnType<typeof setTimeout> | null = null

const overview = computed(() => admin.value?.overview ?? null)

const storageValue = computed(() => {
  if (!stats.value) return ''
  if (!stats.value.sizes_ready) return t('stat.calculating')
  return stats.value.total_storage_human
})

const shortcuts = computed(() => [
  {
    key: 'packages',
    title: t('home.quickPackages'),
    description: t('home.quickPackagesDesc'),
    icon: 'Box',
    to: '/packages',
  },
  {
    key: 'npm',
    title: t('home.quickNpm'),
    description: t('home.quickNpmDesc'),
    icon: 'ShoppingBag',
    to: '/npm',
  },
  {
    key: 'tools',
    title: t('home.quickTools'),
    description: t('home.quickToolsDesc'),
    icon: 'Tools',
    to: '/tools',
  },
  {
    key: 'models',
    title: t('home.quickModels'),
    description: t('home.quickModelsDesc'),
    icon: 'Cpu',
    to: '/models',
  },
  {
    key: 'keys',
    title: t('home.quickKeys'),
    description: t('home.quickKeysDesc'),
    icon: 'Key',
    to: '/api-keys',
  },
  ...(session.isAdmin
    ? [
        {
          key: 'admin',
          title: t('home.quickAdmin'),
          description: t('home.quickAdminDesc'),
          icon: 'DataAnalysis',
          to: '/admin',
        },
      ]
    : []),
])

async function load(): Promise<void> {
  // No session gate: the counters need nothing but `package:read`, and waiting
  // for the identity is what made a cold `/api/v1/session` hold the cards.
  loading.value = !stats.value
  try {
    stats.value = await fetchStatsOverview()
    error.value = ''
    bootRetries = 0
    if (!stats.value.sizes_ready) scheduleWarmPoll()
  } catch (e) {
    error.value = apiError(e) || t('home.loadFailed')
    if (!stats.value && apiStatus(e) !== 401) scheduleBootRetry()
  } finally {
    loading.value = false
  }
}

function scheduleBootRetry(): void {
  if (bootTimer !== null || bootRetries >= BOOT_MAX_RETRIES) return
  bootRetries += 1
  bootTimer = setTimeout(() => {
    bootTimer = null
    void load()
  }, BOOT_RETRY_MS)
}

function scheduleWarmPoll(): void {
  if (warmTimer !== null || warmPolls >= WARM_MAX_POLLS) return
  warmPolls += 1
  warmTimer = setTimeout(() => {
    warmTimer = null
    void load()
  }, WARM_POLL_MS)
}

/**
 * Admins additionally see the download/upload/key counters, which only the
 * admin surface exposes.  That request stays off the first-paint path: the base
 * cards render from the cached overview alone.
 */
async function loadAdminCounters(): Promise<void> {
  try {
    admin.value = await fetchAdminStats()
  } catch {
    // The counter row is optional garnish; its failure must not disturb the page.
  }
}

onMounted(() => {
  void load()
})

watch(
  () => session.loaded,
  (ready) => {
    if (ready && session.isAdmin) void loadAdminCounters()
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (warmTimer !== null) clearTimeout(warmTimer)
  if (bootTimer !== null) clearTimeout(bootTimer)
  warmTimer = null
  bootTimer = null
})
</script>

<template>
  <div class="page">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('home.title') }}</h1>
        <p class="page__description">
          {{ t('home.description', { server: session.serverName }) }}
        </p>
      </div>
      <el-tag type="success" effect="plain">
        <el-icon><CircleCheck /></el-icon>
        <span style="margin-left: 4px">{{ t('common.yes') }} · /health</span>
      </el-tag>
    </div>

    <el-alert
      v-if="error"
      class="page__alert"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    >
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>

    <el-alert
      v-else-if="stats && !stats.sizes_ready"
      class="page__alert"
      type="info"
      :closable="false"
      show-icon
      :title="t('home.warming')"
    />

    <div class="stat-grid">
      <StatCard
        :label="t('stat.packages')"
        :value="stats?.package_count ?? ''"
        :loading="loading"
        icon="Box"
      />
      <StatCard
        :label="t('stat.files')"
        :value="stats?.file_count ?? ''"
        :loading="loading"
        icon="Document"
        tone="info"
      />
      <StatCard
        :label="t('stat.storage')"
        :value="storageValue"
        :loading="loading"
        icon="Coin"
        tone="warning"
      />
      <template v-if="overview">
        <StatCard
          :label="t('stat.downloads')"
          :value="overview.total_downloads"
          icon="Download"
          tone="success"
        />
        <StatCard
          :label="t('stat.uploads')"
          :value="overview.total_uploads"
          icon="Upload"
          tone="primary"
        />
        <StatCard
          :label="t('stat.activeKeys')"
          :value="`${overview.active_keys} / ${overview.total_keys}`"
          icon="Key"
          tone="danger"
        />
      </template>
    </div>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('home.quickTitle') }}</span>
      </template>
      <div class="shortcuts">
        <el-card
          v-for="item in shortcuts"
          :key="item.key"
          class="shortcut"
          shadow="hover"
          @click="router.push(item.to)"
        >
          <el-icon class="shortcut__icon" :size="22"><component :is="item.icon" /></el-icon>
          <div class="shortcut__text">
            <div class="shortcut__title">{{ item.title }}</div>
            <div class="shortcut__desc">{{ item.description }}</div>
          </div>
          <el-icon class="shortcut__arrow"><ArrowRight /></el-icon>
        </el-card>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.page__alert {
  margin-bottom: 12px;
}

.shortcuts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.shortcut {
  cursor: pointer;
}

.shortcut :deep(.el-card__body) {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
}

.shortcut__icon {
  color: var(--el-color-primary);
  flex-shrink: 0;
}

.shortcut__text {
  flex: 1;
  min-width: 0;
}

.shortcut__title {
  font-size: 14px;
  font-weight: 600;
}

.shortcut__desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}

.shortcut__arrow {
  color: var(--el-text-color-placeholder);
  flex-shrink: 0;
}
</style>
