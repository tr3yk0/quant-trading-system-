"""Persist bounded, account-partitioned MT5 account snapshots for performance analytics.

The runtime samples periodically while the application is running. Snapshots are
valuations, not a complete accounting ledger; cash-flow-adjusted returns remain estimates.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


class PortfolioSnapshotStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ensure_schema()

    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=3.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=3000")
        return con

    def _ensure_schema(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS portfolio_account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                balance REAL NOT NULL,
                equity REAL NOT NULL,
                margin REAL NOT NULL,
                free_margin REAL NOT NULL,
                floating_profit REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT '',
                account_server TEXT NOT NULL DEFAULT '',
                account_login TEXT NOT NULL DEFAULT ''
            )""")
            # Upgrade databases created by earlier builds without requiring a reset.
            columns = {row[1] for row in con.execute("PRAGMA table_info(portfolio_account_snapshots)").fetchall()}
            if "account_login" not in columns:
                con.execute("ALTER TABLE portfolio_account_snapshots ADD COLUMN account_login TEXT NOT NULL DEFAULT ''")
            if "account_server" not in columns:
                con.execute("ALTER TABLE portfolio_account_snapshots ADD COLUMN account_server TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_account_snapshots_ts ON portfolio_account_snapshots(timestamp)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_account_snapshots_account_ts ON portfolio_account_snapshots(account_login, timestamp)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_account_snapshots_identity_ts ON portfolio_account_snapshots(account_login, account_server, timestamp)")
            con.commit()

    def record(self, account: dict[str, Any], *, min_interval_seconds: float = 60.0, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        values = (
            stamp, float(account.get("balance") or 0), float(account.get("equity") or 0),
            float(account.get("margin") or 0), float(account.get("margin_free") or 0),
            float(account.get("profit") or 0), str(account.get("currency") or ""),
            str(account.get("server") or "")[:128],
            str(account.get("login") or "")[:64],
        )
        with _LOCK, self._connect() as con:
            last = con.execute("SELECT MAX(timestamp) AS ts FROM portfolio_account_snapshots WHERE account_login=? AND account_server=?", (values[8], values[7])).fetchone()["ts"]
            if last is not None and stamp - float(last) < max(0.0, float(min_interval_seconds)):
                return False
            con.execute("INSERT INTO portfolio_account_snapshots(timestamp,balance,equity,margin,free_margin,floating_profit,currency,account_server,account_login) VALUES(?,?,?,?,?,?,?,?,?)", values)
            # Keep roughly one year of minute-level samples. The indexed time cutoff is
            # cheap when nothing is expired; a hard row cap is checked only every 10 min.
            try:
                retention_days = max(30, min(730, int(__import__("os").getenv("PORTFOLIO_SNAPSHOT_RETENTION_DAYS", "400"))))
                max_rows = max(50000, min(1000000, int(__import__("os").getenv("PORTFOLIO_SNAPSHOT_MAX_ROWS", "600000"))))
            except (TypeError, ValueError):
                retention_days, max_rows = 400, 600000
            con.execute("DELETE FROM portfolio_account_snapshots WHERE timestamp < ?", (stamp - retention_days * 86400,))
            if int(stamp // 600) != int((stamp - max(1.0, float(min_interval_seconds))) // 600):
                count = int(con.execute("SELECT COUNT(*) FROM portfolio_account_snapshots").fetchone()[0])
                if count > max_rows:
                    con.execute("DELETE FROM portfolio_account_snapshots WHERE id IN (SELECT id FROM portfolio_account_snapshots ORDER BY timestamp ASC LIMIT ?)", (count - max_rows,))
            con.commit()
        return True

    def list(self, *, days: int = 365, limit: int = 600, account_login: Any = None,
             account_server: Any = None, now: float | None = None) -> list[dict[str, Any]]:
        """Return evenly sampled account history without hydrating the full table in Python."""
        stamp = time.time() if now is None else float(now)
        start = stamp - max(1, int(days)) * 86400
        lim = max(1, min(150000, int(limit)))
        clauses = ["timestamp >= ?"]
        params: list[Any] = [start]
        if account_login is not None:
            clauses.append("account_login = ?")
            params.append(str(account_login or "")[:64])
        if account_server is not None:
            clauses.append("account_server = ?")
            params.append(str(account_server or "")[:128])
        where = " AND ".join(clauses)
        select_cols = "timestamp,balance,equity,margin,free_margin,floating_profit,currency,account_server,account_login"
        with _LOCK, self._connect() as con:
            total = int(con.execute(f"SELECT COUNT(*) FROM portfolio_account_snapshots WHERE {where}", params).fetchone()[0])
            if total > lim and lim > 1:
                # SQL performs the thinning so a year of minute-level snapshots does
                # not create hundreds of thousands of Python dictionaries per read.
                stride = max(1, int((total - 1 + (lim - 2)) // (lim - 1)))
                rows = con.execute(
                    f"WITH ranked AS (SELECT {select_cols}, "
                    "ROW_NUMBER() OVER (ORDER BY timestamp ASC, id ASC) AS rn, "
                    "COUNT(*) OVER () AS total_rows "
                    f"FROM portfolio_account_snapshots WHERE {where}) "
                    "SELECT timestamp,balance,equity,margin,free_margin,floating_profit,currency,account_server,account_login "
                    "FROM ranked WHERE rn=1 OR rn=total_rows OR ((rn-1) % ?) = 0 ORDER BY rn",
                    [*params, stride],
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT {select_cols} FROM portfolio_account_snapshots WHERE {where} "
                    "ORDER BY timestamp ASC, id ASC LIMIT ?",
                    [*params, lim],
                ).fetchall()
        rows = [dict(r) for r in rows]

        from datetime import datetime, timezone
        out = []
        for row in rows:
            epoch = float(row.pop("timestamp"))
            row.pop("account_login", None)
            row.pop("account_server", None)
            row["timestamp_epoch"] = epoch
            row["time_utc"] = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            out.append(row)
        return out
