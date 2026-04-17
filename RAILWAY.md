# Deploy on Railway (FastAPI + Next.js + Haiku)

Use **two Railway services** from this repo: one API (Python), one web (Next.js).

## Railway CLI (this repo)

Prereqs: [Railway CLI](https://docs.railway.com/develop/cli) installed and `railway login`.

### Bootstrap (one-time)

From `trasco-asin/`:

```bash
cd trasco-asin
railway link -p TRASCO
railway add --service trasco-api
railway add --service trasco-web
```

That links the folder to project **TRASCO** and creates empty services **trasco-api** and **trasco-web** (names may differ in your account). Use **`-s trasco-api`** when working at the repo root, and link **`frontend/`** with **`-s trasco-web`** for the Next app.

### Deploy the API (repo root)

```bash
cd trasco-asin
railway link -p TRASCO -s trasco-api
railway variables --set "KEEPA_API_KEY=..." --set "ANTHROPIC_API_KEY=..." --set "CORS_ALLOW_ORIGINS=https://your-frontend.up.railway.app"
railway up
```

Use `-d` to detach from logs: `railway up -d`. If **`railway up` times out** (upload to Railway), run it from your own terminal or **connect the GitHub repo** in the dashboard and deploy from Git instead of CLI upload.

### Deploy the frontend (`frontend/` as service root)

In the Railway dashboard, set service **trasco-web** root directory to **`frontend`**, or from CLI:

```bash
cd trasco-asin/frontend
railway link -p TRASCO -s trasco-web
railway variables --set "NEXT_PUBLIC_API_BASE=https://your-api.up.railway.app"
npm ci && npm run build
railway up
```

Set `NEXT_PUBLIC_API_BASE` **before** `npm run build` (or set it in Railway and use a build command that exports it). Redeploy the frontend after changing it.

### Useful commands

| Command | Purpose |
|---------|---------|
| `railway status` | Project / env / linked service |
| `railway list` | List projects |
| `railway open` | Open project in browser |
| `railway logs` | Stream deploy logs |
| `railway variables -k` | Print variables as `KEY=value` |

Repo root includes `railway.toml` and `Procfile` for the API; `frontend/railway.toml` documents the web start command when root is `frontend`.

## 1. Backend service

1. **New project → Deploy from GitHub** (or empty service + connect repo).
2. **Settings → Service → Root directory**: leave empty (repo root is `trasco-asin`, the folder that contains `backend/` and `requirements.txt`).
3. **Settings → Deploy → Custom start command** (if Railway does not pick up `Procfile`):

   ```bash
   uvicorn backend.main:app --host 0.0.0.0 --port $PORT
   ```

4. **Variables** (minimum for production with Claude Haiku headers):

   | Variable | Notes |
   |----------|--------|
   | `KEEPA_API_KEY` | Required for `/api/v1/process`. |
   | `ANTHROPIC_API_KEY` | Required for Haiku-based header / column mapping when Ollama is not used. |
   | `HAIKU_MODEL` | Optional; default in code is `claude-3-5-haiku-20241022`. Set to your Haiku model id (e.g. newer Haiku 4.5 id from Anthropic docs). |
   | `CORS_ALLOW_ORIGINS` | **Required** for the browser UI: your frontend public URL, e.g. `https://<frontend-service>.up.railway.app`. Comma-separate if you have several. |
   | `USE_OLLAMA_ASIN_VALIDATE` | Set `false` if you do not run Ollama in production. |
   | `USE_OLLAMA_RESOLVER_GEMMA` | Set `false` without Ollama. |
   | `USE_OLLAMA_SHEET_DOMAIN` | Set `false` without Ollama (uses `KEEPA_DOMAIN` only). |
   | `TRASCO_PROCESS_HISTORY_DIR` | Optional; defaults to `.trasco_process_history` under the app cwd (ephemeral on Railway unless you attach a volume). |

5. Copy the **public URL** of this service (e.g. `https://trasco-api-production-xxxx.up.railway.app`). You will plug it into the frontend as `NEXT_PUBLIC_API_BASE`.

## 2. Frontend service

1. **New service** in the same Railway project → same repo.
2. **Root directory**: `frontend` (the Next.js app).
3. **Build command** (if not auto-detected):

   ```bash
   npm ci && npm run build
   ```

4. **Start command**:

   ```bash
   npm run start
   ```

   Next.js listens on `PORT` from Railway; the script binds `0.0.0.0`.

5. **Variables**:

   | Variable | Notes |
   |----------|--------|
   | `NEXT_PUBLIC_API_BASE` | Full backend URL, **no trailing slash**, e.g. `https://trasco-api-production-xxxx.up.railway.app`. Must be set **before** `npm run build` so it is inlined into the client bundle. After changing it, **redeploy** the frontend. |

6. Set **`CORS_ALLOW_ORIGINS`** on the **backend** to this frontend’s public URL.

## 3. Haiku-only behavior (no Ollama on Railway)

- In the UI, you can **turn off** “Use Ollama (Gemma) for header detection”.
- If **`ANTHROPIC_API_KEY`** is set on the API, the parser still runs **Claude Haiku** for the **largest sheet** (and for CSV), so column mapping works without Ollama.
- SKU resolver Gemma, per-sheet Keepa domain, and ASIN validation still expect Ollama unless you disable those flags with the env vars above.

## 4. Health check

Point Railway’s health check path to: **`/health`**.

## 5. Local `.env`

Copy `.env.example` to `.env` at the repo root for local `uvicorn`; the frontend can use `frontend/.env.local` with `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000`.
