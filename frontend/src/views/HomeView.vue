<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { fetchAdminStats, fetchPackages, type AdminStats, type PackageSummary } from '@/api'
import { apiError } from '@/api/client'
import StatCard from '@/components/StatCard.vue'
import { useSessionStore } from '@/stores/session'
import { formatBytes } from '@/utils/format'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()

const packages = ref<PackageSummary[]>([])
const admin = ref<AdminStats | null>(null)
const loading = ref(true)

const localTotals = computed(() => ({
  packages: packages.value.length,
  files: packages.value.reduce((sum, p) => sum + p.file_count, 0),
  storage: formatBytes(packages.value.reduce((sum, p) => sum + p.total_size, 0)),
}))

const overview = computed(() => admin.value?.overview ?? null)

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
  // The route paints before `/api/v1/session` resolves (see router/index.ts);
  // wait for the identity because it decides which endpoint fills the cards:
  // admins read the cached overview, everyone else the package list (the only
  // source of the three totals a non-admin may read).
  if (!session.loaded) return
  loading.value = true
  try {
    if (session.isAdmin) {
      admin.value = await fetchAdminStats()
    } else {
      packages.value = await fetchPackages()
    }
  } catch (e) {
    ElMessage.error(apiError(e) || t('home.loadFailed'))
  } finally {
    loading.value = false
  }
}

// Never gate the page on the data: the skeleton cards stand in while the
// package list (1 MB+, and slow to aggregate on a 100k-file registry) loads.
watch(
  () => session.loaded,
  (ready) => {
    if (ready) void load()
  },
  { immediate: true },
)
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

    <div class="stat-grid">
      <StatCard
        :label="t('stat.packages')"
        :value="overview?.package_count ?? localTotals.packages"
        :loading="loading"
        icon="Box"
      />
      <StatCard
        :label="t('stat.files')"
        :value="overview?.file_count ?? localTotals.files"
        :loading="loading"
        icon="Document"
        tone="info"
      />
      <StatCard
        :label="t('stat.storage')"
        :value="overview?.total_storage_human ?? localTotals.storage"
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
