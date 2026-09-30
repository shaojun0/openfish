/**
 * teardown 回归测试：验证「完全还原」这条**删文件**的路径。
 *
 * 全程在临时 `CONFIG_ROOT` / `DSH_HOME` 下运行（见插件 `CONFIG_ROOT` 的说明），
 * 不碰宿主机任何 `/etc` 文件，也不需要网络、DSH 或任何依赖：
 *
 *   node test/teardown.test.mjs      # 或 npm test
 *
 * 覆盖四件事：
 *   1. `POST /teardown` 收走 provider、默认模型、凭据、包源文件、状态文件，
 *      且**保留**被用户改过的配置文件、只报告清单外的旧残留；
 *   2. 插件已从 profile 移除时，dispose 会自动做同样的还原；
 *   3. 插件仍被 profile 声明（重启 / 热重载）时，dispose 不动任何东西；
 *   4. `autoMirrors: false` 的 `/apply` 只写 git 两个文件（用本地假平台跑完整
 *      应用流程）。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-intranet-verify-'))
const home = path.join(root, 'home')
const sys = path.join(root, 'sys')
fs.mkdirSync(home, { recursive: true })
fs.mkdirSync(sys, { recursive: true })
process.env.ENTERPRISE_INTRANET_CONFIG_ROOT = sys
process.env.DSH_HOME = home

const PLUGIN = new URL('../lib/index.js', import.meta.url).href
const mod = await import(PLUGIN)

const sha = (t) => crypto.createHash('sha256').update(t, 'utf8').digest('hex')
const P = {
  pip: path.join(sys, 'etc/pip.conf'),
  npmrc: path.join(sys, 'usr/local/etc/npmrc'),
  apt: path.join(sys, 'etc/apt/sources.list.d/enterprise-intranet.list'),
  profile: path.join(sys, 'etc/profile.d/enterprise-intranet.sh'),
  helper: path.join(sys, 'usr/local/bin/openfish-git-credential'),
  gitconfig: path.join(sys, 'etc/gitconfig'),
  keep: path.join(sys, 'etc/keep-me.conf'),
}
const STATE_FILE = path.join(home, 'enterprise-intranet.json')

let failures = 0
function check(label, cond, extra) {
  if (cond) console.log('  ✅ ' + label)
  else { failures++; console.log('  ❌ ' + label + (extra ? '  → ' + extra : '')) }
}

/** 造一份「已启用过」的状态 + 磁盘文件。modified=true 的那个文件被用户改过。 */
function seed({ modified = false, unmanaged = false } = {}) {
  const written = {}
  for (const [key, file] of Object.entries({ pip: P.pip, npmrc: P.npmrc, apt: P.apt, profile: P.profile, helper: P.helper, gitconfig: P.gitconfig })) {
    const content = `generated:${key}\n`
    fs.mkdirSync(path.dirname(file), { recursive: true })
    if (unmanaged && key === 'apt') {
      // 旧版本启用过、没进清单的残留：内容里有平台地址，但状态文件里没有它。
      fs.writeFileSync(file, 'deb [trusted=yes] http://127.0.0.1:1/debian/ ./\n')
      continue
    }
    fs.writeFileSync(file, modified && key === 'pip' ? 'user edited this\n' : content)
    written[file] = sha(content)
  }
  fs.mkdirSync(path.dirname(P.keep), { recursive: true })
  fs.writeFileSync(P.keep, 'belongs to the user\n')

  fs.writeFileSync(STATE_FILE, JSON.stringify({
    config: { enterpriseMode: false, platformUrl: 'http://127.0.0.1:1' },
    providers: ['intranet-deepseek-flash', 'intranet-deepseek-v4-pro'],
    routes: [
      { name: 'deepseek-flash', provider: 'openai', kind: 'chat' },
      { name: 'bge-m3-embedding', provider: 'openai', kind: 'embedding' },
    ],
    keysStored: ['deepseek-flash -> ENTERPRISE_INTRANET_KEY_DEEPSEEK_FLASH'],
    mirrorsWritten: written,
  }, null, 2) + '\n', 'utf8')
  return written
}

