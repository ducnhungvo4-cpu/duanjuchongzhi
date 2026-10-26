"""Secret-checked streaming ASGI proxy to the already running workshop API."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

GATEWAY_SECRET = os.environ.get("WORKSHOP_GATEWAY_SECRET", "")
BACKEND_URL = os.environ.get("WORKSHOP_BACKEND_URL", "http://127.0.0.1:8003").rstrip("/")
if not GATEWAY_SECRET:
    raise RuntimeError("WORKSHOP_GATEWAY_SECRET must be configured for the tunnel gateway.")

HOP_BY_HOP = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailers", b"transfer-encoding", b"upgrade", b"host",
}
client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=None, write=None, pool=None), follow_redirects=False)


async def _incoming_body(receive: Any) -> AsyncIterator[bytes]:
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            return
        body = event.get("body", b"")
        if body:
            yield body
        if not event.get("more_body", False):
            return


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    kind = scope["type"]
    if kind == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await client.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    if kind == "websocket":
        await send({"type": "websocket.close", "code": 4403})
        return
    if kind != "http":
        return

    # Reject before touching receive: unauthenticated bodies are never read or forwarded.
    supplied = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"x-workshop-gateway-secret"), "")
    if not hmac.compare_digest(supplied, GATEWAY_SECRET):
        body = b'{"detail":"Gateway authentication required."}'
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return

    method = scope["method"].upper()
    path = scope.get("raw_path", scope["path"].encode("utf-8"))
    query = scope.get("query_string", b"")
    url = f"{BACKEND_URL}{path.decode('ascii', 'surrogateescape')}" + (f"?{query.decode('ascii', 'surrogateescape')}" if query else "")
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in HOP_BY_HOP | {b"authorization", b"x-workshop-gateway-secret"}]
    body_iter = _incoming_body(receive)
    try:
        request = client.build_request(method, url, headers=headers, content=body_iter if method not in {"GET", "HEAD"} else None)
        response = await client.send(request, stream=True)
    except httpx.HTTPError:
        body = b'{"detail":"Local backend is unavailable."}'
        await send({"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return

    response_headers = [(k.lower(), v) for k, v in response.headers.raw if k.lower() not in HOP_BY_HOP]
    await send({"type": "http.response.start", "status": response.status_code, "headers": response_headers})
    try:
        async for chunk in response.aiter_raw():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
    finally:
        await response.aclose()
