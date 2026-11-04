"""只读快照库访问（SQLite），供 MCP / HTTP 查询使用。

三条纪律：
1. `mode=ro` 打开，绝不与采集进程争写锁；
2. 不依赖 api.py —— 查询层单独可用；
3. 「查不到」翻译成带 hint 的 CiError，而不是返回空列表让模型猜。
"""

from __future__ import annotations

from urllib.parse import quote

import aiosqlite

from .config import Settings
from .errors import CiError

MAX_LIMIT = 500


async def _connect(settings: Settings) -> aiosqlite.Connection:
    if not settings.db_path.exists():
        raise CiError(
            "DB_NOT_FOUND",
            f"快照库不存在: {settings.db_path}",
            hint="先运行 `ci init-db && ci run`，或把 DB_PATH 指向已有库",
        )
    uri = f"file:{quote(str(settings.db_path.resolve()))}?mode=ro"
    db = await aiosqlite.connect(uri, uri=True)
    db.row_factory = aiosqlite.Row
    return db


async def _fetch_all(db: aiosqlite.Connection, sql: str, params: list) -> list[dict]:
    async with db.execute(sql, params) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def list_snapshots(
    settings: Settings,
    *,
    competitor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict]:
    limit = max(1, min(limit, MAX_LIMIT))
    sql = (
        "SELECT competitor, page_url, page_type, snapshot_date, status, "
        "content_hash, created_at FROM snapshots WHERE 1=1"
    )
    params: list = []
    if competitor:
        sql += " AND competitor = ?"
        params.append(competitor)
    if since:
        sql += " AND snapshot_date >= ?"
        params.append(since)
    if until:
        sql += " AND snapshot_date <= ?"
        params.append(until)
    sql += " ORDER BY snapshot_date DESC, competitor, page_url LIMIT ?"
    params.append(limit)

    db = await _connect(settings)
    try:
        return await _fetch_all(db, sql, params)
    finally:
        await db.close()


async def latest_extracted(
    settings: Settings,
    *,
    competitor: str | None = None,
    page_url: str | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """每个 (competitor, page_url) 取最新一条 extracted 快照。"""
    sql = (
        "SELECT competitor, page_url, page_type, snapshot_date, payload_json "
        "FROM snapshots WHERE status = 'extracted'"
    )
    params: list = []
    if competitor:
        sql += " AND competitor = ?"
        params.append(competitor)
    if page_url:
        sql += " AND page_url = ?"
        params.append(page_url)
    if as_of:
        sql += " AND snapshot_date <= ?"
        params.append(as_of)
    sql += " ORDER BY competitor, page_url, snapshot_date DESC LIMIT ?"
    params.append(MAX_LIMIT * 4)

    db = await _connect(settings)
    try:
        rows = await _fetch_all(db, sql, params)
    finally:
        await db.close()

    seen: set[tuple[str, str]] = set()
    latest: list[dict] = []
    for row in rows:
        key = (row["competitor"], row["page_url"])
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
    return latest


async def snapshot_before(
    settings: Settings, *, competitor: str, page_url: str, before: str
) -> dict | None:
    db = await _connect(settings)
    try:
        rows = await _fetch_all(
            db,
            "SELECT competitor, page_url, page_type, snapshot_date, payload_json "
            "FROM snapshots WHERE status = 'extracted' AND competitor = ? "
            "AND page_url = ? AND snapshot_date < ? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            [competitor, page_url, before],
        )
    finally:
        await db.close()
    return rows[0] if rows else None


async def latest_on_or_before(
    settings: Settings, *, competitor: str, page_url: str, day: str
) -> dict | None:
    db = await _connect(settings)
    try:
        rows = await _fetch_all(
            db,
            "SELECT competitor, page_url, page_type, snapshot_date, payload_json "
            "FROM snapshots WHERE status = 'extracted' AND competitor = ? "
            "AND page_url = ? AND snapshot_date <= ? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            [competitor, page_url, day],
        )
    finally:
        await db.close()
    return rows[0] if rows else None


async def available_dates(settings: Settings, *, competitor: str, page_url: str) -> list[str]:
    db = await _connect(settings)
    try:
        rows = await _fetch_all(
            db,
            "SELECT DISTINCT snapshot_date FROM snapshots WHERE status = 'extracted' "
            "AND competitor = ? AND page_url = ? ORDER BY snapshot_date DESC",
            [competitor, page_url],
        )
    finally:
        await db.close()
    return [row["snapshot_date"] for row in rows]


async def review_queue(settings: Settings) -> list[dict]:
    db = await _connect(settings)
    try:
        return await _fetch_all(
            db,
            "SELECT id, competitor, page_url, snapshot_date, error_detail "
            "FROM snapshots WHERE status = 'extraction_error' "
            "ORDER BY snapshot_date DESC",
            [],
        )
    finally:
        await db.close()


async def latest_run(settings: Settings) -> dict | None:
    db = await _connect(settings)
    try:
        rows = await _fetch_all(
            db, "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1", []
        )
    except aiosqlite.OperationalError:
        return None
    finally:
        await db.close()
    return rows[0] if rows else None


async def get_run(settings: Settings, run_id: str) -> dict | None:
    db = await _connect(settings)
    try:
        rows = await _fetch_all(db, "SELECT * FROM runs WHERE id = ?", [run_id])
    except aiosqlite.OperationalError:
        return None
    finally:
        await db.close()
    return rows[0] if rows else None
