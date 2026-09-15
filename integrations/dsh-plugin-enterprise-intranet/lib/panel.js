/**
 * dsh-plugin-enterprise-intranet — DSH Web 面板（浏览器侧）。
 *
 * 左下角常驻一个「内网」按钮，点开是企业内网模式面板：
 *  - 没有 api-key 时显示「登录企业平台获取 API Key」，点击后打开平台的设备授权页，
 *    并在后台轮询，登录成功后平台签发的 key 由宿主插件自动收下（无需复制粘贴）；
 *  - 有 key 时显示企业内网模式开关、模型路由默认模型、包源与工具/文档入口。
 *
 * 安全：所有请求都带 `X-DSH-Intranet-Token`（只在 index HTML 里下发的 per-process
 * CSRF token，见 lib/index.js）。面板从不接收也不展示 api-key 的值。
 */
;(function () {
  'use strict'
  var BOOT = window.__DSH_INTRANET_BOOT__
  if (!BOOT || !BOOT.csrf) return
  if (window.__dshIntranetPanel) return
  window.__dshIntranetPanel = true

  var EP = BOOT.endpoint || '/dsh-intranet'
  var HEADERS = { 'X-DSH-Intranet-Token': BOOT.csrf }
  var pollTimer = null
  var loginState = null

  var CSS = [
    '.dshei-root{position:fixed;left:16px;bottom:16px;z-index:9998;font:13px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2329}',
    '.dshei-btn{display:flex;align-items:center;gap:6px;padding:8px 12px;border-radius:999px;border:1px solid #d5d9e0;background:#fff;color:#1f2329;cursor:pointer;box-shadow:0 4px 14px rgba(15,23,42,.12);font-weight:600}',
    '.dshei-btn:hover{background:#f2f5fa}',
    '.dshei-dot{width:8px;height:8px;border-radius:50%;background:#9aa3af;flex:none}',
    '.dshei-dot.on{background:#16a34a}.dshei-dot.warn{background:#f59e0b}.dshei-dot.off{background:#dc2626}',
    '.dshei-panel{position:fixed;left:16px;bottom:64px;width:400px;max-width:calc(100vw - 32px);max-height:76vh;overflow:auto;background:#fff;border:1px solid #e3e6ea;border-radius:14px;box-shadow:0 16px 44px rgba(15,23,42,.2);padding:16px;display:none}',
    '.dshei-panel.open{display:block}',
    '.dshei-panel h2{margin:0 0 2px;font-size:15px}',
    '.dshei-panel h3{margin:14px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280}',
    '.dshei-muted{color:#6b7280;font-size:12px}',
    '.dshei-row{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px dashed #eef0f3}',
    '.dshei-row:last-child{border-bottom:0}',
    '.dshei-pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;background:#eef2f7;color:#475569}',
    '.dshei-pill.ok{background:#dcfce7;color:#166534}.dshei-pill.bad{background:#fee2e2;color:#991b1b}.dshei-pill.warn{background:#fef3c7;color:#92400e}',
    '.dshei-act{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}',
    '.dshei-act button{padding:8px 12px;border-radius:8px;border:1px solid #d5d9e0;background:#fff;cursor:pointer;font-weight:600}',
    '.dshei-act button.primary{background:#2563eb;border-color:#2563eb;color:#fff}',
    '.dshei-act button.primary:hover{background:#1d4ed8}',
    '.dshei-act button:disabled{opacity:.55;cursor:default}',
    '.dshei-code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:20px;letter-spacing:.14em;font-weight:700;text-align:center;padding:10px;margin:8px 0;border-radius:9px;background:rgba(37,99,235,.09);color:#2563eb}',
    '.dshei-msg{margin-top:8px;font-size:12px;white-space:pre-wrap;word-break:break-word}',
    '.dshei-msg.err{color:#dc2626}.dshei-msg.ok{color:#16a34a}',
    '.dshei-code-inline{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;background:#f1f3f6;padding:1px 4px;border-radius:4px}',
    'a.dshei-link{color:#2563eb;text-decoration:none}a.dshei-link:hover{text-decoration:underline}',
    'body.dark .dshei-btn,body.dark .dshei-panel,body[data-theme="dark"] .dshei-btn,body[data-theme="dark"] .dshei-panel{background:#1e2128;color:#e6e8eb;border-color:#2c313a}',
  ].join('\n')

  function el(tag, attrs, text) {
    var node = document.createElement(tag)
    if (attrs) Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]) })
    if (text != null) node.textContent = text
    return node
  }

  function api(pathname, options) {
    var opts = options || {}
    return fetch(EP + pathname, {
      method: opts.method || 'GET',
      headers: Object.assign({}, HEADERS, opts.body ? { 'Content-Type': 'application/json' } : {}),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    }).then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status } }) })
  }

  function install() {
    var style = el('style')
    style.textContent = CSS
    document.head.appendChild(style)

    var root = el('div', { class: 'dshei-root' })
    var dot = el('span', { class: 'dshei-dot' })
    var btn = el('button', { class: 'dshei-btn', type: 'button' })
    btn.appendChild(dot)
    btn.appendChild(el('span', null, '内网'))
    var panel = el('div', { class: 'dshei-panel' })
    root.appendChild(btn)
    root.appendChild(panel)
    document.body.appendChild(root)

    var open = false
    btn.addEventListener('click', function () {
      open = !open
      panel.classList.toggle('open', open)
      if (open) render()
    })

    function setDot(kind) {
      dot.className = 'dshei-dot' + (kind ? ' ' + kind : '')
    }

    function section(title) {
      panel.appendChild(el('h3', null, title))
    }

    function row(label, valueNode) {
      var r = el('div', { class: 'dshei-row' })
      r.appendChild(el('span', { class: 'dshei-muted' }, label))
      r.appendChild(valueNode)
      panel.appendChild(r)
      return r
    }

    function pill(text, kind) {
      return el('span', { class: 'dshei-pill' + (kind ? ' ' + kind : '') }, text)
    }

    function msg(text, kind) {
      panel.appendChild(el('div', { class: 'dshei-msg' + (kind ? ' ' + kind : '') }, text))
    }

    function clearPanel(heading) {
      panel.innerHTML = ''
      panel.appendChild(el('h2', null, heading || '企业内网模式'))
    }

    // ── 状态与渲染 ──────────────────────────────────────────────────
    function render(state) {
      clearPanel()
      if (!state) {
        msg('正在读取状态…')
        api('/state.json').then(render).catch(function (e) { clearPanel(); msg(String(e), 'err') })
        return
      }
      if (state.ok === false) {
        msg(state.error || '读取状态失败', 'err')
        return
      }

      var on = state.enterprise_mode === true
      setDot(on ? 'on' : (state.has_api_key ? 'warn' : 'off'))
      msg('平台：' + state.platform_url, 'dshei-muted' === '' ? '' : undefined)

      section('API Key（必选）')
      if (state.has_api_key) {
        row('状态', pill('已配置（来源：' + (state.api_key_source || 'unknown') + '）', 'ok'))
        if (state.platform_user) row('平台账号', el('span', null, state.platform_user))
        if (state.reachable === false) row('连通性', pill('不可达', 'bad'))
        else if (state.reachable === true) row('连通性', pill('正常', 'ok'))
      } else {
        row('状态', pill('未配置', 'bad'))
        msg('企业内网模式必须先有 api-key。点击下面的按钮打开企业平台登录，登录成功后会自动把密钥交给 DSH。')
        renderLogin()
      }

      if (state.has_api_key) {
        section('企业内网模式')
        row('模式', pill(on ? '已启用' : '未启用', on ? 'ok' : 'warn'))
        if (state.default_model) {
          row('默认模型', el('span', { class: 'dshei-code-inline' }, state.default_provider + ' / ' + state.default_model))
        }
        if (state.last_apply_at) row('最近应用', el('span', { class: 'dshei-muted' }, state.last_apply_at))
        row('包源自动切换', pill(state.auto_mirrors ? '开' : '关', state.auto_mirrors ? 'ok' : ''))
        var act = el('div', { class: 'dshei-act' })
        var toggle = el('button', { type: 'button', class: on ? '' : 'primary' }, on ? '停用企业内网模式' : '启用企业内网模式')
        var reapply = el('button', { type: 'button' }, '重新应用')
        var relogin = el('button', { type: 'button' }, '重新登录获取 Key')
        act.appendChild(toggle)
        act.appendChild(reapply)
        act.appendChild(relogin)
        panel.appendChild(act)

        toggle.addEventListener('click', function () {
          toggle.disabled = true
          reapply.disabled = true
          api('/mode', { method: 'POST', body: { enabled: !on } })
            .then(function (r) {
              toggle.disabled = false
              reapply.disabled = false
              if (r.ok === false) { msg(r.code + ': ' + r.error, 'err'); return }
              render(null)
            })
            .catch(function (e) { toggle.disabled = false; reapply.disabled = false; msg(String(e), 'err') })
        })
        reapply.addEventListener('click', function () {
          reapply.disabled = true
          msg('正在应用…')
          api('/apply', { method: 'POST', body: {} })
            .then(function (r) {
              reapply.disabled = false
              render(null)
              if (r.ok === false) msg(r.code + ': ' + r.error, 'err')
              else msg('已接入 ' + (r.providers || []).length + ' 个 provider，默认模型 ' + r.default_provider + ' / ' + r.default_model, 'ok')
            })
            .catch(function (e) { reapply.disabled = false; msg(String(e), 'err') })
        })
        relogin.addEventListener('click', function () { renderLogin(true) })
      }

      if (state.routes && state.routes.length) {
        section('模型路由（' + state.routes.length + '）')
        state.routes.forEach(function (r) {
          var v = el('span')
          v.appendChild(el('span', { class: 'dshei-code-inline' }, r.name))
          v.appendChild(document.createTextNode(' '))
          var defaultish = (r.aliases || []).indexOf(state.default_alias) >= 0
          v.appendChild(pill(r.enabled ? (defaultish ? '默认' : '启用') : '停用',
            r.enabled ? (defaultish ? 'ok' : '') : 'warn'))
          row(r.provider + ' · ' + r.model, v)
        })
      }
      if (state.routes_error) msg('路由读取失败：' + state.routes_error, 'err')

      section('企业制品中心')
      api('/catalog.json').then(function (c) {
        if (!c || c.ok === false) return
        var box = el('div')
        Object.keys(c.links || {}).forEach(function (name) {
          var a = el('a', { class: 'dshei-link', href: c.links[name], target: '_blank', rel: 'noreferrer' }, name)
          box.appendChild(a)
          box.appendChild(document.createTextNode('  '))
        })
        panel.appendChild(box)
        var counts = []
        if (c.tools && c.tools.tool_count != null) counts.push('工具 ' + c.tools.tool_count)
        if (c.docs && c.docs.ecosystems) counts.push('文档生态 ' + (c.docs.ecosystems.length || 0))
        if (counts.length) msg(counts.join(' · '), '')
      }).catch(function () {})

      var foot = el('div', { class: 'dshei-act' })
      var mirrorsBtn = el('button', { type: 'button' }, '查看/重写包源配置')
      foot.appendChild(mirrorsBtn)
      panel.appendChild(foot)
      mirrorsBtn.addEventListener('click', function () {
        api('/mirrors.json').then(function (m) {
          if (!m || m.ok === false) { msg((m && m.error) || '读取失败', 'err'); return }
          var lines = []
          Object.keys(m.mirrors).forEach(function (k) {
            var e = m.mirrors[k]
            lines.push('• ' + e.label + (e.registry ? ' → ' + e.registry : '') + (e.indexUrl ? ' → ' + e.indexUrl.replace(/:[^:@/]+@/, ':***@') : ''))
          })
          msg(lines.join('\n'), '')
          api('/mirrors/apply', { method: 'POST', body: {} }).then(function (r) {
            if (r.ok === false) { msg(r.code + ': ' + r.error, 'err'); return }
            var bad = (r.mirrors || []).filter(function (x) { return !x.ok })
            msg(bad.length ? ('已写入，' + bad.length + ' 个文件失败：' + bad.map(function (b) { return b.file }).join(', ')) : '包源配置已写入容器。', bad.length ? 'err' : 'ok')
          })
        })
      })
    }

    function renderLogin(force) {
      if (loginState && !force) return
      loginState = { polling: false }
      var box = el('div')
      var startBtn = el('button', { type: 'button', class: 'primary' }, '打开企业平台登录并获取 API Key')
      var wrap = el('div', { class: 'dshei-act' })
      wrap.appendChild(startBtn)
      box.appendChild(wrap)
      panel.appendChild(box)

      startBtn.addEventListener('click', function () {
        startBtn.disabled = true
        fetchLogin()
      })

      function fetchLogin() {
        api('/login/start', { method: 'POST', body: {} })
          .then(function (r) {
            if (r.ok === false) { msg(r.code + ': ' + r.error, 'err'); startBtn.disabled = false; return }
            var url = r.verification_uri_complete || r.verification_uri
            msg('已生成授权码，正在打开企业登录窗口…\n授权码：' + r.user_code)
            panel.appendChild(el('div', { class: 'dshei-code' }, r.user_code))
            var a = el('a', { class: 'dshei-link', href: url, target: '_blank', rel: 'noreferrer' }, '若没有自动打开，点这里 →')
            panel.appendChild(a)
            try { window.open(url, '_blank', 'noopener') } catch (e) { /* 浏览器可能拦截，用户可点上面的链接 */ }
            loginState = { polling: true, loginId: r.login_id }
            startPolling()
          })
          .catch(function (e) { msg(String(e), 'err'); startBtn.disabled = false })
      }

      function startPolling() {
        if (pollTimer) clearInterval(pollTimer)
        pollTimer = setInterval(function () {
          if (!loginState || !loginState.polling) return
          api('/login/poll?login_id=' + encodeURIComponent(loginState.loginId))
            .then(function (r) {
              if (!r || r.ok === false) return
              if (r.status === 'pending') return
              clearInterval(pollTimer)
              pollTimer = null
              loginState = null
              if (r.status === 'approved') {
                render(null)
                msg('登录成功，API Key 已自动写入 DSH' + (r.user ? '（' + r.user + '）' : '') + '。', 'ok')
                if (r.apply_error) msg('自动应用企业内网模式失败：' + r.apply_error, 'err')
              } else {
                msg(r.message || '登录未完成，请重试。', 'err')
                render(null)
              }
            })
            .catch(function () {})
        }, 2000)
      }
    }

    // 首次后台探测，把点染成正确颜色（不自动展开面板）。
    api('/state.json').then(function (s) {
      if (s && s.ok !== false) setDot(s.enterprise_mode ? 'on' : (s.has_api_key ? 'warn' : 'off'))
      if (open) render(s)
    }).catch(function () {})
  }

  if (document.body) install()
  else document.addEventListener('DOMContentLoaded', install)
})()
