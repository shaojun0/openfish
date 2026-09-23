-- Model-routing seed: the route set a fresh openfish deployment starts from.
--
-- The routing table is a database table (`model_routes`), not a file: the
-- `/models` panel edits it in place and a downstream intranet DSH reads it —
-- with real upstream credentials — from `GET /api/v1/models/resolved`.  This
-- script is the shipped starting point; it is **not** run automatically, and
-- nothing is imported from JSON.
--
--   SQLite      sqlite3 data/cpypiserver.db < config/model_routes.seed.sql
--   PostgreSQL  psql "$DATABASE_URL" -f config/model_routes.seed.sql
--   compose     docker compose exec -T backend \
--                 python -c "print(open('/app/config/model_routes.seed.sql').read())" \
--                 | sqlite3 /app/data/cpypiserver.db
--
-- The table itself is created by the application (`Base.metadata.create_all`),
-- so run this against a database the server has started at least once.
--
-- Idempotent: every statement is `ON CONFLICT (name) DO NOTHING` (SQLite ≥3.24
-- and PostgreSQL ≥9.5 both speak it), so re-running never clobbers a route an
-- administrator has since edited, and `id` is deliberately left to the
-- database — naming it would desync a PostgreSQL sequence.
--
-- No secret appears here, and none *can*: a route's upstream key is sealed with
-- Fernet under MODEL_ROUTE_KEY before it is stored, so the ciphertext of one
-- deployment is useless in another and must never be committed.  This script
-- therefore ships every route with an empty `api_key`, and the operator stores
-- the real one afterwards — the `/models` panel, or:
--
--   MODEL_ROUTE_KEY=…  cli.py model-route set-key deepseek-flash \
--                        --key-env ENTERPRISE_DEEPSEEK_API_KEY
--
-- `--key-env` reads the value from the deployment environment at that moment
-- (the secret still never lands in the shell history or in argv), and only the
-- sealed result is written.  Until a key is set, `GET /api/v1/models` reports
-- the route as `api_key_source: "none"` rather than pretending it is configured.
--
-- Each route is classified on two independent axes:
--   provider  the wire format  — openai / mineru / anthropic
--   kind      the model function — chat / completion / embedding / rerank /
--                                  ocr / asr / tts
-- A downstream DSH registers only chat / completion routes as LLM providers;
-- the rest are registry entries for other consumers.  `kind` defaults from the
-- protocol (openai, anthropic → chat; mineru → ocr) but is written out here so
-- the seed says exactly what it means.

-- The intranet default model: its `aliases` include `default`, which is what a
-- downstream DSH adopts as the agent default.
INSERT INTO model_routes
  (name, provider, kind, base_url, path, model, api_key,
   aliases, enabled, description, created_at, updated_at)
VALUES
  ('deepseek-flash', 'openai', 'chat', 'https://api.deepseek.com',
   '/v1/chat/completions', 'deepseek-flash', '',
   '["flash", "default"]', true,
   '默认对话模型（DeepSeek 官方 API）。DSH 企业内网模式会把默认模型设为该路由。',
   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (name) DO NOTHING;

-- Long-context / hard-reasoning workhorse.
INSERT INTO model_routes
  (name, provider, kind, base_url, path, model, api_key,
   aliases, enabled, description, created_at, updated_at)
VALUES
  ('deepseek-v4-pro', 'openai', 'chat', 'https://api.deepseek.com',
   '/v1/chat/completions', 'deepseek-v4-pro', '',
   '["pro"]', true,
   '长上下文 / 复杂推理主力模型。',
   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (name) DO NOTHING;

-- Disabled examples, kept as templates: one non-chat kind (never registered as
-- an LLM provider downstream) and one non-openai protocol.
INSERT INTO model_routes
  (name, provider, kind, base_url, path, model, api_key,
   aliases, enabled, description, created_at, updated_at)
VALUES
  ('bge-m3-embedding', 'openai', 'embedding', 'http://10.0.0.11:8002',
   '/v1/embeddings', 'bge-m3', '',
   '["embedding"]', false,
   '向量化模型示例，尚未上线（示例：enabled=false，下游 DSH 会跳过）。注意 kind=embedding：它不是对话模型，DSH 不会把它注册成 LLM provider。',
   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (name) DO NOTHING;

INSERT INTO model_routes
  (name, provider, kind, base_url, path, model, api_key,
   aliases, enabled, description, created_at, updated_at)
VALUES
  ('mineru-ocr', 'mineru', 'ocr', 'http://10.0.0.12:8000',
   '/file_parse', 'mineru', '',
   '["ocr"]', false,
   '文档解析 / OCR 示例，尚未上线。mineru 协议本身就是 OCR 功能，kind 缺省即为 ocr。',
   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (name) DO NOTHING;
