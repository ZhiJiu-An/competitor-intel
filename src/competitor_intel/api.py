"""FastAPI 服务（P1 / TDD 7.4）。

四个契约端点：POST /runs、GET /runs/{id}、GET /snapshots、GET /reports/latest。
设计约束：
- 只做协议转换，执行交给 Celery（或测试注入的 dispatcher）；
- 鉴权可选：配置 API_TOKEN 后要求 X-API-Token，未配置时默认仅监听本机。
"""

import json
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .config import Settings
from .service import DEFAULT_CONFIG, enqueue_run
from .store import get_run, init_db, list_snapshot_records

Dispatcher = Callable[[str, str, Optional[str], bool], None]


class RunRequest(BaseModel):
    competitor: Optional[str] = None
    force: bool = False
    config: Optional[str] = None


def _default_dispatch(run_id: str, config_path: str, competitor: Optional[str], force: bool) -> None:
    from .tasks import execute_run_task

    execute_run_task.delay(run_id, config_path, competitor, force)


def create_app(
    settings: Optional[Settings] = None,
    *,
    dispatcher: Optional[Dispatcher] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    dispatch = dispatcher or _default_dispatch

    app = FastAPI(title="竞品情报服务", version="0.2")

    def _auth(x_api_token: Optional[str] = Header(default=None)) -> None:
        if settings.api_token and x_api_token != settings.api_token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/healthz")
    async def healthz() -> dict:
        try:
            await init_db(settings.db_path)
            db_status = "ok"
        except Exception as exc:  # 探活不抛 500，便于编排系统读取原因
            db_status = f"error: {exc}"
        return {"status": "ok" if db_status == "ok" else "degraded", "db": db_status}

    @app.post("/runs", dependencies=[Depends(_auth)])
    async def trigger_run(body: RunRequest) -> JSONResponse:
        record, created = await enqueue_run(
            settings,
            competitor=body.competitor,
            force=body.force,
            config_path=body.config or DEFAULT_CONFIG,
        )
        if created:
            dispatch(
                record["id"], body.config or DEFAULT_CONFIG, body.competitor, body.force
            )
        return JSONResponse(
            status_code=202 if created else 200,
            content={
                "run_id": record["id"],
                "status": record["status"],
                "idempotent": not created,
            },
        )

    @app.get("/runs/{run_id}", dependencies=[Depends(_auth)])
    async def read_run(run_id: str) -> dict:
        record = await get_run(settings.db_path, run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        record["counts"] = json.loads(record.pop("counts_json") or "{}")
        return record

    @app.get("/snapshots", dependencies=[Depends(_auth)])
    async def read_snapshots(
        competitor: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict:
        rows = await list_snapshot_records(
            settings.db_path,
            competitor=competitor,
            since=since,
            until=until,
            limit=limit,
        )
        return {"count": len(rows), "items": rows}

    @app.get("/reports/latest", dependencies=[Depends(_auth)])
    async def latest_report() -> PlainTextResponse:
        report_dir = Path(settings.report_dir)
        reports = sorted(
            report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not reports:
            raise HTTPException(status_code=404, detail="no report yet")
        return PlainTextResponse(
            reports[0].read_text(encoding="utf-8"), media_type="text/markdown"
        )

    return app


app = create_app()
