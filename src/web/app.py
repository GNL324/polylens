from __future__ import annotations

import os
from pathlib import Path

from src.web.dashboard import create_dashboard
from src.web.mission_control import create_mission_control_page


def run_web_dashboard(host: str | None = None, port: int | None = None, db_path: str | Path = "data/polylens.db") -> None:
    from nicegui import app, ui

    bind_host = host or os.environ.get("POLYLENS_WEB_HOST") or "127.0.0.1"
    bind_port = int(port or os.environ.get("POLYLENS_WEB_PORT") or 8787)
    password = os.environ.get("POLYLENS_WEB_PASSWORD")

    if password:
        @app.middleware("http")
        async def password_gate(request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path.startswith("/_nicegui"):
                return await call_next(request)
            supplied = request.query_params.get("password") or request.headers.get("x-polylens-password")
            if supplied != password:
                from starlette.responses import PlainTextResponse

                return PlainTextResponse("Polylens dashboard password required", status_code=401)
            return await call_next(request)

    create_dashboard(db_path=db_path)
    create_mission_control_page(polylens_db_path=db_path)
    ui.run(host=bind_host, port=bind_port, title="Polylens Command Center", reload=False, show=False)

