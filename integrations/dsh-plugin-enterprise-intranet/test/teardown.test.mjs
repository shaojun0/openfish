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

function makeCtx(options = {}) {
  // 组合 base 层：0.2.0 的 replace() 是 mergeLayers(base, section)，所以桩里必须有一层
  // base，否则「还原」测不出真实落盘结果。
  const baseStore = { 'agent-default-model': { provider: 'deepseek', model: 'deepseek-chat' } }
  const settingsStore = {
    // 默认状态：默认模型已经指着本插件的 provider（= 停用时要还原的那种情形）。
    // 场景 5/7 用 initialDefault 换成别人的 provider，好让 apply 真的走到「采集基线」
    // 那条分支（否则 `readResolvedDefault()` 判定「已经是我们的」→ 基线为 null）。
    'agent-default-model': options.initialDefault
      ? { ...options.initialDefault }
      : { provider: 'intranet-deepseek-flash', model: 'deepseek-flash' },
  }
  const creds = new Map([
    ['ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret'],
    ['ENTERPRISE_INTRANET_KEY_DEEPSEEK_FLASH', 'sk-route'],
    ['ENTERPRISE_INTRANET_KEY_BGE_M3_EMBEDDING', 'sk-embed'],
  ])
  const state = {
    registrations: [], indexRows: [], disposer: null,
    replaced: [], unsetProviders: [], mutated: [], saved: [],
    failProviders: options.failProviders || [],
    // describe().user 的形状就是 DSH 0.2.0 给的那份；null = 用户没设过（走 base）。
    descriptorUser: options.descriptorUser === undefined ? null : options.descriptorUser,
  }
  const ctx = {
    webServer: {
      register(route) { state.registrations.push(route); return () => {} },
    },
    // 0.2.0 的结构化注入：插件用 ctx.on('webserver/index-inject', table => table.push(row))，
    // 不再是 tapIndex 的字符串改写。
    on(event, handler) {
      if (event === 'webserver/index-inject') state.indexRows.push(handler)
    },
    settings: {
      get(ns) { return settingsStore[ns] },
      async mutate(ns, ops) {
        // 0.2.0 起 dsh-llm-pi-ai 在写入时校验（config.d.ts 的 assertServiceable）：
        // 一条不可服务的路由会让**整批** mutate 抛错。桩要能复现，否则逐条兜底测不到。
        const bad = ops.filter((op) => state.failProviders.includes(op.path[1]))
        if (bad.length) throw new Error('assertServiceable: ' + bad.map((op) => op.path[1]).join(','))
        state.mutated.push([ns, ops])
        for (const op of ops) state.unsetProviders.push(op.path[1])
      },
      // 真实语义是 write(ns, (_current, base) => mergeLayers(base, section))：把组合 base
      // 物化进用户层，而不是清空用户层。
      async replace(ns, section) {
        const merged = { ...(baseStore[ns] || {}), ...(section || {}) }
        state.replaced.push([ns, section, merged])
        settingsStore[ns] = merged
      },
      describe() {
        return [{
          ns: 'agent-default-model',
          value: settingsStore['agent-default-model'],
          user: state.descriptorUser,
        }]
      },
    },
    credentials: {
      async set(ref, value) { creds.set(ref, value) },
      async resolve(ref) { return creds.has(ref) ? { value: creds.get(ref), source: 'store' } : undefined },
      async unset(ref) { creds.delete(ref) },
    },
    effect(fn) { state.disposer = fn() },
    logger: { info() {}, warn() {} },
  }
  if (options.withDefaultModelService) {
    // DSH 0.2.0 的正式写入路径（dsh-agent-default-model）；提供了它就该被优先使用。
    ctx.agentDefaultModel = {
      currentSelection() { return { ...settingsStore['agent-default-model'] } },
      async saveSelection(next) {
        state.saved.push(next)
        settingsStore['agent-default-model'] = { ...next }
      },
    }
  }
  return { ctx, state, creds, settings: settingsStore, base: baseStore }
}

/** 跑一遍注入链，取出 boot 行里的 csrf（结构化注入，不再是字符串改写）。 */
function bootRowsOf(state) {
  const table = []
  for (const handler of state.indexRows) handler(table)
  return table
}

