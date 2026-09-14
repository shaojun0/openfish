"""Error handler extension — global Flask error handlers."""

from flask import jsonify, redirect, request

from config import settings
from extensions import Extension
from errors import PypiError, UnauthorizedError, BadRequestError, UploadConflictError
from auth.oauth import get_authorize_url

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
    "npm", "docker", "debian", "hub",
}


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
            return jsonify({"error": exc.message}), exc.status_code

        @app.errorhandler(404)
        def _404(exc):
            return jsonify({"error": exc.description or "Not found"}), 404

        @app.errorhandler(405)
        def _405(exc):
            return jsonify({"error": exc.description or "Method not allowed"}), 405

        @app.errorhandler(413)
        def _413(exc):
            return jsonify({"error": "Upload file too large"}), 413

        @app.errorhandler(500)
        def _500(exc):
            return jsonify({"error": "Internal server error"}), 500

        @app.errorhandler(BadRequestError)
        def _400(exc):
            return jsonify({"error": exc.message}), 400

        @app.errorhandler(UploadConflictError)
        def _409(exc):
            return jsonify({"error": exc.message}), 409


def _unauthorized_json(exc: UnauthorizedError):
    resp = jsonify({"error": exc.message})
    resp.status_code = 401
    if exc.www_authenticate:
        resp.headers["WWW-Authenticate"] = exc.www_authenticate
    return resp
