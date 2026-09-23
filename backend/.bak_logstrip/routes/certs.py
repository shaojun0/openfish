"""Publish the private CA chain so intranet clients can install it.

Why this is a route and not a static file
-----------------------------------------
``pip``, ``npm``, ``docker`` and ``apt`` all refuse a mirror whose TLS chain the
client does not trust, so the CA has to be fetchable before anyone has
credentials.  It used to be served by Flask's blanket ``static`` handler out of
``static/certs/`` — a directory ``.gitignore`` designates for TLS material, which
made "where do I put the CA" and "where do I put the server key" the same
answer.  A key dropped there was downloadable by anyone.

So the file moved out of the web root (``TLS_CA_FILE``, default
``certs/ca_chain.pem``) and this module serves **one configured file**, never a
directory listing.  The endpoint is public on purpose; the permission system is
not the right control here because the client needs the CA *before* it can be
authenticated.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, send_file

from config import settings

certs_bp = Blueprint("certs", __name__)

#: The CA chain is PEM, and that is the only thing this endpoint will ever emit.
_CA_MIMETYPE = "application/x-pem-file"


@certs_bp.route("/certs/ca_chain.pem")
def ca_chain():
    """Serve the configured CA chain, or 404 when the operator has not set one.

    Anonymous by design: an intranet client has to fetch this before it can
    trust the mirror at all.  Only the single configured path is readable — a
    key placed next to it is not reachable through this route.
    """
    path = Path(settings.server.tls_ca_file)
    if not path.is_file():
        abort(404, description="No CA chain configured")
    return send_file(path, mimetype=_CA_MIMETYPE)
