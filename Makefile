# openfish — repository-wide verification entry point.
#
# `make gates` is the single command a human, an agent and CI all run, so there
# is exactly one definition of "green".  It is deliberately a thin wrapper: each
# gate is a script under backend/scripts/ that can be run on its own, and this
# file only sequences them and adds the frontend smoke test.
#
# The backend gates are run from `backend/` because several of them resolve
# paths (`data/`, `openapi/`, `scripts/`) relative to that directory, and because
# they import the application as a top-level package.
#
# `.venv` is the location this project's local deployment uses; `PYTHON` can be
# overridden (`make gates PYTHON=python3`) for CI or a system interpreter.
#
# One gate is special: `check_contract.py` validates *live* responses against the
# published OpenAPI document, so it needs a running server and an API key.  The
# `contract-gate` target below boots a throwaway instance on an isolated database
# and port, mints a key, runs the gate and tears it all down again — which is why
# `make gates` is self-contained and CI needs no extra service container.

PYTHON ?= .venv/bin/python
BACKEND := backend
FRONTEND := frontend

# `gates-backend` does **not** keep a hand-written list of gates: it calls
# `services.gates`, which discovers every `check_*.py` beside it (and skips the
# live-server gate).  That is the same entry point the agent runtime uses, so a
# newly added gate is enforced here and in CI without editing this file — the
# two can never disagree about what "green" means.
#
# `check_contract.py` is the one exception and keeps its own target below.
CONTRACT_GATE := scripts/check_contract.py

# Port and credentials for the throwaway contract server.  Deliberately not the
# development defaults, so running this can never collide with a server the
# developer already has on 9090.
CONTRACT_PORT ?= 19717
CONTRACT_BASE_URL ?= http://127.0.0.1:$(CONTRACT_PORT)
CONTRACT_USER ?= contract-gate
CONTRACT_PASS ?= contract-gate-secret

.PHONY: gates gates-backend gates-frontend contract-gate checks help

help:
	@echo "make gates           — every backend gate, then the frontend smoke test"
	@echo "make gates-backend   — backend/scripts/check_*.py (incl. the live contract gate)"
	@echo "make gates-frontend  — frontend npm run smoke (skipped without node_modules)"
	@echo "make checks          — list the checks this Makefile runs"

checks:
	@echo "  backend: every scripts/check_*.py, discovered by \`python -m services.gates\`"
	@echo "  backend/$(CONTRACT_GATE)  (needs the throwaway server: make contract-gate)"
	@echo "  frontend: npm run smoke"

# ── Everything ───────────────────────────────────────────────────────
# Both halves always run: the frontend recipe never fails when its dependencies
# are absent (it prints a warning and returns success), so a missing
# `node_modules` cannot hide a backend failure and vice versa.
gates: gates-backend gates-frontend

# ── Backend ──────────────────────────────────────────────────────────
gates-backend: contract-gate
	@echo ""
	@echo "── discovered gates (services.gates) ──────────────────"
	@cd $(BACKEND) && $(PYTHON) -m services.gates
	@echo ""
	@echo "✅ all backend gates passed"

# Boot `app.py` on a temporary SQLite database, wait for /health, mint an API key
# through the same `ApiKeyManager` the server uses, then run the live contract
# gate.  The server is killed and the database removed on every exit path; `set -e`
# inside a single shell is what makes the trap reliable, so keep this as one line
# list rather than separate recipes.
contract-gate:
	@set -e; \
	tmp=$$(mktemp -d "$${TMPDIR:-/tmp}/openfish-contract-XXXXXX"); \
	srv_pid=""; \
	cleanup() { \
		if [ -n "$$srv_pid" ]; then kill "$$srv_pid" 2>/dev/null || true; wait "$$srv_pid" 2>/dev/null || true; fi; \
		rm -rf "$$tmp"; \
	}; \
	trap cleanup EXIT INT TERM; \
	echo ""; \
	echo "── $(CONTRACT_GATE) (live server on $(CONTRACT_BASE_URL)) ─────"; \
	( \
		cd $(BACKEND) && \
		DATABASE_URL="sqlite:///$$tmp/contract.db" \
		HOST=127.0.0.1 PORT=$(CONTRACT_PORT) \
		ADMIN_USERS='["$(CONTRACT_USER)"]' \
		AUTH_USERNAME=$(CONTRACT_USER) AUTH_ASSERT=$(CONTRACT_PASS) \
		OAUTH2_INTROSPECT_URL= OAUTH2_AUTHORIZE_URL= \
		$(PYTHON) app.py > "$$tmp/server.log" 2>&1 \
	) & \
	srv_pid=$$!; \
	ready=""; \
	for _ in $$(seq 1 60); do \
		if curl -sf "$(CONTRACT_BASE_URL)/health" > /dev/null 2>&1; then ready=1; break; fi; \
		sleep 0.5; \
	done; \
	if [ -z "$$ready" ]; then \
		echo "❌ the contract server did not become healthy on $(CONTRACT_BASE_URL)"; \
		cat "$$tmp/server.log"; \
		exit 1; \
	fi; \
	key=$$( \
		cd $(BACKEND) && \
		DATABASE_URL="sqlite:///$$tmp/contract.db" \
		$(PYTHON) scripts/mint_contract_key.py --user $(CONTRACT_USER) --name contract-gate \
	); \
	if [ -z "$$key" ]; then echo "❌ could not mint a contract API key"; exit 1; fi; \
	( cd $(BACKEND) && $(PYTHON) $(CONTRACT_GATE) --base-url "$(CONTRACT_BASE_URL)" --api-key "$$key" )

# ── Frontend ─────────────────────────────────────────────────────────
# `npm run smoke` builds the SPA for SSR and renders every route through jsdom.
# It needs two things a fresh checkout may not have — `frontend/node_modules`
# (one `npm ci` away) and `npm` on PATH.  Both are warnings, not failures, so
# this target is usable from a machine that has never built the frontend.
#
# The `set -e` is the important part: without it a failing build still printed
# "✅ frontend smoke passed", because the recipe's exit status is the *last*
# command's.  A real failure must fail the target.
gates-frontend:
	@set -e; \
	if [ ! -d "$(FRONTEND)/node_modules" ]; then \
		echo "⚠ $(FRONTEND)/node_modules is missing — skipping the frontend smoke test."; \
		echo "  Install it with: cd $(FRONTEND) && npm ci"; \
	elif ! command -v npm > /dev/null 2>&1; then \
		echo "⚠ npm is not on PATH — skipping the frontend smoke test."; \
		echo "  Install Node (see .github/workflows/gates.yml, node 20) or put it on PATH."; \
	else \
		echo ""; \
		echo "── frontend npm run smoke ─────────────────────────────"; \
		( cd $(FRONTEND) && npm run smoke ); \
		echo ""; \
		echo "✅ frontend smoke passed"; \
	fi
