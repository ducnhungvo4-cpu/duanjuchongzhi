# Cloudflare deployment preparation

The Cloudflare Worker serves the built frontend, requires HTTP Basic Auth for every route, and forwards `/api/*` only when a backend has been configured. It returns `503` when the backend is missing, and fails closed if a URL backend has no shared gateway secret. `ACCESS_PASSWORD`, `BACKEND_URL`, and `BACKEND_SHARED_SECRET` belong in Wrangler secrets, never in this repository or frontend build variables.

The production Vite build writes only the app shell and referenced bundle to `cloudflare/assets`; it does not copy `frontend/public/design-proposals` or the ignored local API setting.

Build the frontend with a same-origin API base. The trailing slash is normalized to an empty base by the app, and the ignored `frontend/.env.local` must not be copied into a build environment.

```powershell
$env:VITE_API_BASE = "/"
npm --prefix frontend run build
Remove-Item Env:VITE_API_BASE
npx wrangler@latest deploy --config cloudflare/wrangler.jsonc
```

Before deploying, set `ACCESS_PASSWORD` using `npx wrangler@latest secret put ACCESS_PASSWORD --config cloudflare/wrangler.jsonc`. Use a unique, long random password with ASCII characters. The worker username defaults to `owner` and can be changed in the non-secret `ACCESS_USER` variable.

The Docker image includes the FastAPI backend, Python requirements, FFmpeg, and ffprobe. `compose.yaml` mounts the SQLite database and media directory on a persistent Docker volume and binds its API and gateway ports to loopback only. Copy `.env.example` to `.env` for a self-hosted run and fill in both secrets locally; `.env` is ignored by Git.

For a temporary free tunnel, run the app and its secret-checking gateway with Docker Compose, then point Cloudflare Tunnel at `http://127.0.0.1:8004`. Set the tunnel's current HTTPS URL as the Worker `BACKEND_URL` secret and the same random gateway value as both Worker `BACKEND_SHARED_SECRET` and local `WORKSHOP_GATEWAY_SECRET`. The gateway secret prevents callers who discover the tunnel URL from reaching the API directly. This mode depends on the host computer and tunnel staying online.

Cloudflare Containers require the Workers Paid plan in this account. The current config does not publish a backend container because the app stores SQLite and user media on disk, while Containers do not provide a durable POSIX volume for that data. Do not place the SQLite database on an R2 FUSE mount. A standalone Cloudflare deployment needs a separately designed durable database and object-storage path before the backend can be moved there.