function makeCtx() {
  const settingsStore = { 'agent-default-model': { provider: 'intranet-deepseek-flash', model: 'deepseek-flash' } }
  const creds = new Map([
    ['ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret'],
    ['ENTERPRISE_INTRANET_KEY_DEEPSEEK_FLASH', 'sk-route'],
    ['ENTERPRISE_INTRANET_KEY_BGE_M3_EMBEDDING', 'sk-embed'],
  ])
  const state = { registrations: [], tap: null, disposer: null, replaced: [], unsetProviders: [], mutated: [] }
  const ctx = {
    webServer: {
      register(route) { state.registrations.push(route); return () => {} },
      tapIndex(fn) { state.tap = fn; return () => {} },
    },
    settings: {
      get(ns) { return settingsStore[ns] },
      async mutate(ns, ops) { state.mutated.push([ns, ops]); for (const op of ops) state.unsetProviders.push(op.path[1]) },
      async replace(ns, value) { state.replaced.push([ns, value]); settingsStore[ns] = value },
      describe() { return [] },
    },
    credentials: {
      async set(ref, value) { creds.set(ref, value) },
      async resolve(ref) { return creds.has(ref) ? { value: creds.get(ref), source: 'store' } : undefined },
      async unset(ref) { creds.delete(ref) },
    },
    effect(fn) { state.disposer = fn() },
    logger: { info() {}, warn() {} },
  }
  return { ctx, state, creds }
}

function csrfOf(state) {
  const html = state.tap('<body></body>')
  const m = /__DSH_INTRANET_BOOT__=(\{.*?\});/.exec(html)
  return JSON.parse(m[1]).csrf
}

function callEndpoint(state, pathname, csrf) {
  const route = state.registrations.find((r) => r.path === pathname)
  if (!route) throw new Error('no route ' + pathname)
  return new Promise((resolve) => {
    const res = { status: 0, body: '', writeHead(s) { this.status = s }, end(b) { this.body = b; resolve(this) } }
    route.handler({ method: 'POST', headers: { 'x-dsh-intranet-token': csrf }, on() {} }, res)
  })
}

// ── 场景 1：显式 /teardown（用户改过的文件必须保留） ────────────────────
console.log('\n场景 1 — POST /teardown')
seed({ modified: true, unmanaged: true })
const a = makeCtx()
mod.apply(a.ctx, {})
const res = await callEndpoint(a.state, '/dsh-intranet/teardown', csrfOf(a.state))
const payload = JSON.parse(res.body)
check('200 + ok:true', res.status === 200 && payload.ok === true, JSON.stringify(payload))
check('provider 已注销', a.state.unsetProviders.includes('intranet-deepseek-flash') && a.state.unsetProviders.includes('intranet-deepseek-v4-pro'), JSON.stringify(a.state.unsetProviders))
check('默认模型已还原', a.state.replaced.some(([ns, v]) => ns === 'agent-default-model' && v && Object.keys(v).length === 0))
check('凭据已删除（平台 key + 路由 key）', !a.creds.has('ENTERPRISE_INTRANET_PLATFORM_KEY') && !a.creds.has('ENTERPRISE_INTRANET_KEY_DEEPSEEK_FLASH'), [...a.creds.keys()].join(','))
check('凭据报告 removed 含平台 key', payload.credentials_removed.includes('ENTERPRISE_INTRANET_PLATFORM_KEY'))
check('生成的包源文件已删除', !fs.existsSync(P.npmrc) && !fs.existsSync(P.profile))
check('旧版本残留被报告、且没被误删', fs.existsSync(P.apt) && payload.mirrors_unmanaged.includes(P.apt), JSON.stringify(payload.mirrors_unmanaged))
check('git helper / gitconfig 已删除', !fs.existsSync(P.helper) && !fs.existsSync(P.gitconfig))
check('用户改过的 /etc/pip.conf 被保留', fs.existsSync(P.pip) && payload.mirrors_kept.includes(P.pip), JSON.stringify(payload.mirrors_kept))
check('状态文件已删除', !fs.existsSync(STATE_FILE))
check('完全没碰别人的配置文件', fs.existsSync(P.keep) && fs.readFileSync(P.keep, 'utf8') === 'belongs to the user\n')

