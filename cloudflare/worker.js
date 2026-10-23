const unauthorized = () => new Response("Authentication required", {
  status: 401,
  headers: { "WWW-Authenticate": 'Basic realm="Video Remake Workshop", charset="UTF-8"' },
});

function sameText(a, b) {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i += 1) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

function authorized(request, env) {
  if (!env.ACCESS_PASSWORD) return false;
  const username = env.ACCESS_USER || "owner";
  const expected = `Basic ${btoa(`${username}:${env.ACCESS_PASSWORD}`)}`;
  return sameText(request.headers.get("Authorization") || "", expected);
}

async function proxyApi(request, env) {
  const sourceUrl = new URL(request.url);
  const requestPath = `${sourceUrl.pathname}${sourceUrl.search}`;

  if (env.BACKEND) {
    const container = env.BACKEND.get(env.BACKEND_CONTAINER_ID || "video-remake-workshop");
    return container.fetch(new Request(request.url, request));
  }

  if (!env.BACKEND_URL) {
    return Response.json({ detail: "Backend is not configured." }, { status: 503 });
  }
  if (!env.BACKEND_SHARED_SECRET) {
    return Response.json({ detail: "Backend gateway protection is not configured." }, { status: 503 });
  }

  const target = `${env.BACKEND_URL.replace(/\/+$/, "")}${requestPath}`;
  const headers = new Headers(request.headers);
  headers.delete("Authorization");
  if (env.BACKEND_SHARED_SECRET) {
    headers.set("X-Workshop-Gateway-Secret", env.BACKEND_SHARED_SECRET);
  }
  headers.delete("Host");
  return fetch(new Request(target, { method: request.method, headers, body: request.body, redirect: "manual" }));
}

export default {
  async fetch(request, env) {
    if (!authorized(request, env)) return unauthorized();

    const url = new URL(request.url);
    if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
      return proxyApi(request, env);
    }

    const assetResponse = await env.ASSETS.fetch(request);
    if (assetResponse.status !== 404 || !request.headers.get("Accept")?.includes("text/html")) {
      return assetResponse;
    }

    const indexUrl = new URL("/index.html", request.url);
    return env.ASSETS.fetch(new Request(indexUrl, request));
  },
};
