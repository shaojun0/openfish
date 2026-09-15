<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  createRole,
  deleteRole,
  fetchPermissions,
  fetchRoles,
  fetchUsers,
  grantUserRole,
  revokeUserRole,
  setRolePermissions,
  setUserSuperuser,
  type PermissionInfo,
  type RoleInfo,
  type UserInfo,
} from '@/api'
import { apiError } from '@/api/client'
import TablePager from '@/components/TablePager.vue'
import { usePagination } from '@/composables/usePagination'
import { useSessionStore } from '@/stores/session'
import { formatDate } from '@/utils/format'

const { t } = useI18n()
const session = useSessionStore()

const permissions = ref<PermissionInfo[]>([])
const roles = ref<RoleInfo[]>([])
const users = ref<UserInfo[]>([])
const loading = ref(true)

// All three lists are paged client-side; the roles and permissions tables are
// small, but the accounts list can grow with every OAuth2/basic user.
const {
  page: rolesPage,
  pageSize: rolesPageSize,
  pageSizes: rolesPageSizes,
  total: rolesTotal,
  rows: rolesRows,
} = usePagination(roles)

const {
  page: usersPage,
  pageSize: usersPageSize,
  pageSizes: usersPageSizes,
  total: usersTotal,
  rows: usersRows,
} = usePagination(users)

const {
  page: permissionsPage,
  pageSize: permissionsPageSize,
  pageSizes: permissionsPageSizes,
  total: permissionsTotal,
  rows: permissionsRows,
} = usePagination(permissions)

/** `admin:roles` is the grant that owns this whole screen. */
const canManage = computed(() => session.can('admin:roles'))

const permissionByCode = computed(() => {
  const map = new Map<string, PermissionInfo>()
  for (const perm of permissions.value) map.set(perm.code, perm)
  return map
})

/** Known module buckets get a translated heading; anything else shows as-is. */
const MODULES = ['package', 'build', 'key', 'admin']

function moduleLabel(module: string | null): string {
  if (!module) return t('access.module.other')
  return MODULES.includes(module) ? t(`access.module.${module}`) : module
}

interface PermissionGroup {
  module: string
  label: string
  items: PermissionInfo[]
}

/** Group the catalogue by `module` so the editor mirrors how the code is organised. */
const permissionGroups = computed<PermissionGroup[]>(() => {
  const groups = new Map<string, PermissionInfo[]>()
  for (const perm of permissions.value) {
    const key = perm.module ?? ''
    const bucket = groups.get(key) ?? []
    bucket.push(perm)
    groups.set(key, bucket)
  }
  return [...groups.entries()].map(([module, items]) => ({
    module,
    label: moduleLabel(module || null),
    items,
  }))
})

function permissionName(code: string): string {
  return permissionByCode.value.get(code)?.name ?? code
}

// ── Roles ────────────────────────────────────────────────────────────

const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive({ code: '', name: '', description: '' })

const editorVisible = ref(false)
const editorRole = ref<RoleInfo | null>(null)
const editingCodes = ref<string[]>([])
const saving = ref(false)

function openCreate(): void {
  createForm.code = ''
  createForm.name = ''
  createForm.description = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  const code = createForm.code.trim()
  if (!code) {
    ElMessage.warning(t('access.create.codeRequired'))
    return
  }
  creating.value = true
  try {
    await createRole({
      code,
      name: createForm.name.trim() || code,
      description: createForm.description.trim() || null,
    })
    ElMessage.success(t('access.roles.created'))
    createVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.roles.createFailed'))
  } finally {
    creating.value = false
  }
}

function openEditor(role: RoleInfo): void {
  editorRole.value = role
  editingCodes.value = [...role.permissions]
  editorVisible.value = true
}

