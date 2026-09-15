"""Device authorization routes — the key hand-off the DSH plugin depends on.

Two audiences share this module:

* the **plugin** (``/api/v1/device/*``, JSON, anonymous by design), and
* the **human** who approves it (``/device``, HTML, reached through whatever
  login the deployment has configured — HTTP Basic or the corporate
  OAuth2/4A provider).

The pages here are rendered from inline templates rather than living in the Vue
SPA.  That is deliberate: the approval URL has to survive a full OAuth round
trip (``/auth/login?next=/device?user_code=…``) and be usable in a deployment
whose SPA bundle predates this feature, so it must not depend on a rebuilt
frontend or on client-side routing.

Flow and storage live in :mod:`services.device_auth`; this module only binds
HTTP to it.

``POST /api/v1/device/code``    start a request (anonymous)
``POST /api/v1/device/token``   poll for the minted key (anonymous, one-shot)
``GET  /device``                the approval page (bounces to login, then back)
``POST /device/approve``        mint the key and bind it to the request
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template_string,
    request, url_for,
)

from auth.decorators import (
    current_display_name, current_sub, current_user_id, require_permission,
)
from auth.permissions import KEY_CREATE
from config import settings
from errors import BadRequestError
from openapi import api_operation, errors, json_body, ok
from services.device_auth import (
    AlreadyApprovedError, DeviceAuthStore, ExpiredUserCodeError,
    UnknownUserCodeError,
)

device_bp = Blueprint("device", __name__)

#: Key lifetime handed to the plugin.  ``None`` would be permanent; 90 days is
#: long enough that a container restart is not a re-login, short enough that an
#: abandoned key does not outlive the interest in it.
KEY_LIFETIME_DAYS = 90


def _store() -> DeviceAuthStore:
    """The process-wide store, built on first use and cached on the app."""
    store = current_app.extensions.get("device_auth")
    if store is None:
        store = DeviceAuthStore(
            settings.hub.device_codes_file,
            ttl=settings.hub.device_code_ttl,
        )
        current_app.extensions["device_auth"] = store
    return store


def _base_url() -> str:
    """Absolute public base URL, for the ``verification_uri`` we hand out.

    ``SERVER__PUBLIC_BASE_URL`` wins when set; otherwise the request's own Host
    (with ``X-Forwarded-Proto``/``-Host`` already honoured by Flask's
    ``ProxyFix``) is used, which is what the shipped nginx deployment needs.
    """
    configured = (settings.server.public_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


# ── Pages ────────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · {{ server_name }}</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; padding: 24px;
         font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f5f6f8; color: #1f2329; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181d; color: #e6e8eb; }
    .card { background: #1e2128 !important; border-color: #2c313a !important; }
    .muted { color: #9aa3af !important; }
    code { background: #262b34 !important; }
    input { background: #16181d !important; color: inherit !important;
            border-color: #343a44 !important; }
  }
  .card { width: 100%; max-width: 460px; background: #fff; border: 1px solid #e3e6ea;
          border-radius: 14px; padding: 28px; box-shadow: 0 8px 28px rgba(15,23,42,.08); }
  h1 { margin: 0 0 6px; font-size: 19px; }
  p { margin: 0 0 14px; }
  .muted { color: #6b7280; font-size: 13px; }
  code { background: #f0f2f5; padding: 2px 6px; border-radius: 5px;
         font: 13px/1.5 ui-monospace, Menlo, Consolas, monospace; }
  .code { display: block; text-align: center; font-size: 30px; letter-spacing: .16em;
          font-weight: 700; padding: 14px; margin: 16px 0; border-radius: 10px;
          background: rgba(59,130,246,.10); color: #2563eb;
          font-family: ui-monospace, Menlo, Consolas, monospace; }
  input[type=text] { width: 100%; padding: 11px 12px; font-size: 18px; letter-spacing: .12em;
          text-align: center; text-transform: uppercase; border: 1px solid #d5d9e0;
          border-radius: 9px; margin-bottom: 14px; font-family: ui-monospace, monospace; }
  button { width: 100%; padding: 12px; font-size: 15px; font-weight: 600; color: #fff;
           background: #2563eb; border: 0; border-radius: 9px; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
  ul { padding-left: 18px; margin: 0 0 14px; } li { margin: 4px 0; }
</style>
</head>
<body><div class="card">{{ body|safe }}</div></body>
</html>
"""


