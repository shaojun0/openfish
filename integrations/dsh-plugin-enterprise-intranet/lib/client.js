/**
 * 客户端半边 —— 只做一件事：把「完全还原（卸载前）」放到 DSH 插件页的本插件详情页上。
 *
 * 为什么需要它：DSH 至今没有插件面向的卸载钩子（`dsh-plugin-manager` 的
 * `plugin-manager/changed` 是在插件已经被卸载之后才发出的，见 lib/index.js 里
 * `pluginStillDeclared()` 的说明），所以宿主侧那段兜底必须保留。这个按钮只是把
 * 「卸载前先完全还原」摆到用户真正会走的路径上 —— `plugins.detail.actions` 槽位的
 * 位置就是「页面自己的开关与卸载按钮之前」。
 *
 * 形态遵循 DSH 0.2.0 的客户端模块契约（见 dsh-agent-preset 的
 * cordis-plugin-development/references/ui-plugin.md 与 templates/decoration/）：
 * 注册一个 id 等于包名的惰性工厂，React 从浏览器模块表取。因此本插件**仍然是零构建**的
 * —— 没有打包步骤，也没有 node_modules 运行时依赖。
 *
 * 文案与 lib/panel.js 保持一致，直接写中文，不接客户端 locale 服务：panel.js 同样如此，
 * 而这个半边只有一句话，接 locale 需要额外的字典文件与注入声明，收益不抵成本。
 */
window.__ModuleLoader__.load({
  id: 'dsh-plugin-enterprise-intranet',

  factory(require) {
    const React = require('react')
    const h = React.createElement

    const PACKAGE_NAME = 'dsh-plugin-enterprise-intranet'

    /** 宿主下发的 boot 负载；见 lib/index.js 的 webserver/index-inject。 */
    function readBoot() {
      const value = window.__DSH_INTRANET_BOOT__
      return value && typeof value === 'object' ? value : null
    }

    /**
     * 调本插件在宿主侧的端点。CSRF token 只随 index HTML 下发，所以这里从 boot 负载读，
     * 用户不必再手工从页面里抄一个 token。
     */
    async function post(path, body) {
      const info = readBoot()
      if (!info || !info.csrf) {
        throw new Error('未找到宿主下发的 CSRF token（本插件的面板脚本未注入？）')
      }
      const res = await fetch(`${info.endpoint}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-DSH-Intranet-Token': info.csrf },
        body: JSON.stringify(body || {}),
      })
      const text = await res.text()
      let payload = null
      try {
        payload = text ? JSON.parse(text) : null
      } catch {
        payload = { raw: text }
      }
      if (!res.ok) {
        const detail = payload && (payload.error || payload.raw || payload.message)
        throw new Error(detail ? String(detail) : `HTTP ${res.status}`)
      }
      return payload
    }

    /** 把 teardown 的结果压成一句话，好让按钮旁边直接看出发生了什么。 */
    function summarize(result) {
      if (!result || typeof result !== 'object') return '已完成'
      const parts = []
      if (typeof result.providers_unregistered === 'number') {
        parts.push(`注销 ${result.providers_unregistered} 个 provider`)
      }
      const removed = [].concat(result.mirrors_removed || [])
      if (removed.length) parts.push(`删除 ${removed.length} 个包源文件`)
      if (result.state_removed) parts.push('状态文件已删')
      // 被用户改过、或不是本插件写入的文件只报告不删除，值得露出条数。
      const kept = [].concat(
        result.credentials_kept || [],
        result.mirrors_kept || [],
        result.mirrors_unmanaged || [],
      )
      if (kept.length) parts.push(`保留 ${kept.length} 项未动（被改过或非本插件写入）`)
      if (result.settings_error) parts.push(`设置还原出错：${result.settings_error}`)
      return parts.length ? parts.join('，') : '已完成'
    }

    /**
     * 详情页头部的一个按钮。槽位会把页面的 subject 作为 props 传进来，不属于本页时返回 null
     * —— 这是契约要求的（「an entry renders null for a subject it has nothing for」）。
     */
    function TeardownAction({ subject }) {
      const isOurs =
        !!subject && subject.kind === 'bundle' && !!subject.pkg && subject.pkg.name === PACKAGE_NAME
      const [busy, setBusy] = React.useState(false)
      const [note, setNote] = React.useState('')
      const [failed, setFailed] = React.useState(false)

      if (!isOurs) return null

      const run = async () => {
        if (busy) return
        const ok = window.confirm(
          '完全还原会停用企业内网模式，并删除平台 key、git credential helper 与本插件写入的包源配置。\n' +
            '被你自己改过的文件会保留。确定继续？',
        )
        if (!ok) return
        setBusy(true)
        setFailed(false)
        setNote('')
        try {
          const result = await post('/teardown', {})
          setNote(summarize(result))
        } catch (err) {
          setFailed(true)
          setNote(String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      return h(
        'span',
        { style: { display: 'inline-flex', alignItems: 'center', gap: '0.5em', flexWrap: 'wrap' } },
        h(
          'button',
          { type: 'button', onClick: run, disabled: busy },
          busy ? '正在还原…' : '完全还原（卸载前）',
        ),
        note
          ? h(
              'span',
              {
                style: {
                  fontSize: '0.85em',
                  color: failed ? 'var(--ant-color-error, inherit)' : 'inherit',
                  opacity: failed ? 1 : 0.75,
                },
              },
              note,
            )
          : null,
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('plugins.detail.actions', () =>
          ctx.slots.register(
            { name: 'plugins.detail.actions', id: 'enterprise-intranet-teardown', order: 5 },
            TeardownAction,
          ),
        )
      },
    }
  },
})
