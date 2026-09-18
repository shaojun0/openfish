/**
 * dsh-plugin-enterprise-intranet — DSH「企业内网模式」宿主侧插件。
 *
 * 它把 DSH 接到企业制品/模型平台（openfish，默认 https://47.97.243.86:9443）：
 *
 *  1. **API key 是必选项。** 没有 key 时不会启用企业内网模式，面板会把用户
 *     送到平台的设备授权页（`/device`）完成登录；登录成功后平台签发一枚 API key
 *     并由本插件自动收下（无需复制粘贴），随后自动启用模式。
 *  2. **自动接入模型路由的默认模型。** 用 key 调用
 *     `GET /api/v1/models/resolved` 拿到路由表（含各上游 api_key），为每条启用的
 *     路由注册一个 `llm-pi-ai` provider，并把 `agent-default-model` 指向
 *     `aliases` 含 `default` 的那条路由。
 *  3. **包源切换。** 把 pip / npm / docker / debian 指向平台的内网镜像。
 *  4. **git 仓库凭据。** 生成一个 git credential helper（`/etc/gitconfig` 指向
 *     它），对 `https://<平台>/git/<owner>/<name>.git` 用平台 key 换一张短期
 *     Forgejo 票 —— push 由 Forgejo 校验，平台 key 本身过不了。
 *  5. **工具与文档。** 面板里列出平台 `/api/v1/tools` 与 `/api/v1/docs` 目录。
 *
 * 设计约束（为什么是这样写的）
 * ---------------------------
 * * **不 import 任何 `@deepseek-ai/*`。** 插件装在 profile 的 node_modules 下，
 *   而 DSH 内部包是嵌套在 `@deepseek-ai/dsh/node_modules` 里的，从插件位置解析
 *   不到。所有协作都通过 `ctx` 上的服务（webServer / settings / credentials）进行，
 *   这是第三方 DSH 插件能稳定工作的唯一姿势。
 * * **平台用自签证书**（SAN = IP），Node 的 fetch 会拒绝它，所以这里用
 *   `node:https` 直连并允许通过 `verifyTls` / `caFile` 控制校验，而不是把
 *   `NODE_TLS_REJECT_UNAUTHORIZED=0` 塞进进程环境。
 * * **插件路由不受 DSH 应用层会话保护**（webServer 路由先于应用鉴权匹配），所以
 *   这里用一个只在 index HTML 里下发的 per-process CSRF token 保护全部读写端点，
 *   并且**从不**把 API key 的值返回给浏览器。
 *
 * @module dsh-plugin-enterprise-intranet
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import https from 'node:https'
import http from 'node:http'
import crypto from 'node:crypto'
import { fileURLToPath } from 'node:url'

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
const STATE_FILE = path.join(DSH_HOME, 'enterprise-intranet.json')
const PANEL_JS_FILE = path.join(PACKAGE_ROOT, 'lib', 'panel.js')

/** 平台 API key 存进 DSH 凭据服务时用的引用名（POSIX 标识符）。 */
export const PLATFORM_KEY_REF = 'ENTERPRISE_INTRANET_PLATFORM_KEY'

/** 每条模型路由的上游 key 引用前缀：`ENTERPRISE_INTRANET_KEY_<SLUG>`。 */
export const ROUTE_KEY_REF_PREFIX = 'ENTERPRISE_INTRANET_KEY_'

/** `llm-pi-ai` / `agent-default-model` 是 DSH 已注册的设置命名空间。 */
const LLM_NS = 'llm-pi-ai'
const DEFAULT_MODEL_NS = 'agent-default-model'

const ROUTE_PREFIX = '/dsh-intranet'

const DEFAULTS = {
  platformUrl: 'https://47.97.243.86:9443',
  enterpriseMode: false,
  autoMirrors: true,
  autoGitCredential: true,
  defaultAlias: 'default',
  verifyTls: false,
  caFile: '',
  requestTimeoutMs: 20000,
  apiKey: '',
}

/** 插件生成的 git credential helper 路径（gitconfig 里指向它）。 */
const GIT_HELPER_PATH = '/usr/local/bin/openfish-git-credential'

/** openfish 的 provider 取值 → pi-ai 的 wire protocol。 */
const API_BY_PROVIDER = {
  openai: 'openai-completions',
  anthropic: 'anthropic-messages',
}

const JSON_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
}

// ── 小工具 ────────────────────────────────────────────────────────────

function slugify(value) {
  const slug = String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return slug || 'route'
}

function refForRoute(name) {
  const slug = slugify(name).replace(/-/g, '_').toUpperCase()
  return ROUTE_KEY_REF_PREFIX + (slug || 'ROUTE')
}

function nowIso() {
  return new Date().toISOString()
}

function readState() {
  try {
    const raw = fs.readFileSync(STATE_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeState(state) {
  try {
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true })
    const tmp = `${STATE_FILE}.${process.pid}.tmp`
    fs.writeFileSync(tmp, JSON.stringify(state, null, 2) + '\n', 'utf8')
    fs.renameSync(tmp, STATE_FILE)
    return true
  } catch {
    return false
  }
}

/** 原子写一个配置文件，返回结果而不是抛错（容器里有些路径可能只读）。 */
function writeConfigFile(file, content, mode = null) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    const tmp = `${file}.${process.pid}.tmp`
    fs.writeFileSync(tmp, content, 'utf8')
    fs.chmodSync(tmp, mode === null || mode === undefined ? 0o644 : mode)
    fs.renameSync(tmp, file)
    return { file, ok: true }
  } catch (err) {
    return { file, ok: false, error: String((err && err.message) || err) }
  }
}

