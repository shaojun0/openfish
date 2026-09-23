"""Error handler extension — global Flask error handlers."""

from __future__ import annotations

import logging

from flask import jsonify, redirect, request

from config import settings
from extensions import Extension
from errors import PypiError, UnauthorizedError, BadRequestError, UploadConflictError
from auth.oauth import get_authorize_url
from services.logsafe import scrub

_log = logging.getLogger("cpypiserver.errors")

#: Blueprints that serve the SPA and machine clients.  These must always answer
#: JSON — an HTML redirect would be followed silently by XHR, and the client
#: would then try to parse a login page as JSON.
#:
#: The artifact-hub ecosystems belong here for the same reason.  `npm install`,
#: `docker pull` and `apt update` are registry clients: handed a 302 to an OAuth
#: consent page they do not retry with credentials, they fail with a confusing
#: parse error.  A JSON 401 carrying a `WWW-Authenticate` challenge is what
#: makes them authenticate instead.
_API_BLUEPRINTS = {
    "session", "api_keys", "admin", "access",
    "npm", "docker", "debian", "hub", "docs",
}

#: What a 5xx says instead of the exception's own text.
#:
#: A ``PypiError(..., status_code=500)`` is raised where the *server* failed —
#: no sealing key, a dead database, a Forgejo call that raised.  Those messages
#: are built from ``str(exc)`` and carry a driver message, a socket error, a
#: filesystem path or a configuration detail: exactly the "system or debug
#: information sent to a remote machine" an audit reports as information
#: disclosure.  The operator still gets the whole thing — in the log, where it
#: belongs — and the caller gets a status with no internals in it.
_SERVER_ERROR_TEXT = "Internal server error"


class ErrorHandlersExtension(Extension):
    name = "error_handlers"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        @app.errorhandler(UnauthorizedError)
        def _401(exc: UnauthorizedError):
            if request.blueprint in _API_BLUEPRINTS or "application/json" in request.headers.get("Accept", ""):
                return _unauthorized_json(exc)

            # Browser navigation: prefer the OAuth flow, otherwise fall back to
            # an HTTP Basic challenge so a Basic-only deployment can still sign
            # in instead of being redirected to an empty authorization URL.
            if settings.auth.oauth2_authorize_url.strip():
                return redirect(get_authorize_url())
            if settings.auth.basic_username and settings.auth.basic_password:
                resp = _unauthorized_json(exc)
                resp.headers["WWW-Authenticate"] = 'Basic realm="cpypiserver"'
                return resp
            return _unauthorized_json(exc)

        # Catch-all for the domain exceptions.  Flask picks the most specific
        # handler, so BadRequestError / UploadConflictError below still win.
        @app.errorhandler(PypiError)
        def _pypi_error(exc: PypiError):
            return _json_error({"error": _client_text(exc)}, exc.status_code)

        @app.errorhandler(404)
        def _404(exc):
            return _json_error({"error": exc.description or "Not found"}, 404)

        @app.errorhandler(405)
        def _405(exc):
            return _json_error({"error": exc.description or "Method not allowed"}, 405)

        @app.errorhandler(413)
        def _413(exc):
            return _json_error({"error": "Upload file too large"}, 413)

        @app.errorhandler(500)
        def _500(exc):
            return _json_error({"error": _SERVER_ERROR_TEXT}, 500)

        @app.errorhandler(BadRequestError)
        def _400(exc):
            return _json_error({"error": _client_text(exc)}, 400)

        @app.errorhandler(UploadConflictError)
        def _409(exc):
            return _json_error({"error": _client_text(exc)}, 409)


#: The statuses whose message may name an internal.  ``500`` and up is the
#: server telling on itself; a ``4xx`` is about the request that just arrived,
#: and withholding it would leave a client unable to fix its own call.
_REDACT_FROM = 500


def _client_text(exc: PypiError) -> str:
    """The message a ``PypiError`` may put on the wire.

    Everything a ``4xx`` carries was written for the caller (``"role not found"``,
    ``"invalid repository slug; expected '<owner>/<name>'"``).  From ``500`` up
    the message is *our* failure and is replaced with a constant — the original
    goes to the log, scrubbed, so the operator keeps the diagnostic.
    """
    if exc.status_code < _REDACT_FROM:
        return exc.message
    _log.error(
        "%s -> %s %s",
        type(exc).__name__,
        exc.status_code,
        scrub(exc.message),
        exc_info=exc,
    )
    return _SERVER_ERROR_TEXT


#: Every error body is JSON, but an error body *quotes* an input (a filename, a
#: package name, an upstream message).  A browser that sniffs such a response as
#: HTML would run any markup in it, so the JSON error surface is pinned with
#: ``nosniff`` here — the one place all of them pass through — rather than
#: relying on each caller to remember.
def _json_error(payload: dict, status: int):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _unauthorized_json(exc: UnauthorizedError):
    resp = _json_error({"error": _client_text(exc)}, 401)
    if exc.www_authenticate:
        resp.headers["WWW-Authenticate"] = exc.www_authenticate
    return resp
