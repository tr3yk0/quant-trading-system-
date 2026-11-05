from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH


class ExitStateStore:
    """Restart-safe, per-ticket state for monotonic exit management."""

    def __init__(self, path: str = str(DEFAULT_DB_PATH)):
        self.path = str(path)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS position_exit_state (
                ticket TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                initial_risk REAL NOT NULL DEFAULT 0,
                peak_price REAL NOT NULL DEFAULT 0,
                trough_price REAL NOT NULL DEFAULT 0,
                partial_stage INTEGER NOT NULL DEFAULT 0,
                opened_epoch REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")
            db.commit()

    def get(self, ticket: int | str) -> dict | None:
        with sqlite3.connect(self.path, timeout=10) as db:
            row = db.execute("SELECT ticket,symbol,side,initial_risk,peak_price,trough_price,partial_stage,opened_epoch,updated_at FROM position_exit_state WHERE ticket=?", (str(ticket),)).fetchone()
        if not row:
            return None
        keys = ("ticket", "symbol", "side", "initial_risk", "peak_price", "trough_price", "partial_stage", "opened_epoch", "updated_at")
        return dict(zip(keys, row))

    def save(self, ticket: int | str, *, symbol: str, side: str, initial_risk: float,
             peak_price: float, trough_price: float, partial_stage: int, opened_epoch: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("""INSERT INTO position_exit_state
                (ticket,symbol,side,initial_risk,peak_price,trough_price,partial_stage,opened_epoch,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticket) DO UPDATE SET symbol=excluded.symbol,side=excluded.side,
                initial_risk=excluded.initial_risk,peak_price=excluded.peak_price,
                trough_price=excluded.trough_price,partial_stage=excluded.partial_stage,
                opened_epoch=excluded.opened_epoch,updated_at=excluded.updated_at""",
                (str(ticket), str(symbol), str(side), max(0.0, float(initial_risk)),
                 max(0.0, float(peak_price)), max(0.0, float(trough_price)),
                 max(0, int(partial_stage)), max(0.0, float(opened_epoch)), now))
            db.commit()

    def remove(self, ticket: int | str) -> None:
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("DELETE FROM position_exit_state WHERE ticket=?", (str(ticket),))
            db.commit()
