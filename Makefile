# openfish — repository-wide verification entry point.
#
# The backend used to be verified here too: `make gates-backend` discovered and
# ran every `backend/scripts/check_*.py`.  That directory no longer exists, so
# this file now sequences exactly one thing — the frontend smoke test — and
# nothing pretends the backend is verified when it is not.
#
# `.venv` is the location this project's local deployment uses; the frontend
# target needs `npm` and `frontend/node_modules` and degrades to a warning
# without them.

FRONTEND := frontend

.PHONY: gates-frontend help

help:
	@echo "make gates-frontend  — frontend npm run smoke (skipped without node_modules)"

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
