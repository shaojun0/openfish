<script setup lang="ts">
/**
 * Prebuilt-interpreter mirror viewer, shared by two pages.
 *
 * The Python page shows it for `python-build-standalone` (uv) and the npm page
 * for the `nodejs.org/dist` mirror (nvm/fnm).  The two mirrors differ only in
 * their filenames, so the server flattens that difference into the
 * `BuildCatalog` shape (see `services/build_mirror.py`) and this component
 * renders it without knowing which ecosystem it is looking at.
 *
 * It is deliberately *presentational*: the page around it owns the header and
 * the dropdown that chooses between "packages" and "builds".
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { fetchBuildCatalog, type BuildCatalog, type BuildFile } from '@/api'
import { apiError } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'

const props = defineProps<{
  /** Which mirror to render: `python` (CPython/uv) or `node` (Node.js/nvm). */
  kind: 'python' | 'node'
}>()

const { t } = useI18n()

const catalog = ref<BuildCatalog | null>(null)
const loading = ref(true)
const query = ref('')
/** Empty means "every release". */
const release = ref('')

const releases = computed(() => catalog.value?.releases ?? [])

/** One flat, pageable list — a release is just another column on a row. */
const allFiles = computed<BuildFile[]>(() =>
  releases.value.flatMap((item) => item.files),
)

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return allFiles.value.filter((file) => {
    if (release.value && file.release_tag !== release.value) return false
    if (!needle) return true
    return [file.filename, file.label, file.platform, file.version]
      .some((value) => value.toLowerCase().includes(needle))
  })
})

const { page, pageSize, pageSizes, total, rows, reset } = usePagination(filtered)

// A new search or release is a new result set: start it from the first page.
watch([query, release], reset)

/** Where a client points its mirror environment variable. */
const mirrorUrl = computed(() => {
  const url = catalog.value?.mirror_url
  if (!url) return `${window.location.origin}/${props.kind}-builds/`
  // uv expects the trailing slash; nvm concatenates `/index.tab` itself.
  return props.kind === 'python' ? url : url.replace(/\/+$/, '')
})

const usage = computed(() => {
  if (props.kind === 'python') {
    return [
      `# ${t('build.usageCommentPython')}`,
      `export UV_PYTHON_INSTALL_MIRROR=${mirrorUrl.value}`,
      'uv python install 3.12',
    ].join('\n')
  }
  return [
    `# ${t('build.usageCommentNode')}`,
    `export NVM_NODEJS_ORG_MIRROR=${mirrorUrl.value}`,
    'nvm install 20',
    '',
    `# ${t('build.usageCommentFnm')}`,
    `export FNM_NODE_DIST_MIRROR=${mirrorUrl.value}`,
    'fnm install 20',
  ].join('\n')
})

const configEnv = computed(() =>
  props.kind === 'python' ? 'PYTHON_BUILDS_DIR' : 'NODE_BUILDS_DIR',
)

// A CPython archive has a build *variant* (`install_only_stripped`); a Node one
// has an archive *format* (`tar.xz`). Same column, different word.
const variantLabelKey = computed(() =>
  props.kind === 'python' ? 'build.variant' : 'build.format',
)

const alertTitleKey = computed(() => `build.${props.kind}ProxyTitle`)
const alertDescKey = computed(() => `build.${props.kind}ProxyDesc`)

async function load(): Promise<void> {
  loading.value = true
  try {
    catalog.value = await fetchBuildCatalog(props.kind)
  } catch (e) {
    ElMessage.error(apiError(e) || t(`${props.kind}.loadFailed`))
  } finally {
    loading.value = false
  }
}

function open(url: string | null | undefined): void {
  if (!url) return
  window.open(url, '_blank', 'noopener')
}

onMounted(load)
watch(() => props.kind, load)
</script>

