"""Secret-checked ASGI entrypoint for a Cloudflare Tunnel origin."""

from __future__ import annotations

import hmac
import os
from typing import Any

from backend.app.main import app as api_app

GATEWAY_SECRET = os.environ.get("WORKSHOP_GATEWAY_SECRET", "")
if not GATEWAY_SECRET:
    raise RuntimeError("WORKSHOP_GATEWAY_SECRET must be configured for the tunnel gateway.")


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] not in {"http", "websocket"}:
        await api_app(scope, receive, send)
        return

    supplied = next(
        (value.decode("latin-1") for name, value in scope.get("headers", []) if name.lower() == b"x-workshop-gateway-secret"),
        "",
    )
    if hmac.compare_digest(supplied, GATEWAY_SECRET):
        await api_app(scope, receive, send)
        return

    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4403})
        return

    body = b'{"detail":"Gateway authentication required."}'
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})