function csrfOf(state) {
  const row = bootRowsOf(state).find((r) => r && r.kind === 'global' && r.name === '__DSH_INTRANET_BOOT__')
  if (!row) throw new Error('未注入 __DSH_INTRANET_BOOT__ 行')
  return row.value.csrf
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
// 基线为空 → 走 settings().replace(ns, {})。0.2.0 的 replace 是 mergeLayers(base, section)，
// 所以断言必须落在**落盘结果**（= 组合 base），而不是调用参数。
check('默认模型已还原到组合 base',
  JSON.stringify(a.settings['agent-default-model']) === JSON.stringify(a.base['agent-default-model']),
  JSON.stringify(a.settings['agent-default-model']))
check('还原用的是空分节写法（基线为空）',
  a.state.replaced.some(([ns, section]) => ns === 'agent-default-model' && section && Object.keys(section).length === 0))
{
  const rows = bootRowsOf(a.state)
  check('注入了 global boot 行', rows.some((r) => r.kind === 'global' && r.name === '__DSH_INTRANET_BOOT__'), JSON.stringify(rows))
  check('注入了 panel 脚本行', rows.some((r) => r.kind === 'script-src' && r.src === '/dsh-intranet/panel.js'), JSON.stringify(rows))
}
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
    res.end(JSON.stringify({ routes: [
      {
        name: 'deepseek-flash', provider: 'openai', kind: 'chat', base_url: 'http://example.invalid',
        model: 'deepseek-flash', aliases: ['default'], path: '/v1/chat/completions', enabled: true, api_key: 'sk-upstream',
      },
      {
        name: 'deepseek-v4-pro', provider: 'openai', kind: 'chat', base_url: 'http://example.invalid',
        model: 'deepseek-v4-pro', aliases: [], path: '/v1/chat/completions', enabled: true, api_key: 'sk-upstream-2',
      },
    ] }))
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
// ── 场景 6：0.2.0 的写入校验拒绝一条路由时，逐条重试不让整批落空 ──────────
console.log('\n场景 6 — assertServiceable 拒绝一条路由时不拖垮整批')
for (const file of Object.values(P)) fs.rmSync(file, { force: true })
fs.rmSync(STATE_FILE, { force: true })
const g = makeCtx({ failProviders: ['intranet-deepseek-v4-pro'], withDefaultModelService: true })
mod.apply(g.ctx, { platformUrl: platform, autoMirrors: true, enterpriseMode: false })
g.creds.set('ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret')
const retried = JSON.parse((await callEndpoint(g.state, '/dsh-intranet/apply', csrfOf(g.state))).body)
check('/apply 仍然成功（坏路由不该让整次启用落空）', retried.ok === true, JSON.stringify(retried))
check('好路由已注册', g.state.unsetProviders.includes('intranet-deepseek-flash'), JSON.stringify(g.state.unsetProviders))
check('坏路由被报出来、且带上路由名', (retried.providers_skipped || []).some((x) => x.provider === 'intranet-deepseek-v4-pro' && x.route === 'deepseek-v4-pro'), JSON.stringify(retried.providers_skipped))
check('坏路由的凭据被撤掉（不留孤儿）', !g.creds.has('ENTERPRISE_INTRANET_KEY_DEEPSEEK_V4_PRO'), [...g.creds.keys()].join(','))
check('确实走了逐条重试（批调用抛错后仍有单条 mutate）', g.state.mutated.some(([, ops]) => ops.length === 1), JSON.stringify(g.state.mutated.map(([, ops]) => ops.length)))
check('默认模型经 agentDefaultModel.saveSelection() 写入', g.state.saved.some((v) => v.provider === 'intranet-deepseek-flash'), JSON.stringify(g.state.saved))

// ── 场景 5：基线来自 describe() 的 user 层（0.2.0 的 describe 形状） ──────
// 基线是在 **apply** 时采集的，所以必须先 /apply 再 /teardown；而初始默认模型得是
// 「别人的」provider，apply 才会走采集分支。
console.log('\n场景 5 — describe().user 非空时按 user 层采集并还原')
for (const file of Object.values(P)) fs.rmSync(file, { force: true })
fs.rmSync(STATE_FILE, { force: true })
const e = makeCtx({
  descriptorUser: { provider: 'deepseek', model: 'deepseek-reasoner' },
  initialDefault: { provider: 'deepseek', model: 'deepseek-chat' },
})
mod.apply(e.ctx, { platformUrl: platform, autoMirrors: true, enterpriseMode: false })
e.creds.set('ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret')
await callEndpoint(e.state, '/dsh-intranet/apply', csrfOf(e.state))
const eCaptured = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')).baselineDefaultModel
check('基线取自 describe().user（而不是解析值/空）',
  JSON.stringify(eCaptured && eCaptured.section) === JSON.stringify({ provider: 'deepseek', model: 'deepseek-reasoner' }),
  JSON.stringify(eCaptured))
const eBody = JSON.parse((await callEndpoint(e.state, '/dsh-intranet/teardown', csrfOf(e.state))).body)
check('200 + ok:true', eBody.ok === true, JSON.stringify(eBody))
check('还原写入的是 user 层那份选择',
  JSON.stringify(e.settings['agent-default-model']) === JSON.stringify({ provider: 'deepseek', model: 'deepseek-reasoner' }),
  JSON.stringify(e.settings['agent-default-model']))

// ── 场景 7：有 agentDefaultModel 服务时优先走 saveSelection() ─────────────
console.log('\n场景 7 — 有 agentDefaultModel 服务时优先 saveSelection()')
for (const file of Object.values(P)) fs.rmSync(file, { force: true })
fs.rmSync(STATE_FILE, { force: true })
const f = makeCtx({
  withDefaultModelService: true,
  descriptorUser: { provider: 'deepseek', model: 'deepseek-reasoner' },
  initialDefault: { provider: 'deepseek', model: 'deepseek-chat' },
})
mod.apply(f.ctx, { platformUrl: platform, autoMirrors: true, enterpriseMode: false })
f.creds.set('ENTERPRISE_INTRANET_PLATFORM_KEY', 'cpypi_secret')
await callEndpoint(f.state, '/dsh-intranet/apply', csrfOf(f.state))
check('apply 经 saveSelection() 写入',
  f.state.saved.some((v) => v.model === 'deepseek-flash'), JSON.stringify(f.state.saved))
await callEndpoint(f.state, '/dsh-intranet/teardown', csrfOf(f.state))
check('全程未回退到 settings().replace()', f.state.replaced.length === 0, JSON.stringify(f.state.replaced))
check('还原经 saveSelection() 写入基线',
  f.state.saved.some((v) => v.model === 'deepseek-reasoner'), JSON.stringify(f.state.saved))
check('落盘的就是基线那份选择',
  JSON.stringify(f.settings['agent-default-model']) === JSON.stringify({ provider: 'deepseek', model: 'deepseek-reasoner' }),
  JSON.stringify(f.settings['agent-default-model']))

server.close()

console.log('\n' + (failures ? `❌ ${failures} 项失败` : '✅ 全部通过') + `  (临时目录 ${root})`)
process.exit(failures ? 1 : 0)
