"""Small ASGI body limits applied before multipart parsing/spooling."""

from __future__ import annotations

import json
from collections.abc import Mapping


class RequestBodyTooLarge(RuntimeError):
    pass


class RequestBodyLimitMiddleware:
    """Fail closed for configured paths, with or without Content-Length."""

    def __init__(
        self,
        app,
        *,
        path_limits: Mapping[str, int],
    ) -> None:
        self.app = app
        self.path_limits = {
            str(path): int(limit)
            for path, limit in path_limits.items()
            if str(path).startswith("/") and int(limit) > 0
        }

    @staticmethod
    async def _reject(scope, receive, send, *, limit: int) -> None:
        body = json.dumps(
            {
                "status": "error",
                "code": "request_body_too_large",
                "detail": f"Request body exceeds the {limit}-byte transport limit.",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self.path_limits.get(str(scope.get("path") or ""))
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {
            bytes(key).lower(): bytes(value)
            for key, value in (scope.get("headers") or [])
        }
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                content_length = -1
            if content_length < 0:
                await self._reject(scope, receive, send, limit=limit)
                return
            if content_length > limit:
                await self._reject(scope, receive, send, limit=limit)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        response_started = False

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, limit=limit)
