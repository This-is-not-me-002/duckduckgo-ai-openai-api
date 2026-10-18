# DuckDuckGo AI OpenAI-compatible proxy

An unofficial OpenAI-compatible API for Duck.ai. It drives the normal Duck.ai web interface in Chromium via Playwright because Duck.ai does not publish a supported public chat API. It is not affiliated with DuckDuckGo.

## Features

- OpenAI-style \`POST /v1/chat/completions\` (legacy \`/chat/completions\` remains available).
- OpenAI-style \`GET /v1/models\` and \`GET /models\`.
- Server-sent event streaming (\`text/event-stream\`) with OpenAI-style chunks and \`data: [DONE]\`.
- Conversation history is included in the upstream prompt.
- Configurable overall request deadline, bounded retry count/backoff, and concurrent-request limit.
- Explicit CAPTCHA/security-challenge errors; the proxy does not attempt to solve or bypass them.
- Admin-managed API keys, SQLite by default or PostgreSQL with \`DATABASE_URL\`.
- Admin control plane at `/` for API-key issuance/revocation, request logs, model mappings, live smoke tests, and client setup instructions.
- Render Blueprint configuration and health checks.

## Current models and upstream behavior

The proxy now uses a Playwright browser transport instead of the legacy `duckduckgo_search` chat selector. At startup it attempts to read Duck.ai's current public model catalog and synchronizes the discovered IDs into the model registry. If that catalog cannot be reached, it falls back to the bundled catalog. Model availability can change, and subscription-only models may not be usable by every Duck.ai session.

The fallback catalog includes `gpt-5.6-luna` (the current Luna ID found in the public catalog; `gpt-6-luna` is accepted as an alias), `gpt-5.4-mini`, `claude-haiku-4-5`, `mistral-small-2603`, `tinfoil/gpt-oss-120b`, and `tinfoil/gemma4-31b`. `GET /v1/models` returns the IDs available to this proxy. The live catalog is authoritative when available.

**Important:** Duck.ai does not provide a supported public chat API. Browser automation may be blocked or prohibited by Duck.ai's terms. This proxy uses normal browser interaction and does not hide automation fingerprints, rotate IPs, solve CAPTCHAs, or bypass security challenges. If Duck.ai requires verification, the request fails with a clear challenge error. For reliable production use, use an AI provider with an official API.

## Local setup

Requires Python 3.10 or newer.

\`\`\`bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp example.env .env
\`\`\`

Set a long random \`ADMIN_TOKEN\` in \`.env\`. Keep \`IGNPRE_API_KEYS=False\` for normal use. When authentication is enabled, create a client key with:

\`\`\`bash
curl -X POST http://127.0.0.1:8080/api-keys \\
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"description":"local client"}'
\`\`\`

Run:

\`\`\`bash
uvicorn main:app --host 0.0.0.0 --port 8080
\`\`\`

Health/docs: \`GET /health\`, \`GET /docs\`. Model list and chat endpoints require a valid client API key unless the explicitly unsafe \`IGNPRE_API_KEYS=True\` development override is enabled.

## OpenAI SDK example

\`\`\`python
from openai import OpenAI

client = OpenAI(
    api_key="YOUR_CLIENT_API_KEY",
    base_url="http://127.0.0.1:8080/v1",
)

print(client.models.list())
result = client.chat.completions.create(
    model="gpt-5.6-luna",
    messages=[{"role": "user", "content": "Hello"}],
)
print(result.choices[0].message.content)
\`\`\`

Streaming works with the OpenAI Python SDK's \`stream=True\` option.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| \`IGNPRE_API_KEYS\` | \`False\` | Unsafe authentication bypass; leave disabled in production |
| \`ADMIN_TOKEN\` | empty | Admin token for key management |
| \`DATABASE_URL\` | empty | Optional PostgreSQL URL; SQLite used otherwise |
| \`SQLITE_PATH\` | \`api_keys.db\` | SQLite database file |
| \`UPSTREAM_TIMEOUT_SECONDS\` | \`45\` | Overall upstream response deadline (1–180 seconds) |
| \`UPSTREAM_MAX_RETRIES\` | \`1\` | Retry attempts for likely transient failures (0–3) |
| \`UPSTREAM_RETRY_BACKOFF_SECONDS\` | \`0.5\` | Exponential retry backoff (0.05–5 seconds) |
| \`MAX_CONCURRENT_UPSTREAM\` | \`8\` | Concurrent upstream request slots |
| \`DEFAULT_MODEL\` | \`gpt-4o-mini\` | Default/configured model ID |
| \`DUCKAI_MODELS\` | example registry | Comma-separated model IDs exposed by \`/v1/models\` |
| \`CORS_ORIGINS\` | \`*\` | Comma-separated allowed origins; credentials disabled |
| \`PORT\` | \`8080\` | Listening port |

## Error behavior

Non-streaming upstream failures return OpenAI-style error objects. Timeouts use HTTP 504; CAPTCHA/security challenges use HTTP 503 with code `captcha_or_challenge`; Duck.ai rate-limit/ban responses use HTTP 429 with code `upstream_rate_limited`. Known rate-limit responses are not automatically retried. Once streaming starts, failures are emitted as an SSE error event because HTTP status can no longer be changed. A 429 means Duck.ai is refusing requests from the current session or network; this proxy cannot remove that restriction.

## Deploy on Render

The included \`render.yaml\` creates a Python web service, sets a generated \`ADMIN_TOKEN\`, keeps key validation enabled, and configures \`/health\` as the health check. Review your Render environment variables and persistent-storage/database setup before production use. SQLite files on ephemeral hosting may not persist across restarts; use PostgreSQL for durable key storage.

## Tests

Run the deterministic tests without a live Duck.ai account:

\`\`\`bash
pytest -q
\`\`\`

The tests stub the upstream client and cover health, model listing, conversation history, unknown models, CAPTCHA errors, and SSE framing. They do not verify live upstream availability or a Render deployment.