// ── 场景 2：插件已从 profile 移除 → dispose 自动还原 ─────────────────────
console.log('\n场景 2 — 插件已被卸载时 dispose 自动还原')
seed()
const strippedProfile = path.join(home, 'profiles', 'web')
fs.mkdirSync(strippedProfile, { recursive: true })
fs.writeFileSync(path.join(strippedProfile, 'package.json'), JSON.stringify({ dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'] } } }))
const b = makeCtx()
mod.apply(b.ctx, {})
b.state.disposer()
await new Promise((r) => setTimeout(r, 300))
check('包源文件已被 dispose 删除', !fs.existsSync(P.npmrc) && !fs.existsSync(P.profile))
check('状态文件已被 dispose 删除', !fs.existsSync(STATE_FILE))

// ── 场景 3：插件仍被 profile 声明（重启/热重载）→ dispose 不动任何东西 ──
console.log('\n场景 3 — 插件仍在 profile 里时 dispose 不做破坏性还原')
seed()
const profileDir = path.join(home, 'profiles', 'web')
fs.mkdirSync(profileDir, { recursive: true })
fs.writeFileSync(path.join(profileDir, 'package.json'), JSON.stringify({ dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', 'dsh-plugin-enterprise-intranet'] } } }))
const c = makeCtx()
mod.apply(c.ctx, {})
c.state.disposer()
await new Promise((r) => setTimeout(r, 300))
check('包源文件仍在（重启不该变成卸载）', fs.existsSync(P.npmrc) && fs.existsSync(P.profile))
check('状态文件仍在', fs.existsSync(STATE_FILE))
check('凭据仍在', c.creds.has('ENTERPRISE_INTRANET_PLATFORM_KEY'))

// ── 场景 4：autoMirrors=false 时只写 git，不写包源文件 ───────────────────
// 用一个本地假平台跑完整的 /apply（插件只依赖 /api/v1/session 与
// /api/v1/models/resolved），验证文档承诺的 autoMirrors 语义。
console.log('\n场景 4 — autoMirrors:false 只写 git，不碰 pip/npm/apt/docker')
const http = await import('node:http')
const server = http.createServer((req, res) => {
  res.setHeader('content-type', 'application/json')
  if (req.url.startsWith('/api/v1/session')) {
    res.end(JSON.stringify({ authenticated: true, display_name: 'tester' }))
    return
  }
  if (req.url.startsWith('/api/v1/models/resolved')) {
    res.end(JSON.stringify({ routes: [{
      name: 'deepseek-flash', provider: 'openai', kind: 'chat', base_url: 'http://example.invalid',
      model: 'deepseek-flash', aliases: ['default'], path: '/v1/chat/completions', enabled: true, api_key: 'sk-upstream',
    }] }))
    return
  }
  res.statusCode = 404
  res.end('{}')
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const platform = `http://127.0.0.1:${server.address().port}`

for (const file of Object.values(P)) fs.rmSync(file, { force: true })
fs.rmSync(STATE_FILE, { force: true }) // 从干净状态开始：只验证 apply 写了什么
const d = makeCtx()
mod.apply(d.ctx, { platformUrl: platform, autoMirrors: false, autoGitCredential: true, enterpriseMode: false })
d.creds.set('ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret')
const applied = await callEndpoint(d.state, '/dsh-intranet/apply', csrfOf(d.state))
const appliedBody = JSON.parse(applied.body)
check('/apply 成功', appliedBody.ok === true && appliedBody.default_route === 'deepseek-flash', applied.body)
check('包源文件没被写', !fs.existsSync(P.pip) && !fs.existsSync(P.npmrc) && !fs.existsSync(P.apt) && !fs.existsSync(P.profile))
check('git helper / gitconfig 照写', fs.existsSync(P.helper) && fs.existsSync(P.gitconfig))
const manifest = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')).mirrorsWritten || {}
check('清单里只有 git 两个文件', !(P.pip in manifest) && !(P.npmrc in manifest) && (P.helper in manifest) && (P.gitconfig in manifest), JSON.stringify(Object.keys(manifest)))
check('镜像文件被 docker 等未涉及', !fs.existsSync(path.join(sys, 'etc/docker/daemon.json')))
server.close()

console.log('\n' + (failures ? `❌ ${failures} 项失败` : '✅ 全部通过') + `  (临时目录 ${root})`)
process.exit(failures ? 1 : 0)
