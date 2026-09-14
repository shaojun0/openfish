<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { fetchAdminStats, fetchPackages, type AdminStats, type PackageSummary } from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import StatCard from '@/components/StatCard.vue'
import { useSessionStore } from '@/stores/session'
import { formatBytes } from '@/utils/format'

const { t } = useI18n()
const router = useRouter()
const session = useSessionStore()

const packages = ref<PackageSummary[]>([])
const admin = ref<AdminStats | null>(null)
const loading = ref(true)

const baseUrl = computed(() => window.location.origin)
const host = computed(() => window.location.host)

const localTotals = computed(() => ({
  packages: packages.value.length,
  files: packages.value.reduce((sum, p) => sum + p.file_count, 0),
  storage: formatBytes(packages.value.reduce((sum, p) => sum + p.total_size, 0)),
}))

const overview = computed(() => admin.value?.overview ?? null)

const pipSnippet = computed(
  () => `pip install -i ${baseUrl.value}/simple/ <package-name>`,
)

const netrcSnippet = computed(
  () => `machine ${host.value}\n  login __token__\n  password <API-KEY>`,
)

const twineSnippet = computed(
  () =>
    `twine upload --repository-url ${baseUrl.value}/ \\\n` +
    `  --username __token__ --password <API-KEY> dist/*`,
)

const uvSnippet = computed(
  () =>
    `# Unix\n` +
    `export UV_PYTHON_INSTALL_MIRROR="${baseUrl.value}/python-builds/"\n` +
    `uv python install 3.12\n\n` +
    `# PowerShell\n` +
    `$env:UV_PYTHON_INSTALL_MIRROR = "${baseUrl.value}/python-builds/"`,
)

const shortcuts = computed(() => [
  {
    key: 'packages',
    title: t('home.quickPackages'),
    description: t('home.quickPackagesDesc'),
    icon: 'Box',
    to: '/packages',
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
  loading.value = true
  try {
    packages.value = await fetchPackages()
  } catch (e) {
    ElMessage.error(apiError(e))
  }
  if (session.isAdmin) {
    try {
      admin.value = await fetchAdminStats()
    } catch {
      admin.value = null
    }
  }
  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
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
        icon="Box"
      />
      <StatCard
        :label="t('stat.files')"
        :value="overview?.file_count ?? localTotals.files"
        icon="Document"
        tone="info"
      />
      <StatCard
        :label="t('stat.storage')"
        :value="overview?.total_storage_human ?? localTotals.storage"
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

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('home.usageTitle') }}</span>
      </template>
      <el-tabs>
        <el-tab-pane :label="t('home.pipTitle')">
          <p class="hint">{{ t('home.pipDesc') }}</p>
          <CodeBlock :code="pipSnippet" />
        </el-tab-pane>
        <el-tab-pane :label="t('home.netrcTitle')">
          <p class="hint">{{ t('home.netrcDesc') }}</p>
          <CodeBlock :code="netrcSnippet" />
        </el-tab-pane>
        <el-tab-pane :label="t('home.twineTitle')">
          <p class="hint">{{ t('home.twineDesc') }}</p>
          <CodeBlock :code="twineSnippet" />
        </el-tab-pane>
        <el-tab-pane :label="t('home.uvTitle')">
          <p class="hint">{{ t('home.uvDesc') }}</p>
          <CodeBlock :code="uvSnippet" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.card-title {
  font-weight: 600;
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

.hint {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
