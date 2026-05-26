import json
import os
import sqlite3
from datetime import date, datetime
from typing import Any

import config

DB_PATH = os.path.join(config.STORAGE_DIR, "buff_arbitrage.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _serialize(obj: Any) -> str:
    """JSON dump that handles date objects."""

    def _default(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        raise TypeError(f"Unserializable type: {type(o)}")

    return json.dumps(obj, ensure_ascii=False, default=_default)


def init_db():
    """Create tables and indexes if they don't exist."""
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            target_date TEXT NOT NULL,
            stable_days INTEGER NOT NULL,
            volatility_threshold REAL NOT NULL,
            conversion_rate REAL NOT NULL,
            max_buff_pages INTEGER NOT NULL,
            status TEXT DEFAULT 'running',
            raw_count INTEGER DEFAULT 0,
            filtered_count INTEGER DEFAULT 0,
            stable_count INTEGER DEFAULT 0,
            steam_count INTEGER DEFAULT 0,
            target_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,
            name TEXT NOT NULL,
            buff_price REAL,
            volume INTEGER,
            turnover REAL,
            step_reached INTEGER DEFAULT 1,
            steam_url TEXT,
            steam_price REAL,
            steam_sold_count INTEGER,
            buff_price_history TEXT,
            steam_price_history TEXT,
            avg_buff_price REAL,
            avg_steam_usd REAL,
            avg_steam_cny REAL,
            avg_diff REAL,
            is_target INTEGER,
            target_count INTEGER,
            date_pairs TEXT,
            fail_reason TEXT,
            volatility REAL,
            debug_info TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
    """)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# Run lifecycle
# ──────────────────────────────────────────────

def create_run(target_date: date, stable_days: int,
               volatility_threshold: float, conversion_rate: float,
               target_count: int) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO runs (started_at, target_date, stable_days, volatility_threshold, "
        "conversion_rate, max_buff_pages, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (datetime.now().isoformat(), target_date.isoformat(), stable_days,
         volatility_threshold, conversion_rate, target_count),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def finish_run(run_id: int, status: str = "completed"):
    conn = _get_conn()
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
        (datetime.now().isoformat(), status, run_id),
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# Step data persistence
# ──────────────────────────────────────────────

def save_step1(run_id: int, raw_items: list, filtered_items: list):
    """Persist raw + filtered items after step 1.  Clears previous items for this run."""
    conn = _get_conn()
    conn.execute("DELETE FROM run_items WHERE run_id = ?", (run_id,))

    filtered_ids = {it.item_id for it in filtered_items}
    rows = []
    for it in raw_items:
        rows.append((
            run_id, it.item_id, it.name, it.buff_price, it.volume, it.turnover,
            2 if it.item_id in filtered_ids else 1,
        ))
    conn.executemany(
        "INSERT INTO run_items (run_id, item_id, name, buff_price, volume, turnover, step_reached) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.execute("UPDATE runs SET raw_count=?, filtered_count=? WHERE id=?",
                 (len(raw_items), len(filtered_items), run_id))
    conn.commit()
    conn.close()


def save_step2(run_id: int, all_filtered: list, stable_items: list):
    """Update items with stability analysis results. Clears downstream step data."""
    conn = _get_conn()
    stable_ids = {it.item_id for it in stable_items}

    for it in all_filtered:
        passed = it.item_id in stable_ids
        ph = [{"date": r.date, "price": r.price} for r in (it.price_history or [])]
        debug = {
            "history_len": getattr(it, "_debug_history_len", -1),
            "min_price": getattr(it, "_debug_min_price", 0),
            "max_price": getattr(it, "_debug_max_price", 0),
            "volatility": getattr(it, "_debug_volatility", 0),
            "fail_reason": getattr(it, "_debug_fail_reason", "") if not passed else "",
        }
        conn.execute(
            "UPDATE run_items SET step_reached=?, buff_price_history=?, volatility=?, "
            "fail_reason=CASE WHEN ? IS NOT NULL THEN ? ELSE fail_reason END, debug_info=?, "
            "steam_url=NULL, steam_price=NULL, steam_sold_count=NULL, steam_price_history=NULL, "
            "avg_buff_price=NULL, avg_steam_usd=NULL, avg_steam_cny=NULL, avg_diff=NULL, "
            "is_target=NULL, target_count=NULL, date_pairs=NULL "
            "WHERE run_id=? AND item_id=?",
            (3 if passed else 2, _serialize(ph), getattr(it, "_debug_volatility", 0),
             getattr(it, "_debug_fail_reason", "") if not passed else None,
             getattr(it, "_debug_fail_reason", "") if not passed else None,
             _serialize(debug), run_id, it.item_id),
        )
    conn.execute("UPDATE runs SET stable_count=? WHERE id=?",
                 (len(stable_items), run_id))
    conn.commit()
    conn.close()


def save_step3(run_id: int, stable_items: list, steam_data: dict):
    """Update items with Steam market data. Clears downstream arbitrage fields."""
    conn = _get_conn()
    success = 0
    for it in stable_items:
        data = steam_data.get(it.item_id)
        if data:
            success += 1
            ph = [{"date": r.date, "price": r.price, "volume": r.volume}
                  for r in data.get("steam_price_history", [])]
            conn.execute(
                "UPDATE run_items SET step_reached=4, steam_url=?, steam_price=?, "
                "steam_sold_count=?, steam_price_history=?, "
                "avg_buff_price=NULL, avg_steam_usd=NULL, avg_steam_cny=NULL, avg_diff=NULL, "
                "is_target=NULL, target_count=NULL, date_pairs=NULL "
                "WHERE run_id=? AND item_id=?",
                (data.get("steam_url"), data.get("steam_price"),
                 data.get("steam_sold_count", 0), _serialize(ph),
                 run_id, it.item_id),
            )
        else:
            conn.execute(
                "UPDATE run_items SET fail_reason=CASE WHEN fail_reason IS NULL "
                "THEN 'Steam数据获取失败' ELSE fail_reason END WHERE run_id=? AND item_id=?",
                (run_id, it.item_id),
            )
    conn.execute("UPDATE runs SET steam_count=? WHERE id=?", (success, run_id))
    conn.commit()
    conn.close()


def save_step4(run_id: int, stable_items: list, arbitrage_results: dict):
    """Update items with arbitrage comparison results and mark run completed."""
    conn = _get_conn()
    target_count = 0
    for it in stable_items:
        ar = arbitrage_results.get(it.item_id)
        if ar:
            is_target = 1 if ar["is_target"] else 0
            if ar["is_target"]:
                target_count += 1
            conn.execute(
                "UPDATE run_items SET avg_buff_price=?, avg_steam_usd=?, avg_steam_cny=?, "
                "avg_diff=?, is_target=?, target_count=?, date_pairs=?, fail_reason=NULL "
                "WHERE run_id=? AND item_id=?",
                (ar["avg_buff_price"], ar["avg_steam_usd"], ar["avg_steam_cny"],
                 ar["avg_diff"], is_target, ar["target_count"],
                 _serialize(ar["date_pairs"]), run_id, it.item_id),
            )
        else:
            conn.execute(
                "UPDATE run_items SET fail_reason=CASE WHEN fail_reason IS NULL "
                "THEN '无Steam数据无法套利对比' ELSE fail_reason END WHERE run_id=? AND item_id=?",
                (run_id, it.item_id),
            )
    conn.execute("UPDATE runs SET target_count=?, status='completed', finished_at=? WHERE id=?",
                 (target_count, datetime.now().isoformat(), run_id))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# Query helpers
# ──────────────────────────────────────────────

def get_recent_runs(limit: int = 20) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_items(run_id: int, step_min: int = 1, target_only: bool = False) -> list[dict]:
    conn = _get_conn()
    cond = f"run_id=? AND step_reached >= ?"
    if target_only:
        cond += " AND is_target=1"
    rows = conn.execute(
        f"SELECT * FROM run_items WHERE {cond} ORDER BY is_target DESC, avg_diff DESC",
        (run_id, step_min),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_run(run_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