async function savePermissions(): Promise<void> {
  const role = editorRole.value
  if (!role) return
  saving.value = true
  try {
    // The PUT replaces the whole set, so the complete list travels back.
    await setRolePermissions(role.id, [...editingCodes.value])
    ElMessage.success(t('access.roles.saved'))
    editorVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.roles.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function confirmDeleteRole(role: RoleInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('access.roles.deleteConfirm', { name: role.name }),
      t('access.roles.deleteTitle'),
      { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') },
    )
  } catch {
    return // user cancelled
  }
  try {
    await deleteRole(role.id)
    ElMessage.success(t('access.roles.deleted'))
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.roles.deleteFailed'))
  }
}

// ── Accounts ─────────────────────────────────────────────────────────

function grantableRoles(user: UserInfo): RoleInfo[] {
  return roles.value.filter((role) => !user.roles.includes(role.code))
}

async function grantRole(user: UserInfo, code: string): Promise<void> {
  try {
    await grantUserRole(user.id, code)
    ElMessage.success(t('access.accounts.granted', { role: code }))
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.accounts.grantFailed'))
  }
}

async function revokeRole(user: UserInfo, code: string): Promise<void> {
  try {
    await revokeUserRole(user.id, code)
    ElMessage.success(t('access.accounts.revoked', { role: code }))
    await load()
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.accounts.revokeFailed'))
  }
}

async function toggleSuperuser(user: UserInfo, value: boolean): Promise<void> {
  try {
    const result = await setUserSuperuser(user.id, value)
    user.is_superuser = result.is_superuser
    ElMessage.success(
      result.is_superuser
        ? t('access.accounts.superuserGranted')
        : t('access.accounts.superuserRevoked'),
    )
    await load()
  } catch (e) {
    // The server may refuse with a 403 (caller is not a superuser) or a 400
    // (that would remove the last one).  Show its own explanation verbatim.
    ElMessage.error(apiError(e) || t('access.accounts.superuserFailed'))
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [perm, role, user] = await Promise.all([
      fetchPermissions(),
      fetchRoles(),
      // One round trip up to the endpoint's cap; the accounts table pages in
      // the browser so the page stays interactive.
      fetchUsers(1000),
    ])
    permissions.value = perm
    roles.value = role
    users.value = user
  } catch (e) {
    ElMessage.error(apiError(e) || t('access.loadFailed'))
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page access-view" v-loading="loading">
    <div class="page__header">
      <div class="page__heading">
        <h1 class="page__title">{{ t('access.title') }}</h1>
        <p class="page__description">{{ t('access.description') }}</p>
      </div>
      <div class="page__actions">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          <span class="btn-label">{{ t('common.refresh') }}</span>
        </el-button>
        <el-button type="primary" :disabled="!canManage" @click="openCreate">
          <el-icon><Plus /></el-icon>
          <span class="btn-label">{{ t('access.roles.create') }}</span>
        </el-button>
      </div>
    </div>

    <!-- Roles ─────────────────────────────────────────────────────── -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t('access.roles.title') }}</span>
          <span class="card-hint">{{ t('access.roles.description') }}</span>
        </div>
      </template>
      <el-table
        :data="rolesRows"
        stripe
        row-class-name="role-row"
        :empty-text="t('access.roles.empty')"
        @row-click="openEditor"
      >
        <el-table-column :label="t('access.roles.code')" min-width="150">
          <template #default="{ row }"><span class="mono">{{ row.code }}</span></template>
        </el-table-column>
        <el-table-column :label="t('access.roles.name')" min-width="240">
          <template #default="{ row }">
            <div class="role-name">{{ row.name }}</div>
            <div class="role-badges">
              <el-tooltip v-if="row.is_builtin" :content="t('access.roles.builtinHint')">
                <el-tag size="small" type="info" effect="plain">
                  {{ t('access.roles.builtin') }}
                </el-tag>
              </el-tooltip>
              <el-tooltip v-if="row.auto_grant" :content="t('access.roles.autoGrantHint')">
                <el-tag size="small" type="success" effect="plain">
                  {{ t('access.roles.autoGrant') }}
                </el-tag>
              </el-tooltip>
              <el-tooltip v-if="row.is_anonymous_default" :content="t('access.roles.anonymousHint')">
                <el-tag size="small" type="warning" effect="plain">
                  {{ t('access.roles.anonymous') }}
                </el-tag>
              </el-tooltip>
            </div>
            <div v-if="row.description" class="muted">{{ row.description }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('access.roles.users')" width="100" align="right">
          <template #default="{ row }">{{ row.user_count }}</template>
        </el-table-column>
        <el-table-column :label="t('access.roles.permissions')" min-width="280">
          <template #default="{ row }">
            <div v-if="row.permissions.length" class="perm-tags">
              <el-tooltip
                v-for="code in row.permissions"
                :key="code"
                :content="permissionName(code)"
              >
                <el-tag size="small" effect="plain" class="mono">{{ code }}</el-tag>
              </el-tooltip>
            </div>
            <span v-else class="muted">{{ t('access.roles.noPermissions') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="120" align="right" fixed="right">
          <template #default="{ row }">
            <el-tooltip :content="t('access.roles.editPermissions')">
              <el-button size="small" text @click.stop="openEditor(row)">
                <el-icon><EditPen /></el-icon>
              </el-button>
            </el-tooltip>
            <el-tooltip
              :content="row.is_builtin ? t('access.roles.builtinHint') : t('common.delete')"
            >
              <span class="inline-btn">
                <el-button
                  size="small"
                  text
                  type="danger"
                  :disabled="row.is_builtin || !canManage"
                  @click.stop="confirmDeleteRole(row)"
                >
                  <el-icon><Delete /></el-icon>
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="rolesPage"
        v-model:page-size="rolesPageSize"
        :page-sizes="rolesPageSizes"
        :total="rolesTotal"
      />
    </el-card>

    <!-- Accounts ──────────────────────────────────────────────────── -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t('access.accounts.title') }}</span>
          <span class="card-hint">{{ t('access.accounts.description') }}</span>
        </div>
      </template>
      <el-table :data="usersRows" stripe :empty-text="t('access.accounts.empty')">
        <el-table-column :label="t('access.accounts.account')" min-width="200">
          <template #default="{ row }">
            <div class="mono">{{ row.external_id }}</div>
            <div v-if="row.display_name" class="muted">{{ row.display_name }}</div>
            <div v-if="!row.is_active" class="muted">
              {{ t('access.accounts.inactive') }}
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="provider" :label="t('access.accounts.provider')" width="120" />
        <el-table-column :label="t('access.accounts.roles')" min-width="300">
          <template #default="{ row }">
            <div class="role-cell">
              <el-tag
                v-for="code in row.roles"
                :key="code"
                size="small"
                effect="plain"
                closable
                :disable-transitions="true"
                :title="t('access.accounts.revokeRole', { role: code })"
                @close="revokeRole(row, code)"
              >
                {{ code }}
              </el-tag>
              <el-select
                v-if="grantableRoles(row).length"
                :key="`${row.id}:${row.roles.join(',')}`"
                class="role-grant"
                :model-value="null"
                size="small"
                :placeholder="t('access.accounts.addRole')"
                @change="grantRole(row, String($event))"
              >
                <el-option
                  v-for="role in grantableRoles(row)"
                  :key="role.code"
                  :label="role.name"
                  :value="role.code"
                />
              </el-select>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('access.accounts.lastLogin')" width="160">
          <template #default="{ row }">{{ formatDate(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('access.accounts.superuser')" width="150" align="center">
          <template #default="{ row }">
            <!-- Only an existing superuser may flip this; the server 403s anyone else. -->
            <el-tooltip
              :disabled="session.isSuperuser"
              :content="t('access.accounts.superuserHint')"
            >
              <span class="switch-wrap">
                <el-switch
                  :model-value="row.is_superuser"
                  :disabled="!session.isSuperuser"
                  @change="toggleSuperuser(row, Boolean($event))"
                />
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="usersPage"
        v-model:page-size="usersPageSize"
        :page-sizes="usersPageSizes"
        :total="usersTotal"
      />
    </el-card>

    <!-- Permission points ─────────────────────────────────────────── -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t('access.permissions.title') }}</span>
          <span class="card-hint">{{ t('access.permissions.description') }}</span>
        </div>
      </template>
      <el-table
        :data="permissionsRows"
        stripe
        :empty-text="t('access.permissions.empty')"
      >
        <el-table-column :label="t('access.permissions.code')" min-width="180">
          <template #default="{ row }"><span class="mono">{{ row.code }}</span></template>
        </el-table-column>
        <el-table-column prop="name" :label="t('access.permissions.name')" min-width="180" />
        <el-table-column :label="t('access.permissions.module')" width="120">
          <template #default="{ row }">{{ moduleLabel(row.module) }}</template>
        </el-table-column>
        <el-table-column :label="t('access.permissions.roles')" width="100" align="right">
          <template #default="{ row }">
            <el-tag :type="row.role_count === 0 ? 'danger' : 'info'" size="small" effect="plain">
              {{ row.role_count }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('access.permissions.status')" min-width="300">
          <template #default="{ row }">
            <el-tag v-if="row.role_count === 0" type="warning" size="small" effect="light">
              <el-icon><WarningFilled /></el-icon>
              <span class="tag-label">{{ t('access.permissions.orphan') }}</span>
            </el-tag>
            <el-tag v-else-if="row.stale" type="danger" size="small" effect="light">
              <el-icon><WarningFilled /></el-icon>
              <span class="tag-label">{{ t('access.permissions.stale') }}</span>
            </el-tag>
            <el-tag
              v-else-if="row.authenticated_pending"
              type="warning"
              size="small"
              effect="light"
            >
              <el-icon><WarningFilled /></el-icon>
              <span class="tag-label">{{ t('access.permissions.pending') }}</span>
            </el-tag>
            <span v-else class="muted">
              {{ t('access.permissions.held', { count: row.role_count }) }}
            </span>
          </template>
        </el-table-column>
      </el-table>

      <TablePager
        v-model:page="permissionsPage"
        v-model:page-size="permissionsPageSize"
        :page-sizes="permissionsPageSizes"
        :total="permissionsTotal"
      />
    </el-card>

    <!-- Role permission editor ────────────────────────────────────── -->
    <el-drawer
      v-model="editorVisible"
      :title="editorRole ? t('access.roles.editTitle', { code: editorRole.code }) : ''"
      size="640px"
    >
      <div v-if="editorRole" class="editor">
        <div class="editor__meta">
          <div class="editor__name">{{ editorRole.name }}</div>
          <p v-if="editorRole.description" class="editor__desc">{{ editorRole.description }}</p>
          <div class="role-badges">
            <el-tag v-if="editorRole.is_builtin" size="small" type="info" effect="plain">
              {{ t('access.roles.builtin') }}
            </el-tag>
            <el-tag v-if="editorRole.auto_grant" size="small" type="success" effect="plain">
              {{ t('access.roles.autoGrant') }}
            </el-tag>
            <el-tag
              v-if="editorRole.is_anonymous_default"
              size="small"
              type="warning"
              effect="plain"
            >
              {{ t('access.roles.anonymous') }}
            </el-tag>
          </div>
        </div>

        <el-alert type="info" :closable="false" show-icon class="editor__alert">
          {{ t('access.roles.saveHint') }}
        </el-alert>

        <div v-for="group in permissionGroups" :key="group.module" class="perm-group">
          <div class="perm-group__title">{{ group.label }}</div>
          <el-checkbox-group v-model="editingCodes" class="perm-group__items">
            <el-checkbox
              v-for="perm in group.items"
              :key="perm.code"
              :value="perm.code"
              class="perm-item"
            >
              <span class="perm-item__name">{{ perm.name }}</span>
              <span class="perm-item__code mono">{{ perm.code }}</span>
              <span v-if="perm.stale" class="perm-item__orphan">
                {{ t('access.permissions.staleShort') }}
              </span>
              <span v-else-if="perm.role_count === 0" class="perm-item__orphan">
                {{ t('access.permissions.orphanShort') }}
              </span>
              <span v-else-if="perm.authenticated_pending" class="perm-item__orphan">
                {{ t('access.permissions.pendingShort') }}
              </span>
            </el-checkbox>
          </el-checkbox-group>
        </div>
      </div>
      <template #footer>
        <el-button @click="editorVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="savePermissions">
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-drawer>

    <!-- Create role ───────────────────────────────────────────────── -->
    <el-dialog v-model="createVisible" :title="t('access.create.title')" width="480px">
      <el-form :model="createForm" label-position="top" @submit.prevent="submitCreate">
        <el-form-item :label="t('access.create.code')" required>
          <el-input
            v-model="createForm.code"
            :placeholder="t('access.create.codePlaceholder')"
            clearable
            @keyup.enter="submitCreate"
          />
          <p class="hint">{{ t('access.create.codeHint') }}</p>
        </el-form-item>
        <el-form-item :label="t('access.create.name')">
          <el-input
            v-model="createForm.name"
            :placeholder="t('access.create.namePlaceholder')"
            clearable
          />
        </el-form-item>
        <el-form-item :label="t('access.create.description')">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="2"
            :placeholder="t('access.create.descriptionPlaceholder')"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">
          {{ t('common.create') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card-title {
  font-weight: 600;
}

.card-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.card-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.btn-label {
  margin-left: 4px;
}

.page__actions {
  display: flex;
  gap: 8px;
}

.role-name {
  font-weight: 600;
}

:deep(.role-row) {
  cursor: pointer;
}

.role-badges {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.perm-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.role-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.role-grant {
  width: 150px;
}

.perm-grant {
  width: 100%;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.inline-btn {
  display: inline-flex;
}

.switch-wrap {
  display: inline-flex;
}

.tag-label {
  margin-left: 4px;
}

.editor__meta {
  margin-bottom: 12px;
}

.editor__name {
  font-size: 15px;
  font-weight: 600;
}

.editor__desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.editor__alert {
  margin-bottom: 14px;
}

.perm-group {
  margin-bottom: 16px;
}

.perm-group__title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.perm-group__items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.perm-item {
  height: auto;
  margin-right: 0;
  padding: 4px 0;
  align-items: flex-start;
}

.perm-item :deep(.el-checkbox__label) {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  line-height: 1.5;
}

.perm-item__name {
  font-weight: 500;
}

.perm-item__code {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.perm-item__orphan {
  font-size: 11px;
  color: var(--el-color-warning);
}

.hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
</style>