function readBody(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error('request body too large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function sendJson(res, status, payload) {
  res.writeHead(status, JSON_HEADERS)
  res.end(JSON.stringify(payload))
}

/**
 * 一个 JSON 请求，返回 `{status, body, raw}`。用 node:https 以支持自签证书。
 * @param {string} url
 * @param {{method?: string, headers?: object, body?: any, timeoutMs?: number,
 *          verifyTls?: boolean, caFile?: string}} options
 */
function requestJson(url, options = {}) {
  const {
    method = 'GET',
    headers = {},
    body,
    timeoutMs = DEFAULTS.requestTimeoutMs,
    verifyTls = false,
    caFile = '',
  } = options

  return new Promise((resolve, reject) => {
    let target
    try {
      target = new URL(url)
    } catch (err) {
      reject(new Error(`平台地址不合法：${url}`))
      return
    }
    const isHttps = target.protocol === 'https:'
    const transport = isHttps ? https : http

    const payload = body === undefined || body === null
      ? null
      : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8')

    const requestOptions = {
      method,
      hostname: target.hostname,
      port: target.port || (isHttps ? 443 : 80),
      path: `${target.pathname}${target.search}`,
      headers: {
        Accept: 'application/json',
        ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': String(payload.length) } : {}),
        ...headers,
      },
    }
    if (isHttps) {
      if (caFile) {
        try {
          requestOptions.ca = fs.readFileSync(caFile)
        } catch (err) {
          reject(new Error(`读取 caFile 失败：${caFile}（${(err && err.message) || err}）`))
          return
        }
      } else if (verifyTls !== true) {
        requestOptions.rejectUnauthorized = false
      }
    }

    const req = transport.request(requestOptions, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8')
        let parsed = null
        try {
          parsed = raw ? JSON.parse(raw) : null
        } catch {
          parsed = null
        }
        resolve({ status: res.statusCode || 0, body: parsed, raw })
      })
    })
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`请求超时（${timeoutMs}ms）：${url}`))
    })
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

// ── 插件本体 ──────────────────────────────────────────────────────────

export const name = 'enterprise-intranet'

/**
 * DSH 服务依赖。`settings` 用来写 `llm-pi-ai` / `agent-default-model`，
 * `credentials` 用来保管平台 key 与各路由的上游 key，`webServer` 用来挂
 * 面板与 JSON 端点。
 */
export const inject = ['webServer', 'settings', 'credentials']