<template>
  <div class="build">
    <el-alert
      v-if="catalog"
      type="info"
      show-icon
      :closable="false"
      :title="t(alertTitleKey)"
      :description="t(alertDescKey)"
    />

    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('build.setupTitle') }}</span>
      </template>
      <div class="setup">
        <div class="setup__item">
          <div class="setup__label">{{ t('build.mirrorLabel') }}</div>
          <el-tag class="mono" type="primary" effect="plain">{{ mirrorUrl }}</el-tag>
        </div>
        <div v-if="catalog?.index_url" class="setup__item">
          <div class="setup__label">{{ t('build.indexLabel') }}</div>
          <el-tag class="mono" type="info" effect="plain">{{ catalog.index_url }}</el-tag>
        </div>
        <div class="setup__item">
          <div class="setup__label">{{ t('build.envLabel') }}</div>
          <el-tag class="mono" type="info" effect="plain">{{ catalog?.env_var || '—' }}</el-tag>
        </div>
        <CodeBlock :label="t('build.usageLabel')" :code="usage" />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t('build.listTitle') }}</span>
          <span v-if="catalog" class="card-summary">
            {{
              t('build.summary', {
                releases: catalog.release_count,
                files: catalog.file_count,
                size: catalog.total_size_human,
              })
            }}
          </span>
        </div>
      </template>

      <div class="toolbar">
        <el-select
          v-model="release"
          class="toolbar__release"
          clearable
          :placeholder="t('build.allReleases')"
        >
          <el-option
            v-for="item in releases"
            :key="item.release_tag"
            :label="item.release_tag"
            :value="item.release_tag"
          />
        </el-select>
        <el-input
          v-model="query"
          class="toolbar__search"
          clearable
          :placeholder="t('build.filterPlaceholder')"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
      </div>

      <el-empty
        v-if="!loading && allFiles.length === 0"
        :description="
          catalog && !catalog.exists
            ? t('build.missingDesc', { root: catalog.root, env: configEnv })
            : t('build.empty')
        "
      />

      <template v-else>
        <el-table v-loading="loading" :data="rows" stripe>
          <el-table-column :label="t('build.name')" min-width="300">
            <template #default="{ row }">
              <div class="item">
                <span class="item__name mono">{{ row.filename }}</span>
                <span class="item__meta">{{ row.label }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column :label="t('build.version')" width="120">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.version }}</el-tag>
            </template>
          </el-table-column>

          <el-table-column prop="release_tag" :label="t('build.release')" width="130">
            <template #default="{ row }">
              <span class="mono">{{ row.release_tag }}</span>
            </template>
          </el-table-column>

          <el-table-column
            prop="platform"
            :label="t('build.platform')"
            min-width="200"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              <code>{{ row.platform }}</code>
            </template>
          </el-table-column>

          <el-table-column
            prop="variant"
            :label="t(variantLabelKey)"
            min-width="160"
            show-overflow-tooltip
          />

          <el-table-column prop="size" :label="t('build.size')" width="110" align="right">
            <template #default="{ row }">{{ row.size_human || '—' }}</template>
          </el-table-column>

          <el-table-column :label="t('common.actions')" width="190" align="right">
            <template #default="{ row }">
              <el-button size="small" type="primary" @click="open(row.download_url)">
                <el-icon><Download /></el-icon>
                <span class="btn-label">{{ t('build.download') }}</span>
              </el-button>
              <el-tooltip :content="t('build.checksum')">
                <el-button size="small" text @click="open(row.sha256_url)">
                  <el-icon><Document /></el-icon>
                </el-button>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>

        <TablePager
          v-model:page="page"
          v-model:page-size="pageSize"
          :page-sizes="pageSizes"
          :total="total"
        />
      </template>
    </el-card>
  </div>
</template>

<style scoped>
.build {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.card-summary {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.toolbar {
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.toolbar__release {
  width: 180px;
}

.toolbar__search {
  width: 260px;
}

.item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.item__name {
  font-weight: 500;
  word-break: break-all;
}

.item__meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
