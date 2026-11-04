from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from competitor_intel.core.config import Settings

DDL = """
CREATE TABLE snapshots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor     TEXT NOT NULL,
  page_url       TEXT NOT NULL,
  page_type      TEXT NOT NULL,
  snapshot_date  TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  status         TEXT NOT NULL,
  payload_json   TEXT,
  error_detail   TEXT,
  created_at     TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX ux_snap_key ON snapshots (competitor, page_url, snapshot_date);
CREATE TABLE runs (
  id               TEXT PRIMARY KEY,
  idempotency_key  TEXT NOT NULL,
  competitor       TEXT,
  status           TEXT NOT NULL,
  counts_json      TEXT,
  report_path      TEXT,
  error_summary    TEXT,
  created_at       TEXT DEFAULT (datetime('now')),
  started_at       TEXT,
  finished_at      TEXT
);
"""

PAGE = "https://example.test/pricing"
BETA_PAGE = "https://beta.test/pricing"


def _payload(day: str, amount: float, features: list[dict] | None = None) -> dict:
    return {
        "competitor_name": "acme",
        "page_url": PAGE,
        "snapshot_date": day,
        "page_type": "pricing",
        "pricing_tiers": [
            {
                "name": "Pro",
                "amount": amount,
                "currency": "USD",
                "billing_period": "monthly",
                "key_limits": ["5 席位"],
                "pricing_model": "subscription",
                "components": [],
            }
        ],
        "feature_items": features or [],
        "target_user_note": None,
    }


def _beta_payload(day: str, amount: float) -> dict:
    payload = _payload(day, amount)
    payload.update(competitor_name="beta", page_url=BETA_PAGE)
    return payload


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "competitor-intel"
    (home / "config").mkdir(parents=True)
    (home / "data" / "reports").mkdir(parents=True)
    (home / "config" / "targets.yaml").write_text(
        "defaults:\n  request_interval_s: 2\n"
        "targets:\n  - competitor: acme\n    pages:\n"
        f"      - type: pricing\n        url: {PAGE}\n        main_selector: main\n",
        encoding="utf-8",
    )

    db_path = home / "data" / "snapshots.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    rows = [
        ("acme", PAGE, "pricing", "2026-10-20", "h1", "extracted",
         json.dumps(_payload("2026-10-20", 25.0))),
        ("acme", PAGE, "pricing", "2026-10-27", "h2", "extracted",
         json.dumps(_payload("2026-10-27", 29.0))),
        ("beta", BETA_PAGE, "pricing", "2026-10-20", "b1", "extracted",
         json.dumps(_beta_payload("2026-10-20", 10.0))),
        ("beta", BETA_PAGE, "pricing", "2026-10-27", "b2", "extracted",
         json.dumps(_beta_payload("2026-10-27", 12.0))),
    ]
    conn.executemany(
        "INSERT INTO snapshots (competitor, page_url, page_type, snapshot_date, "
        "content_hash, status, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO snapshots (competitor, page_url, page_type, snapshot_date, "
        "content_hash, status, error_detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("acme", PAGE, "pricing", "2026-10-28", "h3", "extraction_error", "缺少币种"),
    )
    conn.commit()
    conn.close()

    return Settings(
        home=home,
        db_path=db_path,
        targets_path=home / "config" / "targets.yaml",
        report_dir=home / "data" / "reports",
    )
