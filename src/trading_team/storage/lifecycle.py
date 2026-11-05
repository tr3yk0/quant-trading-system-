from __future__ import annotations
from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH

import sqlite3
from datetime import datetime, timezone
from typing import Any


class LifecycleStore:
    """Durable state for managed-position lifecycle transitions."""

    def __init__(self, path: str = str(DEFAULT_DB_PATH)):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS managed_positions (
                ticket TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                volume REAL NOT NULL,
                price_open REAL NOT NULL,
                last_price REAL NOT NULL,
                sl REAL NOT NULL DEFAULT 0,
                tp REAL NOT NULL DEFAULT 0,
                last_profit REAL NOT NULL DEFAULT 0,
                peak_profit REAL NOT NULL DEFAULT 0,
                max_adverse REAL NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'OPEN'
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ticket TEXT,
                symbol TEXT,
                event TEXT NOT NULL,
                payload TEXT NOT NULL
            )"""
        )
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list_open(self) -> list[dict[str, Any]]:
        cur = self.db.execute(
            """SELECT ticket,symbol,side,volume,price_open,last_price,sl,tp,last_profit,
                       peak_profit,max_adverse,last_seen,state
                FROM managed_positions WHERE state='OPEN' ORDER BY last_seen DESC"""
        )
        keys = [
            "ticket", "symbol", "side", "volume", "price_open", "last_price", "sl", "tp",
            "last_profit", "peak_profit", "max_adverse", "last_seen", "state"
        ]
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def observe(self, position: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        ticket = str(position["ticket"])
        previous = self.db.execute(
            "SELECT last_profit,peak_profit,max_adverse FROM managed_positions WHERE ticket=?", (ticket,)
        ).fetchone()
        profit = float(position.get("profit", 0.0) or 0.0)
        peak = max(profit, float(previous[1]) if previous else profit)
        adverse = min(profit, float(previous[2]) if previous else profit)
        event = "POSITION_UPDATED" if previous else "POSITION_OPENED"
        self.db.execute(
            """INSERT INTO managed_positions
                (ticket,symbol,side,volume,price_open,last_price,sl,tp,last_profit,peak_profit,max_adverse,last_seen,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')
                ON CONFLICT(ticket) DO UPDATE SET
                symbol=excluded.symbol,side=excluded.side,volume=excluded.volume,price_open=excluded.price_open,
                last_price=excluded.last_price,sl=excluded.sl,tp=excluded.tp,last_profit=excluded.last_profit,
                peak_profit=excluded.peak_profit,max_adverse=excluded.max_adverse,last_seen=excluded.last_seen,state='OPEN'""",
            (
                ticket, position.get("symbol", ""), position.get("side", ""), float(position.get("volume", 0.0)),
                float(position.get("price_open", 0.0)), float(position.get("price_current", 0.0)),
                float(position.get("sl", 0.0) or 0.0), float(position.get("tp", 0.0) or 0.0),
                profit, peak, adverse, now,
            ),
        )
        self._event(ticket, position.get("symbol", ""), event, {
            "profit": profit,
            "peak_profit": peak,
            "max_adverse": adverse,
            "sl": position.get("sl", 0),
            "tp": position.get("tp", 0),
        })
        self.db.commit()
        return {"event": event, "ticket": ticket, "peak_profit": peak, "max_adverse": adverse}

    def mark_closed(self, ticket: str, payload: dict[str, Any]) -> None:
        ticket = str(ticket)
        self.db.execute(
            "UPDATE managed_positions SET state='CLOSED', last_seen=? WHERE ticket=?",
            (self._now(), ticket),
        )
        self._event(ticket, payload.get("symbol", ""), "POSITION_CLOSED", payload)
        self.db.commit()

    def _event(self, ticket: str | None, symbol: str | None, event: str, payload: dict[str, Any]) -> None:
        import json
        self.db.execute(
            "INSERT INTO lifecycle_events(ts,ticket,symbol,event,payload) VALUES(?,?,?,?,?)",
            (self._now(), str(ticket) if ticket is not None else None, symbol, event, json.dumps(payload, default=str)),
        )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self.db.execute(
            "SELECT ts,ticket,symbol,event,payload FROM lifecycle_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        rows = []
        import json
        for ts, ticket, symbol, event, payload in cur.fetchall():
            try:
                payload_obj = json.loads(payload)
            except Exception:
                payload_obj = {"raw": payload}
            rows.append({"time": ts, "ticket": ticket, "symbol": symbol, "event": event, "payload": payload_obj})
        return rows

# Backward-compatible helper added in v1.1.
def _get(self, ticket: str):
    cur = self.db.execute("SELECT ticket,symbol,side,volume,price_open,last_price,sl,tp,last_profit,peak_profit,max_adverse,last_seen,state FROM managed_positions WHERE ticket=?", (str(ticket),))
    row = cur.fetchone()
    if not row: return None
    keys=["ticket","symbol","side","volume","price_open","last_price","sl","tp","last_profit","peak_profit","max_adverse","last_seen","state"]
    return dict(zip(keys,row))

LifecycleStore.get = _get
