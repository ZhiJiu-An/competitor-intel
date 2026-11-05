"""FastAPI 服务（P1 / TDD 7.4）。

契约端点：POST /runs、GET /runs/{id}、GET /snapshots、GET /reports/latest、
POST /crawl（临时抓取任意页面）、GET /reports/{name}（报告页）。
设计约束：
- 定时采集只做协议转换，执行交给 Celery（或测试注入的 dispatcher）；
- 临时抓取是同步请求：AI 等待结果，不走 Celery 队列；
- 鉴权可选：配置 API_TOKEN 后要求 X-API-Token，未配置时默认仅监听本机。
"""

import asyncio
import html
import json
import re
from collections.abc import Callable
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, HttpUrl

from ...core.config import Settings
from ...core.orchestrator import crawl_page, enqueue_run
from ...core.store import get_run, init_db, list_snapshot_records

Dispatcher = Callable[[str, str, str | None, bool], None]

#: 报告正文来自竞品页面 + LLM，一律按不可信文本处理：先转义 HTML，
#: 再把 Markdown 语法里的危险协议拍平（html.escape 管不到链接目标）。
_DANGEROUS_URL_RE = re.compile(
    r'(href|src)="\s*(?:javascript|vbscript|data):[^"]*"', re.IGNORECASE
)


def _render_report_html(text: str) -> str:
    body = markdown.markdown(
        html.escape(text, quote=False), extensions=["tables", "fenced_code", "sane_lists"]
    )
    return _DANGEROUS_URL_RE.sub(r'\1="#"', body)


# 报告页模板：卡片按钮在浏览器打开，裸 Markdown 没法读，这里渲染成简洁 HTML。
_REPORT_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body {
    margin: 0; padding: 32px 20px 64px; background: #f7f8fa; color: #1f2329;
    font: 15px/1.7 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  main {
    max-width: 1000px; margin: 0 auto; padding: 28px 32px; background: #fff;
    border: 1px solid #e5e6eb; border-radius: 12px; overflow-x: auto;
  }
  h1 { margin: 0 0 20px; font-size: 22px; }
  h2 { margin: 28px 0 12px; padding-bottom: 8px; font-size: 17px; border-bottom: 1px solid #eef0f3; }
  h3 { margin: 20px 0 8px; font-size: 15px; }
  table { width: 100%; min-width: 1000px; margin: 8px 0 4px; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; vertical-align: top; border: 1px solid #e5e6eb; overflow-wrap: break-word; }
  th { background: #f5f6f7; font-weight: 600; white-space: nowrap; }
  /* 竞品/套餐/周期这类短列不要拆词，宽度不够交给容器横向滚动 */
  td:nth-child(1), td:nth-child(3), td:nth-child(5) { white-space: nowrap; }
  td:nth-child(2) { word-break: break-all; }
  tbody tr:nth-child(even) { background: #fafbfc; }
  a { color: #2b6cff; }
  code { padding: 1px 5px; font-size: 12px; background: #f2f3f5; border-radius: 4px; }
  .raw { float: right; font-size: 13px; }
  @media (max-width: 640px) {
    body { padding: 12px; }
    main { padding: 18px 14px; }
  }
  @media (prefers-color-scheme: dark) {
    body { background: #17181a; color: #e5e6eb; }
    main { background: #1f2023; border-color: #2f3238; }
    h2 { border-bottom-color: #2f3238; }
    th, td { border-color: #2f3238; }
    th { background: #26282c; }
    tbody tr:nth-child(even) { background: #232529; }
    code { background: #2b2d32; }
    a { color: #6d9bff; }
  }
</style>
</head>
<body>
<main>
<a class="raw" href="?raw=1">原始 Markdown</a>
__BODY__
</main>
</body>
</html>
"""


class RunRequest(BaseModel):
    competitor: str | None = None
    force: bool = False
    config: str | None = None


class CrawlRequest(BaseModel):
    """临时抓取：页面不在 targets.yaml 里也能抓。"""

    url: HttpUrl
    competitor: str | None = None
    page_type: str = "pricing"
    main_selector: str | None = None
    force: bool = False


def _default_dispatch(run_id: str, config_path: str, competitor: str | None, force: bool) -> None:
    from ..schedule.tasks import execute_run_task

    execute_run_task.delay(run_id, config_path, competitor, force)


def create_app(
    settings: Settings | None = None,
    *,
    dispatcher: Dispatcher | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    dispatch = dispatcher or _default_dispatch

    app = FastAPI(title="竞品情报服务", version="0.2")

    def _auth(x_api_token: str | None = Header(default=None)) -> None:
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
        config_path = body.config or str(settings.targets_path)
        record, created = await enqueue_run(
            settings,
            competitor=body.competitor,
            force=body.force,
            config_path=config_path,
        )
        if created:
            dispatch(record["id"], config_path, body.competitor, body.force)
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

    @app.post("/crawl", dependencies=[Depends(_auth)])
    async def crawl(body: CrawlRequest) -> dict:
        """临时抓取单个页面（同步返回结果）。

        给 MCP 的「眼睛」用：页面不必写进 targets.yaml，抓完直接返回
        结构化快照 + 与上次的字段级变化 + 临时报告链接。
        同步是有意的 —— 调用方要的就是这一次的结果，不需要轮询。
        超时上限与 MCP 通道一致，避免浏览器/LLM 卡死把请求挂住。
        """
        try:
            return await asyncio.wait_for(
                crawl_page(
                    settings,
                    url=str(body.url),
                    competitor=body.competitor,
                    page_type=body.page_type,
                    main_selector=body.main_selector,
                    force=body.force,
                ),
                timeout=settings.crawl_timeout_s,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"抓取超时（>{settings.crawl_timeout_s:.0f}s），"
                    "可稍后重试，或调大 CI_CRAWL_TIMEOUT_S"
                ),
            ) from exc

    @app.get("/snapshots", dependencies=[Depends(_auth)])
    async def read_snapshots(
        competitor: str | None = None,
        since: str | None = None,
        until: str | None = None,
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

    @app.get("/reports/{name:path}", response_model=None)
    async def read_report(name: str, raw: bool = False) -> PlainTextResponse | HTMLResponse:
        """报告直链：供飞书卡片按钮在浏览器打开，默认渲染成 HTML。

        浏览器点击无法携带 X-API-Token，因此这里不鉴权；
        默认仅监听 127.0.0.1，若改绑 0.0.0.0 请自行加防护。
        `?raw=1` 返回原始 Markdown（方便复制或喂给其它工具）。
        支持 adhoc/ 子目录（临时抓取生成的报告）。
        """
        if not re.fullmatch(r"[0-9A-Za-z._/-]+\.md", name) or ".." in name:
            raise HTTPException(status_code=400, detail="invalid report name")
        root = Path(settings.report_dir).resolve()
        path = (root / name).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="report not found")

        text = path.read_text(encoding="utf-8")
        if raw:
            return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

        body = _render_report_html(text)
        return HTMLResponse(
            _REPORT_PAGE.replace("__TITLE__", f"竞品报告 {name}").replace("__BODY__", body)
        )

    return app


app = create_app()