export function apply(ctx, config) {
  const fileConfig = config && typeof config === 'object' ? config : {}
  const state = readState()
  const merged = { ...DEFAULTS, ...(state.config || {}), ...fileConfig }

  /** per-process CSRF token：只在 index HTML 里下发，保护插件端点。 */
  const csrf = crypto.randomBytes(24).toString('base64url')

  /** 进行中的设备授权登录：login_id -> {deviceCode, userCode, expiresAt, interval, ...} */
  const logins = new Map()

  const disposers = []

  const settings = () => ctx.settings
  const credentials = () => ctx.credentials

  /** 本插件注册进 `llm-pi-ai` 的 provider id 前缀（见 registerProviders）。 */
  const PROVIDER_PREFIX = 'intranet-'

  /** 一个可用的设置分节：非 null、非数组、且有键。 */
  function isSection(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
  }

  function isIntranetProvider(provider) {
    return String(provider || '').startsWith(PROVIDER_PREFIX)
  }

  /** 读 `agent-default-model` 当前解析值（provider/model/…），读不到返回 null。 */
  function readResolvedDefault() {
    try {
      const value = settings().get?.(DEFAULT_MODEL_NS)
      return value && typeof value === 'object' ? value : null
    } catch {
      return null
    }
  }

  /**
   * 采集「用户层」的 `agent-default-model`，作为停用时还原的基线。
   *
   * 优先用 `describe()` 的 user 层而不是 `get()` 的解析值：user 层为空恰好表示
   * 「用户没设过、默认模型来自组合 base」，还原时清空用户层就能继承回 base，
   * 而不是把解析后的值固化成一条本不存在的用户覆盖。`describe()` 在旧版 DSH 上
   * 没有时退回解析值。
   */
  function captureDefaultModelLayer() {
    try {
      const descriptors = typeof settings().describe === 'function' ? settings().describe() : null
      const descriptor = Array.isArray(descriptors)
        ? descriptors.find((d) => d && d.ns === DEFAULT_MODEL_NS)
        : null
      if (descriptor) return isSection(descriptor.user) ? descriptor.user : null
    } catch {
      // describe() 不可用 —— 下面的 get() 兜底。
    }
    const resolved = readResolvedDefault()
    return isSection(resolved) ? resolved : null
  }

  /**
   * 把状态文件里的基线归一成可直接交给 `settings().replace()` 的分节。
   * 兼容两种形状：本版写入的 `{section, capturedAt}`，以及旧版直接存下来的解析值。
   * 没有可还原的内容时返回 null。
   */
  function normalizeBaseline(stored) {
    if (!stored || typeof stored !== 'object') return null
    const section = Object.prototype.hasOwnProperty.call(stored, 'section') ? stored.section : stored
    return isSection(section) ? { ...section } : null
  }

  function configNow() {
    return { ...merged, ...(readState().config || {}) }
  }

  function persistConfig(patch) {
    const current = readState()
    const next = {
      ...current,
      config: { ...(current.config || {}), ...patch },
    }
    writeState(next)
    Object.assign(merged, patch)
  }

  // ── API key 解析：凭据服务 → 环境变量 → 配置文件 ──────────────────

  async function resolvePlatformKey() {
    const cfg = configNow()
    if (cfg.apiKey) return { key: cfg.apiKey, source: 'config' }
    try {
      const resolved = await credentials().resolve(PLATFORM_KEY_REF)
      if (resolved && resolved.value) return { key: resolved.value, source: resolved.source || 'credentials' }
    } catch {
      // 凭据服务不可用/未配置时继续往下试。
    }
    const fromEnv = process.env[PLATFORM_KEY_REF]
    if (fromEnv) return { key: fromEnv, source: 'env' }
    return { key: '', source: null }
  }

  async function storePlatformKey(key) {
    await credentials().set(PLATFORM_KEY_REF, key)
    persistConfig({ apiKey: '' }) // 明文只留在凭据文档里，状态文件不存 key
  }

  // ── 平台调用 ────────────────────────────────────────────────────────

  function platformOptions(extra = {}) {
    const cfg = configNow()
    return {
      timeoutMs: cfg.requestTimeoutMs,
      verifyTls: cfg.verifyTls === true,
      caFile: cfg.caFile || '',
      ...extra,
    }
  }

  function platformUrl(pathname) {
    const base = String(configNow().platformUrl || DEFAULTS.platformUrl).replace(/\/+$/, '')
    return `${base}${pathname}`
  }

  async function platformGet(pathname, key) {
    const headers = key ? { Authorization: `Bearer ${key}` } : {}
    return requestJson(platformUrl(pathname), platformOptions({ method: 'GET', headers }))
  }

  async function fetchResolvedRoutes(key) {
    const res = await platformGet('/api/v1/models/resolved', key)
    if (res.status === 401 || res.status === 403) {
      const err = new Error('平台拒绝了该 API key（401/403）。请在面板里重新登录获取。')
      err.code = 'API_KEY_REJECTED'
      throw err
    }
    if (res.status !== 200 || !res.body) {
      const err = new Error(`读取模型路由失败：HTTP ${res.status} ${String(res.raw || '').slice(0, 200)}`)
      err.code = 'ROUTES_UNAVAILABLE'
      throw err
    }
    return Array.isArray(res.body.routes) ? res.body.routes : []
  }

  async function fetchSession(key) {
    const res = await platformGet('/api/v1/session', key)
    return res.status === 200 ? res.body : null
  }

  /**
   * 校验一枚 api-key 是否被平台接受，并尽量取回账号名。
   *
   * 先问 `/api/v1/session`，但它不是唯一的真相来源：某些部署会在反代层给
   * `/api/v1/session` 加 Basic 门（我们的 ECS nginx 早期就是这样），于是
   * 一个完全有效的 Bearer key 也会拿到 401。所以失败时退回
   * `GET /api/v1/models/resolved` —— 那个端点本来就是插件真正要用的能力，
   * 它 200 就说明这枚 key 至少拥有 model:resolve，足够启用企业内网模式。
   */
  async function verifyKey(key) {
    const session = await fetchSession(key).catch(() => null)
    if (session && session.authenticated === true) {
      return { ok: true, user: session.display_name || session.user || null, via: 'session' }
    }
    try {
      await fetchResolvedRoutes(key)
      return { ok: true, user: null, via: 'models/resolved' }
    } catch (err) {
      const code = (err && err.code) || 'ERROR'
      return {
        ok: false,
        via: 'models/resolved',
        code: code === 'API_KEY_REJECTED' ? 'API_KEY_REJECTED' : 'PLATFORM_UNREACHABLE',
        error: String((err && err.message) || err),
      }
    }
  }

  // ── 设备授权登录 ────────────────────────────────────────────────────

  async function startLogin() {
    const res = await requestJson(platformUrl('/api/v1/device/code'), platformOptions({
      method: 'POST',
      body: {},
    }))
    if (res.status !== 200 || !res.body || !res.body.device_code) {
      throw new Error(`无法发起企业登录：HTTP ${res.status} ${String(res.raw || '').slice(0, 200)}`)
    }
    const loginId = crypto.randomBytes(12).toString('hex')
    logins.set(loginId, {
      deviceCode: res.body.device_code,
      userCode: res.body.user_code,
      verificationUri: res.body.verification_uri,
      verificationUriComplete: res.body.verification_uri_complete,
      interval: Number(res.body.interval) || 2,
      expiresAt: Date.now() + (Number(res.body.expires_in) || 900) * 1000,
      startedAt: nowIso(),
    })
    return {
      login_id: loginId,
      user_code: res.body.user_code,
      verification_uri: res.body.verification_uri,
      verification_uri_complete: res.body.verification_uri_complete,
      expires_in: res.body.expires_in,
      interval: res.body.interval,
    }
  }

  async function pollLogin(loginId) {
    const pending = logins.get(loginId)
    if (!pending) return { status: 'unknown', message: '登录会话不存在或已结束，请重新发起。' }
    if (Date.now() > pending.expiresAt) {
      logins.delete(loginId)
      return { status: 'expired', message: '授权码已过期，请重新发起。' }
    }
    const res = await requestJson(platformUrl('/api/v1/device/token'), platformOptions({
      method: 'POST',
      body: { device_code: pending.deviceCode },
    }))
    if (res.status === 200 && res.body && res.body.api_key) {
      logins.delete(loginId)
      await storePlatformKey(res.body.api_key)
      persistConfig({ enterpriseMode: true })
      let applied = null
      let applyError = null
      try {
        applied = await applyEnterpriseMode()
      } catch (err) {
        applyError = String((err && err.message) || err)
      }
      return {
        status: 'approved',
        user: res.body.display_name || res.body.user || null,
        key_prefix: res.body.key_prefix || null,
        enterprise_mode: true,
        applied,
        apply_error: applyError,
      }
    }
    const code = res.body && res.body.error
    if (code === 'expired_token') {
      logins.delete(loginId)
      return { status: 'expired', message: '授权码已过期，请重新发起。' }
    }
    return {
      status: 'pending',
      user_code: pending.userCode,
      interval: pending.interval,
      message: (res.body && res.body.error_description) || '等待浏览器中完成登录授权…',
    }
  }

  // ── 模型路由 → llm-pi-ai providers ──────────────────────────────────

  async function registerProviders(routes) {
    const cfg = configNow()
    const providers = {}
    const stored = []
    for (const route of routes) {
      if (!route || route.enabled === false) continue
      const api = API_BY_PROVIDER[route.provider]
      if (!api) continue // mineru 等非对话模型只在面板里展示，不注册成 LLM provider
      const routeKey = `${'intranet-'}${slugify(route.name)}`
      const modelId = route.model || route.name
      const profile = {
        displayName: `企业内网 · ${route.name}`,
        api,
        baseURL: route.base_url,
        models: [{ id: modelId, name: route.description ? `${modelId}（${route.description}）` : modelId }],
      }
      if (route.api_key) {
        const ref = refForRoute(route.name)
        await credentials().set(ref, route.api_key)
        profile.apiKeyEnv = ref
        stored.push({ route: route.name, ref })
      }
      providers[routeKey] = profile
    }

    // 用 mutate 的路径写法逐个 provider 写入，避免覆盖用户/其它插件已有的 provider。
    const ops = Object.entries(providers).map(([key, value]) => ({
      op: 'set',
      path: ['providers', key],
      value,
    }))
    if (ops.length && typeof settings().mutate === 'function') {
      await settings().mutate(LLM_NS, ops)
    } else if (ops.length) {
      await settings().update(LLM_NS, { providers })
    }
    return { providers: Object.keys(providers), keysStored: stored }
  }

  async function unregisterProviders(providerIds) {
    if (!providerIds || !providerIds.length) return
    const ops = providerIds.map((id) => ({ op: 'unset', path: ['providers', id] }))
    if (typeof settings().mutate === 'function') {
      await settings().mutate(LLM_NS, ops)
    }
  }

  function pickDefaultRoute(routes) {
    const alias = String(configNow().defaultAlias || 'default').toLowerCase()
    const enabled = routes.filter((r) => r && r.enabled !== false && API_BY_PROVIDER[r.provider])
    if (!enabled.length) return null
    const byAlias = enabled.find((r) =>
      Array.isArray(r.aliases) && r.aliases.some((a) => String(a).toLowerCase() === alias),
    )
    return byAlias || enabled[0]
  }

  // ── 包源（pip / npm / docker / apt）─────────────────────────────────

  /**
   * 把 `__token__:<api-key>@` 嵌进 URL 的 authority 部分。
   *
   * 必须用 URL 解析器而不是字符串 replace：早先写成
   * `base.replace('://', '://' + auth)` 会得到
   * `https://__token__:KEY@47.97.243.86:944347.97.243.86:9443/simple/`
   * —— 原 host 没有被替换掉，pip 直接解析失败。`URL` 还会顺带把 key 里的
   * 特殊字符按 percent-encoding 处理好。
   */
  function credentialedUrl(base, pathname, key) {
    const url = new URL(pathname, base.endsWith('/') ? base : base + '/')
    if (key) {
      url.username = '__token__'
      url.password = key
    }
    return url.toString()
  }

  /**
   * 生成 git credential helper（Node 脚本，只用内置模块）。
   *
   * 它按 git credential 协议从 stdin 读 `protocol/host/path`，从 `path` 解析
   * `<owner>/<name>`，用平台 API key 调平台
   * `GET /api/v1/repos/<owner>/<name>/git-credential` 换一张**短期 Forgejo 票**
   * （openfish 用 admin token 兑换，见 S6.md），再把 `username/password` 输出给
   * git。失败一律**静默退出**（stdout 为空、exit 0），只在 stderr 留一行原因：
   * 平台不可用时不能让 git 卡死。
   */
  function gitCredentialHelperSource(key) {
    const platform = JSON.stringify(String(configNow().platformUrl || DEFAULTS.platformUrl).replace(/\/+$/, ''))
    const apiKey = JSON.stringify(String(key || ''))
    const verifyTls = configNow().verifyTls === true
    const caFile = JSON.stringify(String(configNow().caFile || ''))
    // The helper must only ever answer for *this* platform.  Without the host
    // check, git would hand the minted Forgejo token to any https host whose
    // path looks like <owner>/<name>.
    const platformHost = (() => {
      try { return new URL(configNow().platformUrl || DEFAULTS.platformUrl).host.toLowerCase() } catch { return '' }
    })()
    const platformHostLiteral = JSON.stringify(platformHost)
    return `#!/usr/bin/env node
// 由 dsh-plugin-enterprise-intranet 自动生成，请勿手改。
// openfish git credential helper：把平台 API key 换成短期 Forgejo 票。
'use strict'
const fs = require('node:fs')
const http = require('node:http')
const https = require('node:https')

const PLATFORM = ${platform}
const PLATFORM_HOST = ${platformHostLiteral}
const API_KEY = ${apiKey}
const VERIFY_TLS = ${verifyTls ? 'true' : 'false'}
const CA_FILE = ${caFile}

function fail(reason) {
  process.stderr.write('openfish-git-credential: ' + reason + '\\n')
  process.exit(0) // 空答案 = 没有凭据，让 git 继续（可能提示输入），不阻塞
}

/** Strip a default port so :443 / :80 compare equal to the bare host. */
function normalizeHost(value) {
  const host = String(value || '').toLowerCase()
  if (host.endsWith(':443')) return host.slice(0, -4)
  if (host.endsWith(':80')) return host.slice(0, -3)
  return host
}

function readStdin() {
  try { return fs.readFileSync(0, 'utf8') } catch { return '' }
}

function requestJson(url, headers) {
  return new Promise((resolve, reject) => {
    let target
    try { target = new URL(url) } catch { reject(new Error('platform url invalid: ' + url)); return }
    const isHttps = target.protocol === 'https:'
    const transport = isHttps ? https : http
    const options = {
      method: 'GET',
      hostname: target.hostname,
      port: target.port || (isHttps ? 443 : 80),
      path: target.pathname + target.search,
      headers: Object.assign({ Accept: 'application/json' }, headers),
    }
    if (isHttps) {
      if (CA_FILE) {
        try { options.ca = fs.readFileSync(CA_FILE) } catch { options.rejectUnauthorized = false }
      } else if (VERIFY_TLS !== true) {
        options.rejectUnauthorized = false
      }
    }
    const req = transport.request(options, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8')
        let body = null
        try { body = raw ? JSON.parse(raw) : null } catch { body = null }
        resolve({ status: res.statusCode || 0, body })
      })
    })
    req.setTimeout(15000, () => req.destroy(new Error('platform request timed out')))
    req.on('error', reject)
    req.end()
  })
}

async function main() {
  // git 把动作作为参数传进来（get/store/erase），stdin 上才是 protocol/host/path。
  const action = process.argv[2] || 'get'
  if (action !== 'get') return // store/erase：无需持久化
  const lines = readStdin().split('\\n')
  const fields = {}
  for (const line of lines) {
    const at = line.indexOf('=')
    if (at > 0) fields[line.slice(0, at)] = line.slice(at + 1)
  }
  if (fields.protocol !== 'http' && fields.protocol !== 'https') return
  if (!PLATFORM_HOST || normalizeHost(fields.host) !== normalizeHost(PLATFORM_HOST)) {
    fail('refusing to answer for host ' + (fields.host || '<none>'))
    return
  }
  const parts = String(fields.path || '')
    .replace(/^\\/+/, '')
    .replace(/\\.git$/, '')
    .split('/')
    .filter(Boolean)
  if (parts.length < 2) { fail('git path 里没有 <owner>/<name>'); return }
  const owner = parts[parts.length - 2]
  const name = parts[parts.length - 1]
  const url = PLATFORM + '/api/v1/repos/' + encodeURIComponent(owner) + '/' + encodeURIComponent(name) + '/git-credential'
  try {
    const res = await requestJson(url, { Authorization: 'Bearer ' + API_KEY })
    if (res.status !== 200 || !res.body || !res.body.password) {
      fail('platform refused (HTTP ' + res.status + ') for ' + owner + '/' + name)
      return
    }
    process.stdout.write('username=' + res.body.username + '\\n')
    process.stdout.write('password=' + res.body.password + '\\n')
    process.stdout.write('\\n')
  } catch (err) {
    fail(String((err && err.message) || err))
  }
}

main().catch((err) => fail(String((err && err.message) || err)))
`
  }

  /**
   * `/etc/gitconfig`：把 credential.helper 指向上面的脚本。
   *
   * `useHttpPath = true` 是关键：不打开它，git 不会把仓库 path 传给 helper，
   * 同一 host 下的不同仓库就无法区分。TLS 姿态与插件其余部分一致（平台自签
   * 证书时跳过校验，或按 `caFile` 校验）。
   */
  function gitConfigSource() {
    const cfg = configNow()
    const lines = [
      '# 由 dsh-plugin-enterprise-intranet 自动生成（企业内网模式）',
      '[credential]',
      `\thelper = ${GIT_HELPER_PATH}`,
      '\tuseHttpPath = true',
    ]
    if (cfg.verifyTls === true && cfg.caFile) {
      lines.push('[http]', `\tsslCAInfo = ${cfg.caFile}`)
    } else if (cfg.verifyTls !== true) {
      lines.push('[http]', '\tsslVerify = false')
    }
    return lines.join('\n') + '\n'
  }

  /** 关闭 git 自动化时删掉带 key 的 helper；gitconfig 只删我们生成的那份。 */
  function removeGitCredential() {
    const removed = []
    try {
      fs.rmSync(GIT_HELPER_PATH, { force: true })
      removed.push(GIT_HELPER_PATH)
    } catch {
      // 只读路径或本来就不存在
    }
    try {
      const current = fs.readFileSync('/etc/gitconfig', 'utf8')
      if (current.includes(GIT_HELPER_PATH)) {
        fs.rmSync('/etc/gitconfig', { force: true })
        removed.push('/etc/gitconfig')
      }
    } catch {
      // 没有 gitconfig 就不用管
    }
    return removed
  }

  function mirrorsFor(key, routes) {
    const base = String(configNow().platformUrl || DEFAULTS.platformUrl).replace(/\/+$/, '')
    const host = new URL(base).host
    const bareHost = host.split(':')[0]
    const pipIndex = credentialedUrl(base, '/simple/', key)
    const aptSource = credentialedUrl(base, '/debian/', key)
    return {
      python: {
        label: 'pip / uv',
        indexUrl: pipIndex,
        env: {
          PIP_INDEX_URL: pipIndex,
          UV_INDEX_URL: pipIndex,
          PIP_TRUSTED_HOST: bareHost,
        },
        files: {
          '/etc/pip.conf': `[global]\nindex-url = ${pipIndex}\ntrusted-host = ${bareHost}\n`,
        },
      },
      npm: {
        label: 'npm',
        registry: `${base}/npm/`,
        env: {
          NPM_CONFIG_REGISTRY: `${base}/npm/`,
          npm_config_registry: `${base}/npm/`,
        },
        files: {
          '/usr/local/etc/npmrc': `registry=${base}/npm/\n//${host}/npm/:_authToken=${key}\nstrict-ssl=false\n`,
        },
      },
      node: {
        label: 'node-builds (nvm/fnm)',
        env: {
          NVM_NODEJS_ORG_MIRROR: `${base}/node-builds`,
          FNM_NODE_DIST_MIRROR: `${base}/node-builds`,
        },
        files: {},
      },
      debian: {
        label: 'apt',
        files: {
          '/etc/apt/sources.list.d/enterprise-intranet.list':
            `deb [trusted=yes] ${aptSource} ./\n`,
        },
        env: {},
      },
      docker: {
        label: 'docker',
        // docker daemon 的 registry-mirrors 只接受 host[:port]，不接受路径前缀，
        // 因此 ECS 侧 nginx 额外把 /v2/ 映射到了平台的 /docker/v2/。
        files: {
          '/etc/docker/daemon.json': JSON.stringify({
            'insecure-registries': [host],
            'registry-mirrors': [`https://${host}`],
          }, null, 2) + '\n',
        },
        env: {},
      },
      git: {
        label: 'git 仓库',
        // helper 里带着平台 key（写盘 700 权限），/etc/gitconfig 指过去并打开
        // useHttpPath，这样同一 host 下的不同 <owner>/<name> 都能区分。
        files: {
          [GIT_HELPER_PATH]: { content: gitCredentialHelperSource(key), mode: 0o700 },
          '/etc/gitconfig': { content: gitConfigSource(), mode: 0o644 },
        },
        env: {},
      },
    }
  }

  async function applyMirrors(key) {
    const mirrors = mirrorsFor(key)
    const results = []
    const envLines = ['# 由 dsh-plugin-enterprise-intranet 自动生成（企业内网模式）']
    for (const [name, entry] of Object.entries(mirrors)) {
      if (name === 'git' && configNow().autoGitCredential === false) {
        // 关掉 git 自动化时不能把带 key 的 helper 留在盘上。
        for (const file of removeGitCredential()) results.push({ file, ok: true, removed: true })
        continue
      }
      for (const [file, spec] of Object.entries(entry.files || {})) {
        const value = spec && typeof spec === 'object' && !Array.isArray(spec)
          ? spec
          : { content: spec, mode: null }
        results.push(writeConfigFile(file, value.content, value.mode))
      }
      for (const [envName, value] of Object.entries(entry.env || {})) {
        envLines.push(`export ${envName}="${String(value).replace(/"/g, '\\"')}"`)
      }
    }
    results.push(writeConfigFile('/etc/profile.d/enterprise-intranet.sh', envLines.join('\n') + '\n'))
    return results
  }

  // ── 启用 / 停用 企业内网模式 ────────────────────────────────────────

  async function applyEnterpriseMode() {
    const { key } = await resolvePlatformKey()
    if (!key) {
      const err = new Error('缺少 api-key：请先在面板中完成企业平台登录以获取 API key。')
      err.code = 'API_KEY_REQUIRED'
      err.loginUrl = platformUrl('/')
      throw err
    }

    const verified = await verifyKey(key)
    const routes = await fetchResolvedRoutes(key)
    const { providers, keysStored } = await registerProviders(routes)
    const defaultRoute = pickDefaultRoute(routes)
    if (!defaultRoute) {
      const err = new Error('模型路由表里没有可用的对话模型（provider 需为 openai / anthropic 且 enabled）。')
      err.code = 'NO_ROUTE'
      throw err
    }

    const current = readState()
    const providerId = `${PROVIDER_PREFIX}${slugify(defaultRoute.name)}`
    const modelId = defaultRoute.model || defaultRoute.name

    // 采集停用时要还原的基线 —— 必须在覆盖之前读，且只读一次。
    //
    // 不能用「是否已启用企业内网模式」判断该不该采集：设备授权登录的
    // pollLogin() 会先把 enterpriseMode 置为 true 再调用本函数，那样会漏掉
    // 唯一一次采集机会（这正是以前 previousDefaultModel 恒为 null 的原因）。
    // 改用「当前默认模型是否已经指向本插件的 provider」判断：既不会漏采，
    // 也不会把上一次应用写进去的 intranet provider 当成用户的原始默认模型。
    let baseline = current.baselineDefaultModel ?? current.previousDefaultModel ?? null
    if (!normalizeBaseline(baseline)) {
      const active = readResolvedDefault()
      baseline = isIntranetProvider(active && active.provider)
        ? null                            // 已经是我们的 → 没有可还原的基线
        : { section: captureDefaultModelLayer(), capturedAt: nowIso() }
    }

    await settings().replace(DEFAULT_MODEL_NS, { provider: providerId, model: modelId })

    let mirrorResults = null
    if (configNow().autoMirrors || configNow().autoGitCredential !== false) {
      mirrorResults = await applyMirrors(key)
    }

    const next = {
      ...current,
      config: { ...(current.config || {}), enterpriseMode: true },
      previousDefaultModel: baseline,
      baselineDefaultModel: baseline,
      lastApplyAt: nowIso(),
      lastError: null,
      providers,
      keysStored: keysStored.map((k) => k.route + ' -> ' + k.ref),
      defaultProvider: providerId,
      defaultModel: modelId,
      defaultRouteName: defaultRoute.name,
      routes: routes.map((r) => ({
        name: r.name,
        provider: r.provider,
        base_url: r.base_url,
        model: r.model,
        aliases: r.aliases,
        enabled: r.enabled !== false,
        has_api_key: !!r.api_key,
      })),
      mirrors: mirrorResults,
    }
    writeState(next)
    Object.assign(merged, { enterpriseMode: true })

    return {
      enterprise_mode: true,
      platform_user: verified.user,
      providers,
      default_provider: providerId,
      default_model: modelId,
      default_route: defaultRoute.name,
      route_count: routes.length,
      mirrors: mirrorResults,
    }
  }

  async function disableEnterpriseMode() {
    const current = readState()
    await unregisterProviders(current.providers || [])

    // 停用意味着不再有平台 key 可用，所以带 key 的 git helper 必须删掉，
    // 不能留在镜像里等下一次 clone 时被人读出来。
    removeGitCredential()

    // 只在默认模型确实还指着本插件的 provider 时才动它：用户在企业内网模式下
    // 自己把默认模型改成别的，停用不该把那次修改一起抹掉。
    const active = readResolvedDefault()
    const pointsAtUs = isIntranetProvider(active && active.provider)
    const baseline = normalizeBaseline(current.baselineDefaultModel)
      || normalizeBaseline(current.previousDefaultModel)

    let restored = null
    if (pointsAtUs) {
      // 基线为 null = 用户本来就没有用户层的默认模型，或者状态文件来自修复前的
      // 版本（那时从没采集过）。两种情况都不该让默认模型悬空指向一个已经注销的
      // provider —— 写入空分节即继承回组合 base，这是 replace() 的 reset 语义。
      await settings().replace(DEFAULT_MODEL_NS, baseline || {})
      restored = baseline
    }

    const next = { ...current, config: { ...(current.config || {}), enterpriseMode: false } }
    writeState(next)
    Object.assign(merged, { enterpriseMode: false })
    return { enterprise_mode: false, restored_default: restored }
  }

  // ── 状态快照 ────────────────────────────────────────────────────────

  async function snapshot({ probePlatform = false } = {}) {
    const cfg = configNow()
    const { key, source } = await resolvePlatformKey()
    const st = readState()
    let reachable = null
    let platformUser = null
    let routesError = null
    let routes = st.routes || []
    if (key && probePlatform) {
      try {
        const verified = await verifyKey(key)
        if (verified.ok) {
          reachable = true
          platformUser = verified.user
        } else {
          reachable = false
          routesError = verified.error || '平台拒绝了该 api-key'
        }
        if (reachable) {
          const fresh = await fetchResolvedRoutes(key)
          routes = fresh.map((r) => ({
            name: r.name,
            provider: r.provider,
            base_url: r.base_url,
            model: r.model,
            aliases: r.aliases,
            enabled: r.enabled !== false,
            has_api_key: !!r.api_key,
          }))
        }
      } catch (err) {
        reachable = false
        routesError = String((err && err.message) || err)
      }
    }
    return {
      ok: true,
      plugin: name,
      version: '0.1.0',
      platform_url: cfg.platformUrl,
      has_api_key: !!key,
      api_key_source: source,
      enterprise_mode: cfg.enterpriseMode === true,
      auto_mirrors: cfg.autoMirrors !== false,
      auto_git_credential: cfg.autoGitCredential !== false,
      default_alias: cfg.defaultAlias || 'default',
      verify_tls: cfg.verifyTls === true,
      reachable,
      platform_user: platformUser,
      routes_error: routesError,
      routes,
      default_provider: st.defaultProvider || null,
      default_model: st.defaultModel || null,
      default_route_name: st.defaultRouteName || null,
      last_apply_at: st.lastApplyAt || null,
      last_error: st.lastError || null,
      mirrors: st.mirrors || null,
      login_url: platformUrl('/'),
    }
  }

  async function fetchCatalog(key) {
    if (!key) {
      const err = new Error('缺少 api-key')
      err.code = 'API_KEY_REQUIRED'
      throw err
    }
    const [tools, docs] = await Promise.all([
      platformGet('/api/v1/tools', key).catch(() => null),
      platformGet('/api/v1/docs', key).catch(() => null),
    ])
    const base = String(configNow().platformUrl || DEFAULTS.platformUrl).replace(/\/+$/, '')
    return {
      platform_url: base,
      tools: tools && tools.body ? tools.body : null,
      docs: docs && docs.body ? docs.body : null,
      links: {
        home: `${base}/`,
        tools: `${base}/tools/`,
        npm: `${base}/npm/`,
        docker: `${base}/docker/`,
        debian: `${base}/debian/`,
        models: `${base}/models`,
        docs: `${base}/documentation/python`,
        api_keys: `${base}/api-keys`,
      },
    }
  }

  // ── HTTP 端点 ───────────────────────────────────────────────────────

  function authorized(req) {
    const token = req.headers['x-dsh-intranet-token']
    return typeof token === 'string' && token.length === csrf.length &&
      crypto.timingSafeEqual(Buffer.from(token), Buffer.from(csrf))
  }

  function guard(req, res, fn) {
    if (!authorized(req)) {
      sendJson(res, 403, { ok: false, error: '缺少或错误的 X-DSH-Intranet-Token' })
      return
    }
    Promise.resolve()
      .then(fn)
      .catch((err) => {
        sendJson(res, 200, {
          ok: false,
          code: (err && err.code) || 'ERROR',
          error: String((err && err.message) || err),
          login_url: (err && err.loginUrl) || platformUrl('/'),
        })
      })
  }

  function register(method, pathname, handler) {
    disposers.push(ctx.webServer.register({
      kind: 'exact',
      path: `${ROUTE_PREFIX}${pathname}`,
      handler: (req, res) => {
        if (method && req.method !== method) {
          sendJson(res, 405, { ok: false, error: `method ${req.method} not allowed` })
          return
        }
        handler(req, res)
      },
    }))
  }

  register('GET', '/state.json', (req, res) => {
    guard(req, res, async () => {
      sendJson(res, 200, await snapshot({ probePlatform: true }))
    })
  })

  register('GET', '/routes.json', (req, res) => {
    guard(req, res, async () => {
      const { key } = await resolvePlatformKey()
      if (!key) {
        const err = new Error('缺少 api-key')
        err.code = 'API_KEY_REQUIRED'
        throw err
      }
      sendJson(res, 200, { ok: true, routes: await fetchResolvedRoutes(key) })
    })
  })

  register('GET', '/catalog.json', (req, res) => {
    guard(req, res, async () => {
      const { key } = await resolvePlatformKey()
      sendJson(res, 200, { ok: true, ...(await fetchCatalog(key)) })
    })
  })

  register('GET', '/mirrors.json', (req, res) => {
    guard(req, res, async () => {
      const { key } = await resolvePlatformKey()
      const mirrors = mirrorsFor(key, readState().routes || [])
      const st = readState()
      sendJson(res, 200, {
        ok: true,
        auto_mirrors: configNow().autoMirrors !== false,
        mirrors,
        applied: st.mirrors || null,
      })
    })
  })

  register('POST', '/login/start', (req, res) => {
    guard(req, res, async () => {
      sendJson(res, 200, { ok: true, ...(await startLogin()) })
    })
  })

  register('GET', '/login/poll', (req, res) => {
    guard(req, res, async () => {
      const url = new URL(req.url, 'http://localhost')
      const loginId = url.searchParams.get('login_id') || ''
      sendJson(res, 200, { ok: true, ...(await pollLogin(loginId)) })
    })
  })

  register('POST', '/login/cancel', (req, res) => {
    guard(req, res, async () => {
      const body = JSON.parse((await readBody(req)) || '{}')
      if (body.login_id) logins.delete(body.login_id)
      sendJson(res, 200, { ok: true })
    })
  })

  register('POST', '/key', (req, res) => {
    guard(req, res, async () => {
      const body = JSON.parse((await readBody(req)) || '{}')
      const key = String(body.api_key || '').trim()
      if (!key) {
        sendJson(res, 200, { ok: false, code: 'API_KEY_REQUIRED', error: 'api_key 不能为空' })
        return
      }
      const verified = await verifyKey(key)
      if (!verified.ok) {
        sendJson(res, 200, {
          ok: false,
          code: verified.code || 'API_KEY_REJECTED',
          error: verified.error || '平台不认这枚 api-key（session 与模型路由两次校验都没过）',
        })
        return
      }
      await storePlatformKey(key)
      sendJson(res, 200, { ok: true, user: verified.user, via: verified.via })
    })
  })

  register('POST', '/mode', (req, res) => {
    guard(req, res, async () => {
      const body = JSON.parse((await readBody(req)) || '{}')
      if (body.enabled === true) {
        sendJson(res, 200, { ok: true, ...(await applyEnterpriseMode()) })
        return
      }
      sendJson(res, 200, { ok: true, ...(await disableEnterpriseMode()) })
    })
  })

  register('POST', '/apply', (req, res) => {
    guard(req, res, async () => {
      sendJson(res, 200, { ok: true, ...(await applyEnterpriseMode()) })
    })
  })

  register('POST', '/mirrors/apply', (req, res) => {
    guard(req, res, async () => {
      const { key } = await resolvePlatformKey()
      if (!key) {
        const err = new Error('缺少 api-key')
        err.code = 'API_KEY_REQUIRED'
        throw err
      }
      const results = await applyMirrors(key)
      const current = readState()
      writeState({ ...current, mirrors: results })
      sendJson(res, 200, { ok: true, mirrors: results })
    })
  })

  register('POST', '/config', (req, res) => {
    guard(req, res, async () => {
      const body = JSON.parse((await readBody(req)) || '{}')
      const patch = {}
      if (typeof body.platform_url === 'string' && body.platform_url.trim()) {
        patch.platformUrl = body.platform_url.trim().replace(/\/+$/, '')
      }
      if (typeof body.auto_mirrors === 'boolean') patch.autoMirrors = body.auto_mirrors
      if (typeof body.auto_git_credential === 'boolean') patch.autoGitCredential = body.auto_git_credential
      if (typeof body.verify_tls === 'boolean') patch.verifyTls = body.verifyTls
      if (typeof body.default_alias === 'string' && body.default_alias.trim()) {
        patch.defaultAlias = body.default_alias.trim()
      }
      persistConfig(patch)
      sendJson(res, 200, { ok: true, config: configNow() })
    })
  })

  // 面板脚本本身不校验 CSRF（浏览器要能加载它），但 CSRF 只下发在 index HTML 里，
  // 所以脚本本身不含任何秘密。
  disposers.push(ctx.webServer.register({
    kind: 'exact',
    path: `${ROUTE_PREFIX}/panel.js`,
    handler: (req, res) => {
      try {
        const bytes = fs.readFileSync(PANEL_JS_FILE)
        res.writeHead(200, {
          'Content-Type': 'application/javascript; charset=utf-8',
          'Cache-Control': 'no-store',
          'Content-Length': String(bytes.length),
        })
        res.end(bytes)
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end('panel.js unavailable: ' + String((err && err.message) || err))
      }
    },
  }))

  disposers.push(ctx.webServer.tapIndex((html) => {
    if (html.includes(`${ROUTE_PREFIX}/panel.js`)) return html
    const boot = JSON.stringify({
      csrf,
      endpoint: ROUTE_PREFIX,
      platformUrl: configNow().platformUrl,
    })
    const tags =
      `<script>window.__DSH_INTRANET_BOOT__=${boot};</script>` +
      `<script defer src="${ROUTE_PREFIX}/panel.js"></script>`
    if (html.includes('</body>')) return html.replace('</body>', tags + '</body>')
    return html + tags
  }))

  // 启动时若已配置 key 且模式为开，自动重新套用一次（例如容器重启后设置被清空）。
  ctx.effect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const cfg = configNow()
        if (cfg.enterpriseMode !== true) return
        const { key } = await resolvePlatformKey()
        if (!key || cancelled) return
        await applyEnterpriseMode()
      } catch (err) {
        const current = readState()
        writeState({ ...current, lastError: String((err && err.message) || err) })
      }
    })()
    return () => {
      cancelled = true
      for (const dispose of disposers) {
        try {
          dispose()
        } catch {
          // 卸载路径上的失败不该影响其它 disposer。
        }
      }
    }
  })

  // 让 DSH 的插件清单能看到本插件。
  ctx.logger?.info?.(
    `enterprise-intranet: ready (platform=${configNow().platformUrl}, enterpriseMode=${configNow().enterpriseMode === true})`,
  )
}

export default { name, inject, apply }
