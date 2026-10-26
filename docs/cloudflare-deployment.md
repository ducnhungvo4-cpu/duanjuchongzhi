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

For the current free bridge, `cloudflare/Start-Bridge.ps1 -InstallTask` registers and starts the current-user logon task `VideoRemakeWorkshop-Bridge`. It reuses a healthy backend on `127.0.0.1:8003`; if that port has no listener, it starts the same FastAPI app through the existing Python environment and waits for its health check. If the port is occupied but unhealthy, it leaves that process untouched and records an error. The task starts the secret-checking gateway on loopback port 8004, launches a hidden `cloudflared` Quick Tunnel, and updates the Worker backend URL and origin-secret bindings when the tunnel hostname changes. Credentials are read only from the ignored `cloudflare/.deploy-credentials.json`; the scheduled task and script contain no credentials. The gateway rejects requests before it reads a body, then streams requests and responses to the existing backend, removing browser Basic Auth before forwarding. It periodically checks the tunnel health and restarts a rejected or repeatedly unhealthy Quick Tunnel. The computer must remain powered on, awake, and connected; sleep or shutdown interrupts the service. Quick Tunnels are temporary and have no uptime guarantee, and a restart may take time to update the Worker route.

The site remains protected by Worker Basic Auth. The random origin secret is separate and is injected only by the Worker; it protects the local API even if a Quick Tunnel URL is discovered. The free Cloudflare account limit is 100 MB per HTTP request body, so larger video uploads are rejected before reaching the local backend. The Worker API and media routes are same-origin; file links use `/api/files/...` relative paths and support HTTP Range requests.

Cloudflare Containers require the Workers Paid plan in this account. The current config does not publish a backend container because the app stores SQLite and user media on disk, while Containers do not provide a durable POSIX volume for that data. Do not place the SQLite database on an R2 FUSE mount. A standalone Cloudflare deployment needs a separately designed durable database and object-storage path before the backend can be moved there.
