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
   | `ANTHROPIC_API_KEY` | **Recommended (production):** Claude Haiku maps columns / headers on the largest sheet and CSV. |
   | `HAIKU_MODEL` | Default **`claude-haiku-4-5-20251001`**. Older `claude-3-5-haiku-20241022` is **retired** (API returns **404**). Override with any valid Messages API model id from [Anthropic models](https://docs.anthropic.com/en/docs/about-claude/models). |
   | `CORS_ALLOW_ORIGINS` | **Required** for the browser UI: comma-separated origins (no path), e.g. `https://trasco-web-production.up.railway.app`. Non-local hosts must use `https://`. Trailing slashes are stripped. If unset, only `localhost` / `127.0.0.1` dev origins are allowed. |
   | `USE_OLLAMA_ASIN_VALIDATE` | Default **true**: LLM ASIN vs listing check uses **Haiku** when `ANTHROPIC_API_KEY` is set, else **Ollama** if reachable. Set `false` to skip that step entirely. |
   | `USE_OLLAMA_RESOLVER_GEMMA` | Default **false**. **Haiku** runs finder pick/escalation whenever `ANTHROPIC_API_KEY` is set; set this `true` only if you also want **Ollama** in the mix when a URL is configured. |
   | `USE_OLLAMA_SHEET_DOMAIN` | Default **false**. **Haiku** infers per-sheet Keepa domain whenever `ANTHROPIC_API_KEY` is set; set this `true` only if you also want **Ollama** domain inference when reachable. |
   | `TRASCO_CACHE_DB` | Optional. Absolute path to the SQLite cache file; use with a **volume** so Keepa cache survives redeploys (see **§6**). |
   | `TRASCO_PROCESS_HISTORY_DIR` | Optional. Directory for “Recent” Excel files + manifest; use with a **volume** (see **§6**). |

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
   | `NEXT_PUBLIC_USE_OLLAMA` | Omit or leave unset for **Haiku-first** (browser sends `use_ollama=false`). Set to **`true`** only if you run Ollama and want the UI to request Gemma paths from the API. |
   | `NIXPACKS_NODE_VERSION` | Set to **`20`** if the builder still picks Node 18 (Next 16 needs **≥ 20.9**). The repo also ships `frontend/.nvmrc`, `frontend/.node-version`, `frontend/nixpacks.toml`, and `package.json` `engines.node` to prefer Node 20. |

6. Set **`CORS_ALLOW_ORIGINS`** on the **backend** to this frontend’s public URL.

## 3. Default: Claude Haiku (production)

- **`ANTHROPIC_API_KEY`** + **`HAIKU_MODEL`** on the API drive **column / header mapping** (largest sheet + CSV). This is the intended **Railway** setup.
- Keep **`USE_OLLAMA_*`** on the API set to **`false`** unless you deploy **Ollama** reachable from the API.
- The web app defaults to **`use_ollama=false`** on `/process/start` (Haiku path). Set **`NEXT_PUBLIC_USE_OLLAMA=true`** on the frontend service only if you intentionally use Ollama from the browser.

## 4. Health check

Point Railway’s health check path to: **`/health`**.

## 5. Local `.env`

Copy `.env.example` to `.env` at the repo root for local `uvicorn`. For the Next app, use `frontend/.env.local` with e.g. `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000` (and optional `NEXT_PUBLIC_USE_OLLAMA=true` if you run Ollama locally).

## 6. Cache, “Recent” downloads, and whether you need a database

**You do not need a separate Railway database (Postgres, etc.).** The API uses:

| Feature | Storage | Works on Railway without extra services? |
|--------|---------|-------------------------------------------|
| **Keepa / resolver caching** | **SQLite** file (`data/trasco_cache.sqlite3` by default) | **Yes.** Same container disk; cache is shared across all requests on that instance. |
| **Recent runs + re-download** | Directory **`.trasco_process_history`** (manifest + per-job `.xlsx`) | **Yes** for the same deploy. |
| **Upload → process → download** | In-memory job + browser download | **Yes**, as long as `KEEPA_API_KEY`, **`CORS_ALLOW_ORIGINS`**, and **`NEXT_PUBLIC_API_BASE`** (no trailing slash, set before frontend build) are correct. |

**Ephemeral disk:** Railway’s default filesystem is **wiped on redeploy** and is **not shared** if you ever run **multiple replicas** of the API. After a redeploy, the SQLite cache and “Recent” files are gone until rebuilt by traffic (cache repopulates from Keepa; history starts empty). That is normal unless you add persistence.

**Optional: keep cache + history across redeploys** — add a **Railway volume**, mount it (e.g. `/data`), then set on the **API** service:

| Variable | Example | Purpose |
|----------|---------|--------|
| `TRASCO_CACHE_DB` | `/data/trasco_cache.sqlite3` | SQLite cache file path (Keepa JSON + resolver tiers). |
| `TRASCO_PROCESS_HISTORY_DIR` | `/data/process_history` | Folder for `manifest.json` and `{job_id}.xlsx` for the UI “Recent” table. |

Create the mount once in the Railway dashboard; both paths must live **on the volume**. Still **no Postgres** — only paths change.

**Ollama:** Default UI sends `use_ollama=false` (Haiku-first). Use **`NEXT_PUBLIC_USE_OLLAMA=true`** and **`USE_OLLAMA_*=true`** on the API only when Ollama is deployed (see §3).
