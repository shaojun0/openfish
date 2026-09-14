"""Error handler extension — global Flask error handlers."""

from flask import redirect, jsonify, request

from config import settings
from extensions import Extension
from errors import UnauthorizedError, BadRequestError, UploadConflictError
from auth.oauth import get_authorize_url


class ErrorHandlersExtension(Extension):
    name = "error_handlers"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        @app.errorhandler(UnauthorizedError)
        def _401(exc: UnauthorizedError):
            best = request.accept_mimetypes.best_match(["text/html", "application/json"])
            if best == "text/html":
                return redirect(get_authorize_url())
            resp = jsonify({"error": exc.message})
            resp.status_code = 401
            if exc.www_authenticate:
                resp.headers["WWW-Authenticate"] = exc.www_authenticate
            return resp

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