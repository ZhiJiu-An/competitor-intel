"""快照存储：SQLite（TDD 5.2）。

对外契约：init_db / save_snapshot / latest_snapshot。
失败记录永不消失，只进入 review_queue 复核视图。
"""

from pathlib import Path
from typing import Optional

import aiosqlite

from .config import Target
from .models import CompetitorSnapshot

DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor     TEXT NOT NULL,
  page_url       TEXT NOT NULL,
  page_type      TEXT NOT NULL,
  snapshot_date  TEXT NOT NULL,          -- YYYY-MM-DD
  content_hash   TEXT NOT NULL,          -- 指纹，用于增量判断
  status         TEXT NOT NULL,          -- extracted | extraction_error | skipped
  payload_json   TEXT,                   -- 校验通过的快照 JSON
  error_detail   TEXT,                   -- 校验失败原因，进复核队列
  created_at     TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_snap_key
  ON snapshots (competitor, page_url, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_snap_latest
  ON snapshots (competitor, page_url, snapshot_date DESC);

-- 人工复核队列：只查失败记录，不改原始数据
CREATE VIEW IF NOT EXISTS review_queue AS
  SELECT id, competitor, page_url, snapshot_date, error_detail
  FROM snapshots WHERE status = 'extraction_error';

-- 服务化运行记录（P1 / TDD 7.4）：幂等键 = 日期 + 竞品
CREATE TABLE IF NOT EXISTS runs (
  id               TEXT PRIMARY KEY,
  idempotency_key  TEXT NOT NULL,
  competitor       TEXT,
  status           TEXT NOT NULL,          -- queued | running | succeeded | partial | failed
  counts_json      TEXT,                   -- 各类计数与指标
  report_path      TEXT,
  error_summary    TEXT,
  created_at       TEXT DEFAULT (datetime('now')),
  started_at       TEXT,
  finished_at      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idem ON runs (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_runs_created ON runs (created_at DESC);
"""

_UPSERT = """
INSERT INTO snapshots
  (competitor, page_url, page_type, snapshot_date, content_hash, status,
   payload_json, error_detail)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (competitor, page_url, snapshot_date) DO UPDATE SET
  content_hash = excluded.content_hash,
  status       = excluded.status,
  payload_json = excluded.payload_json,
  error_detail = excluded.error_detail;
"""


_INITIALIZED: set[str] = set()


async def init_db(db_path: str | Path) -> None:
    """建表与索引（ci init-db）。

    WAL 模式让 API 读与 worker 写可以并发，避免 SQLite 默认的整库写锁。
    同进程内只初始化一次：API 每次请求都重跑 DDL 会与 worker 争锁，
    曾导致偶发「attempt to write a readonly database」。
    """
    path = Path(db_path)
    key = str(path.resolve())
    if key in _INITIALIZED:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(DDL)
        await db.commit()
    _INITIALIZED.add(key)


async def save_snapshot(
    db_path: str | Path,
    target: Target,
    snapshot: Optional[CompetitorSnapshot],
    *,
    content_hash: str,
    status: str,
    error_detail: str | None = None,
) -> None:
    """写入一条快照记录（extracted / extraction_error / skipped）。

    同一天同一页面重复运行按唯一键覆盖，保证结果可重放。
    """
    if status not in ("extracted", "extraction_error", "skipped"):
        raise ValueError(f"非法 status: {status}")
    if status == "extracted" and snapshot is None:
        raise ValueError("extracted 状态必须携带 snapshot")

    payload_json = snapshot.model_dump_json() if snapshot is not None else None
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            _UPSERT,
            (
                target.competitor,
                target.url,
                target.page_type,
                snapshot.snapshot_date if snapshot else _today(),
                content_hash,
                status,
                payload_json,
                error_detail,
            ),
        )
        await db.commit()


async def latest_snapshot(
    db_path: str | Path,
    competitor: str,
    page_url: str,
    *,
    before: str | None = None,
) -> Optional[CompetitorSnapshot]:
    """查询某页面最近一次校验通过的快照，作为 diff 基线。

    before（YYYY-MM-DD，不含）用于排除当天刚写入的快照。
    """
    sql = (
        "SELECT payload_json FROM snapshots "
        "WHERE competitor = ? AND page_url = ? AND status = 'extracted'"
    )
    params: list = [competitor, page_url]
    if before is not None:
        sql += " AND snapshot_date < ?"
        params.append(before)
    sql += " ORDER BY snapshot_date DESC LIMIT 1"

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return CompetitorSnapshot.model_validate_json(row[0])


async def latest_hash(
    db_path: str | Path, competitor: str, page_url: str
) -> Optional[str]:
    """最近一次校验通过快照的内容指纹，用于增量短路。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT content_hash FROM snapshots "
            "WHERE competitor = ? AND page_url = ? AND status = 'extracted' "
            "ORDER BY snapshot_date DESC LIMIT 1",
            (competitor, page_url),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def list_review_queue(db_path: str | Path) -> list[dict]:
    """人工复核队列：全部 extraction_error 记录。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM review_queue ORDER BY snapshot_date DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def snapshots_since(
    db_path: str | Path, since: str
) -> list[CompetitorSnapshot]:
    """查询 since（YYYY-MM-DD，含）以来所有校验通过的快照，用于重生成报告。"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT payload_json FROM snapshots "
            "WHERE status = 'extracted' AND snapshot_date >= ? "
            "ORDER BY competitor, page_url, snapshot_date",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    return [CompetitorSnapshot.model_validate_json(r[0]) for r in rows]


_RUN_UPDATABLE = {
    "status",
    "counts_json",
    "report_path",
    "error_summary",
    "started_at",
    "finished_at",
}


async def create_run(
    db_path: str | Path,
    *,
    run_id: str,
    idempotency_key: str,
    competitor: str | None,
) -> tuple[dict, bool]:
    """幂等创建运行记录。

    返回 (记录, 是否本次新建)。同一天同一竞品重复触发时，返回已有记录，
    由调用方据此避免重复执行（并发安全依赖唯一索引 + ON CONFLICT）。
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO runs (id, idempotency_key, competitor, status, counts_json) "
            "VALUES (?, ?, ?, 'queued', '{}') "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (run_id, idempotency_key, competitor),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:  # pragma: no cover - 唯一索引保证必有结果
        raise RuntimeError(f"运行记录创建失败: {idempotency_key}")
    record = dict(row)
    return record, record["id"] == run_id


async def get_run(db_path: str | Path, run_id: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_run(db_path: str | Path, run_id: str, **fields) -> None:
    """更新运行记录；字段白名单校验，避免误写任意列。"""
    unknown = set(fields) - _RUN_UPDATABLE
    if unknown:
        raise ValueError(f"非法更新字段: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id)
        )
        await db.commit()


async def list_snapshot_records(
    db_path: str | Path,
    *,
    competitor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """快照元数据查询（不含 payload，供 GET /snapshots 使用）。"""
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
    sql += " ORDER BY snapshot_date DESC, competitor LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