def _render(*, title: str, body: str, status: int = 200):
    return render_template_string(
        _PAGE, title=title, body=body,
        server_name=html.escape(settings.server.server_name),
    ), status


def _login_redirect():
    """Send an unauthenticated visitor through the deployment's login flow.

    ``next`` points back here *including* the query string, so the user_code the
    plugin generated survives the round trip.  ``/auth/login`` accepts only a
    same-origin absolute path (see ``routes.auth_routes._safe_next``).
    """
    target = request.full_path if request.query_string else request.path
    return redirect(url_for("auth.auth_login", next=target))


def _approve_body(user_code: str, *, note: str = "", error: str = "") -> str:
    escaped = html.escape(user_code)
    message = f'<p class="err">{html.escape(error)}</p>' if error else ""
    hint = f'<p class="muted">{html.escape(note)}</p>' if note else ""
    return f"""
      <h1>授权 DSH 企业内网连接</h1>
      <p>一个 DSH 客户端请求以你的账号身份访问本平台。确认下面的代码与 DSH 界面上显示的一致，然后授权：</p>
      <span class="code">{escaped or '—'}</span>
      {hint}{message}
      <form method="post" action="{url_for('device.approve_device')}">
        <input type="text" name="user_code" value="{escaped}" placeholder="XXXX-XXXX"
               autocomplete="off" autocapitalize="characters" spellcheck="false" required>
        <button type="submit">授权并签发 API 密钥</button>
      </form>
      <p class="muted" style="margin-top:14px">授权后平台会立即签发一枚 API 密钥并交给发起请求的 DSH 客户端，你无需复制粘贴。密钥可在「API 密钥」页面随时吊销。</p>
    """


@device_bp.route("/device")
def authorize():
    """The approval page.  Unauthenticated visitors are sent to log in first."""
    from routes.session import identify

    user, _method = identify()
    if user is None:
        return _login_redirect()

    # A signed-in visitor with no code still gets a usable form (they may be
    # approving out of band), but the common path is ?user_code=… pre-filled.
    user_code = request.args.get("user_code", "").strip().upper()
    note = ""
    error = ""
    if user_code:
        try:
            found = _store().describe_user_code(user_code)
            user_code = found["user_code"]
        except AlreadyApprovedError:
            error = "该授权码已经使用过了。请在 DSH 中重新发起一次。"
        except (UnknownUserCodeError, ExpiredUserCodeError):
            error = "该授权码无效或已过期。请在 DSH 中重新发起一次。"
            user_code = ""

    if not error:
        note = f"当前登录账号：{current_display_name()}"
    return _render(title="授权 DSH", body=_approve_body(user_code, note=note, error=error))


