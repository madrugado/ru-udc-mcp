"""Vercel Function: RuUDC MCP поверх Streamable HTTP (stateless, JSON-ответы)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # .../api
_ROOT = _HERE.parent  # корень проекта


def _find_data_dir() -> Path:
    for candidate in (_ROOT / "data", _HERE / "data", Path.cwd() / "data"):
        if (candidate / "teacode_udc.json").is_file():
            return candidate
    raise RuntimeError(
        "UDC data files not found in the function bundle; "
        "check includeFiles in vercel.json"
    )


os.environ["UDC_DATA_DIR"] = str(_find_data_dir())
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.transport_security import TransportSecuritySettings

import server  # noqa: E402  (импортирует данные и регистрирует инструменты)

session_manager = StreamableHTTPSessionManager(
    app=server.mcp._lowlevel_server,
    json_response=True,  # без SSE — корректно работает в serverless
    stateless=True,  # без привязки сессии к инстансу
    # публичный endpoint за прокси Vercel: стандартная защита от DNS rebinding
    # заворачивает запросы с чужим Host
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

mcp_endpoint = StreamableHTTPASGIApp(session_manager)


async def health(request) -> JSONResponse:
    return JSONResponse({"status": "ok", "sources": sorted(server.SOURCES)})


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        Route("/api/index.py", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
    ],
    lifespan=lifespan,
)
