from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _epoch():
    return datetime.now(timezone.utc).timestamp()


class DurableScanQueue:
    """SQLite-backed scan queue with deduplication, leases and restart recovery."""

    def __init__(self, path: str = str(DEFAULT_DB_PATH), lease_seconds: int = 90):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = max(10, int(lease_seconds))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scan_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL UNIQUE, job_type TEXT NOT NULL,
            symbol TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED', priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, available_epoch REAL NOT NULL DEFAULT 0,
            lease_until_epoch REAL, worker_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT, last_error TEXT
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_scan_jobs_claim ON scan_jobs(status,priority DESC,available_epoch,created_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_scan_jobs_symbol ON scan_jobs(symbol,job_type,created_at DESC)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scan_results (
            symbol TEXT PRIMARY KEY, job_id INTEGER, scanned_at TEXT NOT NULL, scanned_epoch REAL NOT NULL,
            status TEXT NOT NULL, result_json TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS orchestration_kv (
            state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        self.db.commit()

    def enqueue(self, *, job_key: str, symbol: str, job_type: str = "symbol_scan", payload: dict | None = None,
                priority: int = 0, max_attempts: int = 3, available_epoch: float | None = None) -> dict[str, Any]:
        now = _epoch()
        with self._lock:
            self.db.execute("""INSERT OR IGNORE INTO scan_jobs
                (job_key,job_type,symbol,status,priority,attempts,max_attempts,created_at,updated_at,available_epoch,payload_json)
                VALUES(?,?,?,'QUEUED',?,0,?,?,?,?,?)""",
                (str(job_key), str(job_type), str(symbol).upper(), int(priority), max(1, int(max_attempts)), _utc(), _utc(),
                 now if available_epoch is None else float(available_epoch), json.dumps(payload or {}, default=str)))
            self.db.commit()
            row = self.db.execute("SELECT id,status,attempts,symbol,job_key FROM scan_jobs WHERE job_key=?", (str(job_key),)).fetchone()
            return dict(row) if row else {"status": "ERROR", "symbol": symbol}

    def recover_expired(self, now_epoch: float | None = None) -> dict[str, int]:
        now = _epoch() if now_epoch is None else float(now_epoch)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            cur1 = self.db.execute("""UPDATE scan_jobs SET status='QUEUED',worker_id=NULL,lease_until_epoch=NULL,
                available_epoch=?,updated_at=?,last_error=COALESCE(last_error,'Worker lease expired; recovered after restart')
                WHERE status='RUNNING' AND lease_until_epoch IS NOT NULL AND lease_until_epoch<? AND attempts<max_attempts""",
                (now, _utc(), now))
            requeued = cur1.rowcount
            expired_rows = self.db.execute("""SELECT id,symbol,last_error,job_type FROM scan_jobs
                WHERE status='RUNNING' AND lease_until_epoch IS NOT NULL AND lease_until_epoch<? AND attempts>=max_attempts""", (now,)).fetchall()
            cur2 = self.db.execute("""UPDATE scan_jobs SET status='FAILED',worker_id=NULL,lease_until_epoch=NULL,
                updated_at=?,last_error=COALESCE(last_error,'Worker lease expired after final attempt')
                WHERE status='RUNNING' AND lease_until_epoch IS NOT NULL AND lease_until_epoch<? AND attempts>=max_attempts""", (_utc(), now))
            failed = cur2.rowcount
            for expired in expired_rows:
                symbol = str(expired['symbol'] or '').upper()
                if not symbol:
                    continue
                error = str(expired['last_error'] or 'Worker lease expired after final attempt')
                payload = {"symbol": symbol, "status": "FAILED", "scan_job_status": "FAILED",
                           "scan_job_type": str(expired["job_type"] or "symbol_scan"), "scan_completed": False,
                           "scanned_epoch": now, "error": error, "quote_fresh": False,
                           "quote_status": "scan failed", "desk_decision": "WAIT", "execution_authorized": False,
                           "agent_views": [], "reasons": ["Background scan exhausted its recovery attempts; no order authority."]}
                packed = json.dumps(payload, ensure_ascii=False)
                self.db.execute("""INSERT INTO scan_results(symbol,job_id,scanned_at,scanned_epoch,status,result_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET job_id=excluded.job_id,
                    scanned_at=excluded.scanned_at,scanned_epoch=excluded.scanned_epoch,status=excluded.status,result_json=excluded.result_json""",
                    (symbol, int(expired['id']), _utc(), now, "FAILED", packed))
            self.db.commit()
            return {"requeued": max(0, requeued), "failed": max(0, failed)}

    def claim_next(self, worker_id: str, now_epoch: float | None = None) -> dict[str, Any] | None:
        now = _epoch() if now_epoch is None else float(now_epoch)
        self.recover_expired(now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            # Promote due retries back into the claimable state. RETRY_WAIT is retained
            # for observability, but must not become a permanently stranded job.
            self.db.execute("UPDATE scan_jobs SET status='QUEUED',updated_at=? WHERE status='RETRY_WAIT' AND available_epoch<=?", (_utc(), now))
            row = self.db.execute("""SELECT * FROM scan_jobs WHERE status='QUEUED' AND available_epoch<=?
                ORDER BY priority DESC,created_at ASC,id ASC LIMIT 1""", (now,)).fetchone()
            if not row:
                self.db.commit()
                return None
            lease_until = now + self.lease_seconds
            self.db.execute("""UPDATE scan_jobs SET status='RUNNING',attempts=attempts+1,worker_id=?,lease_until_epoch=?,updated_at=? WHERE id=? AND status='QUEUED'""",
                            (str(worker_id), lease_until, _utc(), int(row["id"])))
            claimed = self.db.execute("SELECT * FROM scan_jobs WHERE id=?", (int(row["id"]),)).fetchone()
            self.db.commit()
            return self._decode_job(dict(claimed)) if claimed else None

    def claim_batch(self, worker_id: str, batch_size: int = 2, now_epoch: float | None = None) -> list[dict[str, Any]]:
        """Claim a bounded batch of due jobs in one SQLite write transaction.

        Prefetched jobs receive a lease long enough to wait behind their siblings;
        the active job can renew its lease as usual. Batch size is deliberately capped.
        """
        now = _epoch() if now_epoch is None else float(now_epoch)
        count = max(1, min(20, int(batch_size)))
        self.recover_expired(now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE scan_jobs SET status='QUEUED',updated_at=? "
                "WHERE status='RETRY_WAIT' AND available_epoch<=?", (_utc(), now)
            )
            rows = self.db.execute(
                "SELECT id FROM scan_jobs WHERE status='QUEUED' AND available_epoch<=? "
                "ORDER BY priority DESC,created_at ASC,id ASC LIMIT ?", (now, count)
            ).fetchall()
            ids = [int(row[0]) for row in rows]
            if not ids:
                self.db.commit()
                return []
            placeholders = ",".join("?" for _ in ids)
            # Protect jobs waiting in this worker's local batch from expiring while an
            # earlier job is processed. The active job is also renewed by its lease thread.
            lease_until = now + self.lease_seconds * max(1, len(ids))
            self.db.execute(
                f"UPDATE scan_jobs SET status='RUNNING',attempts=attempts+1,worker_id=?,lease_until_epoch=?,updated_at=? "
                f"WHERE id IN ({placeholders}) AND status='QUEUED'",
                (str(worker_id), lease_until, _utc(), *ids),
            )
            claimed_rows = self.db.execute(
                f"SELECT * FROM scan_jobs WHERE id IN ({placeholders}) AND status='RUNNING' AND worker_id=? "
                "ORDER BY priority DESC,created_at ASC,id ASC", (*ids, str(worker_id))
            ).fetchall()
            self.db.commit()
            return [self._decode_job(dict(row)) for row in claimed_rows]

    @staticmethod
    def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
        try: row["payload"] = json.loads(row.pop("payload_json", "{}") or "{}")
        except Exception: row["payload"] = {}
        try: row["result"] = json.loads(row.pop("result_json", "null") or "null")
        except Exception: row["result"] = None
        return row

    def renew_lease(self, job_id: int, worker_id: str, *, lease_seconds: int | None = None,
                    now_epoch: float | None = None) -> bool:
        """Renew a running job only while the caller still owns its lease."""
        now = _epoch() if now_epoch is None else float(now_epoch)
        duration = max(10, int(lease_seconds or self.lease_seconds))
        with self._lock:
            cur = self.db.execute(
                "UPDATE scan_jobs SET lease_until_epoch=?,updated_at=? "
                "WHERE id=? AND status='RUNNING' AND worker_id=?",
                (now + duration, _utc(), int(job_id), str(worker_id)),
            )
            self.db.commit()
            return cur.rowcount == 1

    def complete(self, job_id: int, result: dict[str, Any], *, now_epoch: float | None = None,
                 worker_id: str | None = None) -> bool:
        """Persist a result only if the job is still running and, when supplied, owned.

        The ownership check prevents an expired worker from overwriting the result of
        a retry that was claimed by another worker after a long pause or disconnect.
        """
        now = _epoch() if now_epoch is None else float(now_epoch)
        scanned_at = _utc()
        symbol = str(result.get("symbol", "")).upper()
        packed = json.dumps(result, ensure_ascii=False, default=str)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT symbol,status,worker_id FROM scan_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row or str(row["status"]) != "RUNNING" or (worker_id is not None and str(row["worker_id"] or "") != str(worker_id)):
                self.db.commit()
                return False
            symbol = str(row["symbol"] or symbol).upper()
            cur = self.db.execute("UPDATE scan_jobs SET status='SUCCEEDED',updated_at=?,lease_until_epoch=NULL,worker_id=NULL,result_json=?,last_error=NULL WHERE id=? AND status='RUNNING'" + (" AND worker_id=?" if worker_id is not None else ""),
                                  (scanned_at, packed, int(job_id), str(worker_id)) if worker_id is not None else (scanned_at, packed, int(job_id)))
            if cur.rowcount != 1:
                self.db.commit()
                return False
            if symbol:
                self.db.execute("""INSERT INTO scan_results(symbol,job_id,scanned_at,scanned_epoch,status,result_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET job_id=excluded.job_id,scanned_at=excluded.scanned_at,
                    scanned_epoch=excluded.scanned_epoch,status=excluded.status,result_json=excluded.result_json""",
                    (symbol, int(job_id), scanned_at, now, "SUCCEEDED", packed))
            self.db.commit()
            return True

    def fail(self, job_id: int, error: str, *, retry_delay_seconds: float = 5.0,
             worker_id: str | None = None) -> bool:
        now = _epoch()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT attempts,max_attempts,status,worker_id,symbol,job_type FROM scan_jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row or str(row["status"]) != "RUNNING" or (worker_id is not None and str(row["worker_id"] or "") != str(worker_id)):
                self.db.commit()
                return False
            attempts, max_attempts = int(row["attempts"]), int(row["max_attempts"])
            status = "RETRY_WAIT" if attempts < max_attempts else "FAILED"
            available = now + max(0.5, float(retry_delay_seconds))
            cur = self.db.execute("UPDATE scan_jobs SET status=?,updated_at=?,available_epoch=?,lease_until_epoch=NULL,worker_id=NULL,last_error=? WHERE id=? AND status='RUNNING'" + (" AND worker_id=?" if worker_id is not None else ""),
                                  (status, _utc(), available, str(error)[:2000], int(job_id), str(worker_id)) if worker_id is not None else (status, _utc(), available, str(error)[:2000], int(job_id)))
            if cur.rowcount != 1:
                self.db.commit()
                return False
            if status == "FAILED":
                symbol = str(row["symbol"] or "").upper()
                result = {"symbol": symbol, "status": "FAILED", "scan_job_status": "FAILED",
                          "scan_job_type": str(row["job_type"] or "symbol_scan"), "scan_completed": False,
                          "scanned_epoch": now, "error": str(error)[:1000], "quote_fresh": False,
                          "quote_status": "scan failed", "desk_decision": "WAIT", "execution_authorized": False,
                          "agent_views": [], "reasons": ["Background symbol scan failed; no order authority."]}
                packed = json.dumps(result, ensure_ascii=False)
                self.db.execute("""INSERT INTO scan_results(symbol,job_id,scanned_at,scanned_epoch,status,result_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET job_id=excluded.job_id,scanned_at=excluded.scanned_at,
                    scanned_epoch=excluded.scanned_epoch,status=excluded.status,result_json=excluded.result_json""",
                    (symbol, int(job_id), _utc(), now, "FAILED", packed))
            self.db.commit()
            return True

    def result(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT result_json FROM scan_results WHERE symbol=?", (str(symbol).upper(),)).fetchone()
            if not row: return None
            try: return json.loads(row[0])
            except Exception: return None

    def results(self, limit: int = 5000) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, int(limit)))
        with self._lock:
            rows = self.db.execute("SELECT result_json FROM scan_results ORDER BY scanned_epoch DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for row in rows:
                try: out.append(json.loads(row[0]))
                except Exception: pass
            return out

    def result_map(self, symbols: list[str] | tuple[str, ...] | set[str]) -> dict[str, dict[str, Any]]:
        """Fetch latest persisted results for many symbols in bounded SQL batches."""
        keys = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        out: dict[str, dict[str, Any]] = {}
        if not keys:
            return out
        with self._lock:
            # 400 is below SQLite builds with the traditional 999 bind-variable limit.
            for start in range(0, len(keys), 400):
                chunk = keys[start:start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.db.execute(
                    f"SELECT symbol,result_json FROM scan_results WHERE symbol IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    try:
                        value = json.loads(row["result_json"])
                    except Exception:
                        continue
                    if isinstance(value, dict):
                        out[str(row["symbol"]).upper()] = value
        return out

    def result_state_map(self, symbols: list[str] | tuple[str, ...] | set[str]) -> dict[str, dict[str, Any]]:
        """Fetch only scan status/timestamp columns for dispatch decisions.

        The dispatcher needs cooldown metadata, not each full JSON research report;
        this avoids deserializing large feature payloads on every dashboard cycle.
        """
        keys = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        out: dict[str, dict[str, Any]] = {}
        if not keys:
            return out
        with self._lock:
            for start in range(0, len(keys), 400):
                chunk = keys[start:start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.db.execute(
                    f"SELECT symbol,scanned_epoch,status FROM scan_results WHERE symbol IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    out[str(row["symbol"]).upper()] = {
                        "scanned_epoch": float(row["scanned_epoch"] or 0),
                        "status": str(row["status"] or "").upper(),
                        "scan_job_status": str(row["status"] or "").upper(),
                    }
        return out

    def pending_symbols(self) -> set[str]:
        """Read the active work set once instead of querying once per symbol."""
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT symbol FROM scan_jobs WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT')"
            ).fetchall()
        return {str(row[0]).upper() for row in rows if row and row[0]}

    def results_page(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        """Return a stable, bounded page of saved scan results."""
        page = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
        offset = (page - 1) * page_size
        with self._lock:
            total_row = self.db.execute("SELECT COUNT(*) FROM scan_results").fetchone()
            total = int(total_row[0] if total_row else 0)
            rows = self.db.execute(
                "SELECT result_json FROM scan_results ORDER BY scanned_epoch DESC,symbol ASC LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            try:
                item = json.loads(row[0])
                if isinstance(item, dict):
                    items.append(item)
            except Exception:
                continue
        pages = max(1, (total + page_size - 1) // page_size)
        return {
            "items": items, "page": page, "page_size": page_size, "total": total, "pages": pages,
            "has_previous": page > 1, "has_next": page < pages,
            "next_page": page + 1 if page < pages else None,
            "previous_page": page - 1 if page > 1 else None,
        }

    def candidate_results_page(self, page: int = 1, page_size: int = 50, query: str | None = None) -> dict[str, Any]:
        """Read only actionable discovery candidates using bounded SQL pagination."""
        page = max(1, int(page))
        page_size = max(1, min(250, int(page_size)))
        query = str(query or "").strip().upper()
        offset = (page - 1) * page_size
        where = "WHERE COALESCE(json_extract(result_json,'$.candidate_status'), json_extract(result_json,'$.director_candidate_status'))='CANDIDATE' AND UPPER(COALESCE(json_extract(result_json,'$.candidate_side'),'')) IN ('BUY','SELL') AND COALESCE(json_extract(result_json,'$.quote_fresh'),0) != 0"
        args: list[Any] = []
        if query:
            where += " AND UPPER(symbol) LIKE ?"
            args.append(f"%{query}%")
        with self._lock:
            try:
                total_row = self.db.execute(f"SELECT COUNT(*) FROM scan_results {where}", args).fetchone()
                total = int(total_row[0] if total_row else 0)
                rows = self.db.execute(
                    f"SELECT result_json FROM scan_results {where} ORDER BY scanned_epoch DESC,symbol ASC LIMIT ? OFFSET ?",
                    [*args, page_size, offset],
                ).fetchall()
                items = [item for row in rows if isinstance((item := self._loads_result(row[0])), dict)]
            except sqlite3.OperationalError:
                # Databases compiled without JSON1 remain functional, with bounded output.
                raw = self.db.execute("SELECT symbol,result_json,scanned_epoch FROM scan_results ORDER BY scanned_epoch DESC LIMIT ?", (min(20000, page_size * page * 20),)).fetchall()
                found = []
                for row in raw:
                    item = self._loads_result(row[1])
                    if not isinstance(item, dict):
                        continue
                    side = str(item.get("candidate_side") or "").upper()
                    status = str(item.get("candidate_status") or item.get("director_candidate_status") or "").upper()
                    if status != "CANDIDATE" or side not in {"BUY", "SELL"} or item.get("quote_fresh") is False:
                        continue
                    if query and query not in str(row[0]).upper():
                        continue
                    found.append(item)
                total = len(found)
                items = found[offset:offset + page_size]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        return {"items": items, "page": page, "page_size": page_size, "total": total, "pages": pages,
                "has_previous": page > 1, "has_next": page < pages}

    def candidate_results(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Load only current-looking BUY/SELL discovery rows for dashboard warm-up."""
        limit = max(1, min(20_000, int(limit)))
        sql = """SELECT result_json FROM scan_results
            WHERE COALESCE(json_extract(result_json,'$.candidate_status'), json_extract(result_json,'$.director_candidate_status'))='CANDIDATE'
              AND UPPER(COALESCE(json_extract(result_json,'$.candidate_side'),'')) IN ('BUY','SELL')
              AND COALESCE(json_extract(result_json,'$.quote_fresh'), 0) != 0
            ORDER BY scanned_epoch DESC LIMIT ?"""
        with self._lock:
            try:
                rows = self.db.execute(sql, (limit,)).fetchall()
                return [item for row in rows if isinstance((item := self._loads_result(row[0])), dict)]
            except sqlite3.OperationalError:
                rows = self.db.execute("SELECT result_json FROM scan_results ORDER BY scanned_epoch DESC LIMIT ?", (min(20_000, limit * 10),)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = self._loads_result(row[0])
            if not isinstance(item, dict):
                continue
            side = str(item.get("candidate_side") or "").upper()
            status = str(item.get("candidate_status") or item.get("director_candidate_status") or "").upper()
            if status == "CANDIDATE" and side in {"BUY", "SELL"} and item.get("quote_fresh") is not False:
                out.append(item)
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _loads_result(raw: Any) -> Any:
        try:
            return json.loads(raw)
        except Exception:
            return None

    def coverage_counts(self) -> dict[str, int]:
        """Aggregate per-symbol outcomes in SQL rather than iterating every symbol in Python."""
        with self._lock:
            rows = self.db.execute("""SELECT sr.status,COUNT(*) AS n
                FROM scan_results sr
                JOIN instrument_registry ir ON UPPER(ir.broker_symbol)=UPPER(sr.symbol)
                WHERE ir.active=1 AND UPPER(ir.broker_symbol)!='GBXUSD'
                GROUP BY sr.status""").fetchall()
            stored = int(self.db.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0])
        counts = {str(row[0]).upper(): int(row[1]) for row in rows}
        return {"succeeded": counts.get("SUCCEEDED", 0), "failed": counts.get("FAILED", 0), "stored_results": stored}

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return compact operational history without raw market data blobs."""
        limit = max(1, min(200, int(limit)))
        with self._lock:
            rows = self.db.execute(
                "SELECT id,job_key,job_type,symbol,status,priority,attempts,max_attempts,created_at,updated_at,available_epoch,lease_until_epoch,worker_id,last_error FROM scan_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def queue_summary(self) -> dict[str, Any]:
        out = self.stats()
        out["recent_jobs"] = self.recent_jobs(30)
        return out

    def prune_terminal(self, *, max_rows: int = 10000, retain_days: int = 14) -> dict[str, int]:
        """Bound historical queue growth while preserving active work and latest results.

        Only terminal job records are pruned. The separate per-symbol scan_results
        table remains intact, so the dashboard can still show the latest result after
        old operational history has been removed.
        """
        max_rows = max(100, min(100000, int(max_rows)))
        retain_days = max(1, min(365, int(retain_days)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        terminal = ("SUCCEEDED", "FAILED")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            cur_age = self.db.execute(
                "DELETE FROM scan_jobs WHERE status IN (?,?) AND updated_at < ?",
                (terminal[0], terminal[1], cutoff),
            )
            age_deleted = max(0, cur_age.rowcount)
            # Enforce a hard cap across terminal rows even on continuously active systems.
            overflow = self.db.execute(
                "SELECT id FROM scan_jobs WHERE status IN (?,?) ORDER BY id DESC LIMIT -1 OFFSET ?",
                (terminal[0], terminal[1], max_rows),
            ).fetchall()
            ids = [int(row[0]) for row in overflow]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.db.execute(f"DELETE FROM scan_jobs WHERE id IN ({placeholders})", ids)
            self.db.commit()
            return {"deleted_by_age": age_deleted, "deleted_by_cap": len(ids), "retained_terminal_limit": max_rows}

    def state_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT state_value FROM orchestration_kv WHERE state_key=?", (str(key),)).fetchone()
        if not row: return default
        try: return json.loads(row[0])
        except Exception: return row[0]

    def state_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("""INSERT INTO orchestration_kv(state_key,state_value,updated_at) VALUES(?,?,?)
                ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at""",
                (str(key), json.dumps(value, default=str), _utc()))
            self.db.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {str(r[0]): int(r[1]) for r in self.db.execute("SELECT status,COUNT(*) FROM scan_jobs GROUP BY status").fetchall()}
            recent = self.db.execute("SELECT MAX(updated_at) FROM scan_jobs").fetchone()[0]
            return {"status_counts": counts, "queued": counts.get("QUEUED", 0) + counts.get("RETRY_WAIT", 0),
                    "running": counts.get("RUNNING", 0), "succeeded": counts.get("SUCCEEDED", 0),
                    "failed": counts.get("FAILED", 0), "last_job_update": recent,
                    "stored_results": int(self.db.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0])}

    def pending_for_symbol(self, symbol: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT 1 FROM scan_jobs WHERE symbol=? AND status IN ('QUEUED','RUNNING','RETRY_WAIT') LIMIT 1", (str(symbol).upper(),)).fetchone()
            return bool(row)

    def close(self):
        with self._lock:
            self.db.close()