@device_bp.route("/device/approve", methods=["POST"])
@require_permission(KEY_CREATE)
def approve_device():
    """Mint an API key for the signed-in account and bind it to the request."""
    user_code = (request.form.get("user_code") or "").strip().upper()
    if not user_code:
        raise BadRequestError("缺少授权码 user_code")

    store = _store()
    try:
        store.describe_user_code(user_code)
    except AlreadyApprovedError:
        return _render(
            title="授权失败",
            body='<h1 class="err">该授权码已使用</h1>'
                 '<p>请在 DSH 中重新发起一次企业内网连接。</p>',
            status=409,
        )
    except (UnknownUserCodeError, ExpiredUserCodeError):
        return _render(
            title="授权失败",
            body='<h1 class="err">授权码无效或已过期</h1>'
                 '<p>请在 DSH 中重新发起一次企业内网连接。</p>',
            status=410,
        )

    manager = current_app.extensions["api_key_manager"]
    display = current_display_name()
    key = manager.create_key(
        name=f"DSH 企业内网 · {display}",
        created_by=current_sub() or "unknown",
        expires_in_days=KEY_LIFETIME_DAYS,
        user_id=current_user_id(),
    )
    store.approve(user_code, key=key, user=current_sub() or "unknown", display_name=display)

    expires_note = key.get("expires_at")
    if expires_note:
        try:
            when = datetime.strptime(expires_note, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M")
            expires_note = f"，有效期至 {when}"
        except ValueError:
            expires_note = ""
    else:
        expires_note = "，长期有效"

    return _render(
        title="授权成功",
        body=(
            '<h1 class="ok">授权成功</h1>'
            f'<p>已为 <strong>{html.escape(display)}</strong> 签发 API 密钥 '
            f'<code>{html.escape(str(key.get("prefix") or ""))}</code>{expires_note}。</p>'
            '<p>密钥已自动发送给发起请求的 DSH 客户端，本页面可以关闭。</p>'
            '<p class="muted">如果没有自动生效，请回到 DSH 界面刷新状态。</p>'
        ),
    )


# ── Machine API ──────────────────────────────────────────────────────

@device_bp.route("/api/v1/device/code", methods=["POST"])
@api_operation(
    summary="Start a device authorization",
    description=(
        "Begins the browser-less key hand-off used by the DSH "
        "`enterprise-intranet` plugin. Returns a `user_code` and a "
        "`verification_uri_complete`; the user opens that URL, signs in and "
        "approves, and the plugin's next `POST /api/v1/device/token` poll "
        "receives the freshly minted API key.\n\n"
        "Anonymous by design — the caller has no credential yet, which is the "
        "entire point of the exchange. The `device_code` is the only secret "
        "and is returned exactly once here."
    ),
    tags=["Device authorization"],
    security=[],
    responses={
        "200": ok("A pending authorization request", "DeviceCodeResponse"),
        **errors("429", "500"),
    },
)
def device_code():
    return jsonify(_store().create(base_url=_base_url()))


@device_bp.route("/api/v1/device/token", methods=["POST"])
@api_operation(
    summary="Poll a device authorization",
    description=(
        "Poll with the `device_code` from `POST /api/v1/device/code`. While "
        "nobody has approved the request the answer is HTTP 400 with "
        "`error: \"authorization_pending\"`; poll no faster than the `interval` "
        "the code response named.\n\n"
        "Once approved, HTTP 200 carries the minted `api_key` **exactly once** "
        "— the request is consumed, so a replay answers `expired_token`. Store "
        "the key immediately."
    ),
    tags=["Device authorization"],
    security=[],
    request_body={"required": True, "content": json_body("DeviceTokenRequest")},
    responses={
        "200": ok("Approved — `api_key` is set and shown only here", "DeviceTokenResponse"),
        **errors("400", "500"),
    },
)
def device_token():
    payload = request.get_json(silent=True) or {}
    device_code_value = str(payload.get("device_code") or "").strip()
    if not device_code_value:
        return jsonify({"error": "invalid_request", "error_description": "缺少 device_code"}), 400

    status, granted = _store().redeem(device_code_value)
    if status == "approved" and granted is not None:
        return jsonify({
            "api_key": granted["api_key"],
            "key_id": granted["key_id"],
            "key_name": granted["key_name"],
            "key_prefix": granted["key_prefix"],
            "key_expires_at": granted["key_expires_at"],
            "user": granted["user"],
            "display_name": granted["display_name"],
            "platform_url": _base_url(),
            "token_type": "Bearer",
        })
    if status == "pending":
        return jsonify({
            "error": "authorization_pending",
            "error_description": "等待用户在浏览器中完成授权",
            "interval": 2,
        }), 400
    return jsonify({
        "error": "expired_token",
        "error_description": "授权码不存在或已过期，请重新发起",
    }), 400


__all__ = ["device_bp"]
