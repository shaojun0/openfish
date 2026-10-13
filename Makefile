# openfish — repository-wide verification entry point.
#
# This file sequences the two smoke checks the repository has, and nothing
# pretends a half is verified when it is not:
#
#   make smoke-frontend   build the SPA for SSR, render every route in jsdom
#   make smoke-backend    S3/MinIO connectivity self-check (needs a real bucket)
#   make smoke            both, frontend first
#
# CI runs the frontend half only (`.github/workflows/gates.yml`); the backend
# half needs a live bucket, so an operator runs it.  `gates-frontend` is kept as
# an alias of `smoke-frontend` because CI and older notes call it by that name.
#
# The backend used to be verified here too: `make gates-backend` discovered and
# ran every `backend/scripts/check_*.py`.  That directory no longer exists, so
# the target is gone rather than left as a green no-op.
#
# Two properties this file is built around:
#
#   1. Paths come from the Makefile's own location (`ROOT`), so a recipe behaves
#      the same when `make` is invoked from a subdirectory.
#   2. A prerequisite that is absent means *nothing was checked*.  That is a
#      SKIP, and a SKIP is not a pass — the target exits non-zero unless the
#      caller says `ALLOW_SKIP=1` out loud.  Both recipes spell the skip out as
#      a shell function rather than a `$(call ...)` macro: a macro argument
#      splits on commas, which silently truncates the reason and breaks the
#      recipe's exit status.

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
FRONTEND := frontend
BACKEND := backend

# The deployment's interpreter first: the backend needs Python 3.10+ (pydantic
# evaluates `X | None`) plus its dependencies, which a bare `python3` may not be.
PY ?= $(firstword $(wildcard $(ROOT)/$(BACKEND)/.venv/bin/python $(ROOT)/.venv/bin/python) python3)

# The only way a skip may exit 0.  The default is to fail.
ALLOW_SKIP ?= 0

.PHONY: smoke smoke-frontend smoke-backend gates-frontend help

help:
	@echo "make smoke           — frontend + backend smoke, frontend first"
	@echo "make smoke-frontend  — npm run smoke: SSR build, then every route rendered in jsdom"
	@echo "make smoke-backend   — S3/MinIO connectivity self-check (needs OBJECT_BACKEND=s3)"
	@echo "make gates-frontend  — alias of smoke-frontend (what CI calls)"
	@echo ""
	@echo "A missing prerequisite skips the run and fails the target; pass"
	@echo "ALLOW_SKIP=1 to accept an unverified run and exit 0."

# ── Frontend ─────────────────────────────────────────────────────────
# `npm run smoke` builds the SPA for SSR and renders every route through jsdom.
# What a fresh checkout may lack is `frontend/node_modules` (one `npm ci` away)
# and `npm` on PATH; both are skips, and a skip fails the target.
#
# The `set -e` is the important part: without it a failing build still printed
# "✅ frontend smoke passed", because the recipe's exit status is the *last*
# command's.  A real failure must fail the target.
smoke-frontend:
	@cd "$(ROOT)" && set -e; \
	skip() { echo "⏭  SKIPPED — NOT VERIFIED: $$1"; \
		echo "   Nothing was checked.  Pass ALLOW_SKIP=1 to accept that and exit 0."; \
		[ "$(ALLOW_SKIP)" = "1" ] || exit 1; }; \
	if [ ! -d "$(FRONTEND)/node_modules" ]; then \
		skip "$(FRONTEND)/node_modules is missing — install it with: cd $(FRONTEND) && npm ci"; \
	elif ! command -v npm > /dev/null 2>&1; then \
		skip "npm is not on PATH — install Node 24 (see .github/workflows/gates.yml)"; \
	else \
		echo ""; \
		echo "── frontend npm run smoke ─────────────────────────────"; \
		( cd $(FRONTEND) && npm run smoke ); \
		echo ""; \
		echo "✅ frontend smoke passed"; \
	fi

# CI and older notes call this target; keep the name working.
gates-frontend: smoke-frontend

# ── Backend ──────────────────────────────────────────────────────────
# The operator tool, not a gate: it needs a reachable bucket.  It restores the
# bucket as it found it (everything it writes lives under `smoke/`), so running
# it against a live deployment is safe.  Exit 2 from the script means "nothing
# was checked" — no `OBJECT_BACKEND=s3`, or an interpreter too old — which is
# the same SKIP as a missing prerequisite, never a pass.
smoke-backend:
	@cd "$(ROOT)" && \
	skip() { echo "⏭  SKIPPED — NOT VERIFIED: $$1"; \
		echo "   Nothing was checked.  Pass ALLOW_SKIP=1 to accept that and exit 0."; \
		[ "$(ALLOW_SKIP)" = "1" ] || exit 1; }; \
	if [ ! -x "$(PY)" ] && ! command -v $(PY) > /dev/null 2>&1; then \
		skip "no Python interpreter — set PY=… or create $(BACKEND)/.venv"; \
	else \
		echo "── backend s3 smoke ($(PY)) ──────────────────────────"; \
		status=0; "$(PY)" .agent/scripts/backend/s3_smoke.py || status=$$?; \
		if [ $$status -eq 2 ]; then \
			skip "nothing was checked (exit 2) — see the reason above: it needs OBJECT_BACKEND=s3 and Python 3.10+"; \
		elif [ $$status -ne 0 ]; then \
			exit $$status; \
		fi; \
	fi

# Both halves, frontend first (it is the fast one).
smoke: smoke-frontend smoke-backend
