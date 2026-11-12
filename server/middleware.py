from __future__ import annotations

import json

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimit:
    """Refuse API request bodies over `max_bytes`, with or without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    await _reject(send)
                    return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI passes HTTPException through its body parser unchanged.
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)


async def _reject(send: Send) -> None:
    body = json.dumps({"code": "payload_too_large", "detail": "Request is too large."}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
