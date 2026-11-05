from __future__ import annotations
from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH
import json, sqlite3
from datetime import datetime, timezone


def _parse_signals(raw) -> list[dict]:
    """Normalize signals_json into a list of dicts (never raise on bad data)."""
    if raw is None or raw == "":
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if data is None:
        return []
    if isinstance(data, dict):
        # open_trade stored meta dict, or single signal object
        if "agent" in data or "side" in data or "name" in data:
            return [_normalize_signal(data)]
        # meta bag — not agent list
        return []
    if not isinstance(data, (list, tuple)):
        return []
    out = []
    for s in data:
        if isinstance(s, dict):
            out.append(_normalize_signal(s))
        elif isinstance(s, str):
            continue
        else:
            try:
                out.append(_normalize_signal(dict(s)))
            except Exception:
                continue
    return out


def _normalize_signal(s: dict) -> dict:
    """Ensure agent/side fields are plain strings (enums may serialize oddly)."""
    d = dict(s)
    side = d.get("side")
    if side is not None and not isinstance(side, str):
        d["side"] = getattr(side, "value", str(side))
    if not d.get("agent") and d.get("name"):
        d["agent"] = d.get("name")
    return d


class PerformanceStore:
    def __init__(self, path=str(DEFAULT_DB_PATH)):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS decision_attribution (
            client_id TEXT PRIMARY KEY, created TEXT NOT NULL, symbol TEXT, decision TEXT,
            confidence REAL, entry REAL, stop REAL, target REAL, signals_json TEXT,
            position_ticket TEXT, closed INTEGER DEFAULT 0, realized_pnl REAL, realized_r REAL,
            outcome TEXT, closed_at TEXT, meta_features_json TEXT, account_login TEXT, account_server TEXT)''')
        self.db.commit()
        try:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(decision_attribution)").fetchall()}
            for col, coltype in (("meta_features_json", "TEXT"), ("account_login", "TEXT"), ("account_server", "TEXT")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE decision_attribution ADD COLUMN {col} {coltype}")
                    self.db.commit()
                    cols.add(col)
        except Exception:
            pass

    def record_decision(self, client_id, symbol, decision, confidence, entry, stop, target, signals,
                        meta_features=None, account_login=None, account_server=None):
        now = datetime.now(timezone.utc).isoformat()
        # Always store a list for agent signals
        if signals is None:
            payload_obj = []
        elif isinstance(signals, dict):
            # single signal or meta — wrap if it looks like a signal
            payload_obj = [signals] if ("agent" in signals or "side" in signals) else []
        elif isinstance(signals, (list, tuple)):
            payload_obj = list(signals)
        else:
            payload_obj = []
        payload = json.dumps(payload_obj, default=str)
        if account_login is None and account_server is None:
            try:
                from trading_team.research.trade_history_learning import get_active_account_scope
                scope = get_active_account_scope()
                if scope:
                    account_login, account_server = scope
            except Exception:
                pass
        account_login = str(account_login)[:80] if account_login not in (None, "") else None
        account_server = str(account_server)[:120] if account_server not in (None, "") else None
        meta_json = None
        if meta_features is not None:
            try:
                meta_json = json.dumps(meta_features, default=str)
            except Exception:
                meta_json = None
        self.db.execute(
            """INSERT OR IGNORE INTO decision_attribution
            (client_id,created,symbol,decision,confidence,entry,stop,target,signals_json,meta_features_json,account_login,account_server)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_id, now, symbol, decision, float(confidence or 0), float(entry or 0),
             float(stop or 0), float(target or 0), payload, meta_json, account_login, account_server),
        )
        self.db.commit()
        # Point-in-time feature snapshot for research (v4.11)
        if meta_features is not None:
            try:
                from trading_team.research.feature_store import FeatureStore
                db_path = None
                try:
                    row = self.db.execute("PRAGMA database_list").fetchone()
                    if row and len(row) >= 3 and row[2]:
                        db_path = row[2]
                except Exception:
                    db_path = None
                fs = FeatureStore(db_path) if db_path else FeatureStore()
                feats = meta_features if isinstance(meta_features, dict) else {}
                fs.write(
                    symbol=str(symbol),
                    features=feats,
                    as_of=now,
                    regime=str(feats.get("regime_key") or feats.get("regime") or "") or None,
                    decision=str(decision),
                    confidence=float(confidence or 0),
                    source="record_decision",
                )
            except Exception:
                pass

    def attach_position(self, client_id, ticket):
        self.db.execute(
            'UPDATE decision_attribution SET position_ticket=? WHERE client_id=?',
            (str(ticket), client_id),
        )
        self.db.commit()

    def close_by_ticket(self, ticket, pnl, r_multiple, outcome):
        now = datetime.now(timezone.utc).isoformat()
        # Snapshot decision row before close for meta-labeler learning
        decision_row = None
        try:
            row = self.db.execute(
                "SELECT client_id,symbol,decision,confidence,entry,stop,target,"
                "signals_json,meta_features_json,position_ticket "
                "FROM decision_attribution WHERE position_ticket=? "
                "ORDER BY created DESC LIMIT 1",
                (str(ticket),),
            ).fetchone()
            if row:
                decision_row = {
                    "client_id": row[0], "symbol": row[1], "decision": row[2],
                    "confidence": row[3], "entry": row[4], "stop": row[5], "target": row[6],
                    "signals": _parse_signals(row[7]),
                    "meta_features": row[8],
                    "position_ticket": row[9],
                }
        except Exception:
            decision_row = None

        self.db.execute(
            """UPDATE decision_attribution SET closed=1,realized_pnl=?,realized_r=?,outcome=?,closed_at=?
               WHERE position_ticket=?""",
            (float(pnl or 0), float(r_multiple or 0), str(outcome), now, str(ticket)),
        )
        self.db.commit()

        # Closed-trade -> meta-labeler learning hook (v4.11)
        try:
            from trading_team.research.trade_learning import on_trade_closed
            on_trade_closed(
                ticket=str(ticket),
                pnl=float(pnl or 0),
                r_multiple=float(r_multiple or 0),
                outcome=str(outcome or ""),
                decision_row=decision_row,
            )
        except Exception:
            pass
        # Attach outcome labels to the latest feature snapshot for this symbol
        try:
            if decision_row and decision_row.get("symbol"):
                from trading_team.research.feature_store import FeatureStore
                db_path = None
                try:
                    row = self.db.execute("PRAGMA database_list").fetchone()
                    if row and len(row) >= 3 and row[2]:
                        db_path = row[2]
                except Exception:
                    db_path = None
                fs = FeatureStore(db_path) if db_path else FeatureStore()
                fs.label_latest(
                    str(decision_row["symbol"]),
                    {
                        "pnl": float(pnl or 0),
                        "r_multiple": float(r_multiple or 0),
                        "outcome": str(outcome or ""),
                        "profitable": float(pnl or 0) > 0,
                        "ticket": str(ticket),
                    },
                )
        except Exception:
            pass

    def decision_for_ticket(self, ticket):
        """Return the originating decision record for a broker/paper position ticket."""
        row = self.db.execute(
            "SELECT client_id,symbol,decision,confidence,signals_json,position_ticket FROM decision_attribution "
            "WHERE position_ticket=? AND decision IN ('BUY','SELL') AND signals_json NOT IN ('[]','') "
            "ORDER BY created DESC LIMIT 1", (str(ticket),)
        ).fetchone()
        if not row:
            row = self.db.execute(
                "SELECT client_id,symbol,decision,confidence,signals_json,position_ticket FROM decision_attribution "
                "WHERE position_ticket=? ORDER BY created DESC LIMIT 1", (str(ticket),)
            ).fetchone()
        if not row:
            return None
        return {
            "client_id": row[0], "symbol": row[1], "decision": row[2],
            "confidence": row[3], "signals": _parse_signals(row[4]), "position_ticket": row[5]
        }

    def recent(self, limit=100):
        cur = self.db.execute(
            """SELECT client_id,created,symbol,decision,confidence,entry,stop,target,position_ticket,
                      closed,realized_pnl,realized_r,outcome,closed_at,signals_json,meta_features_json
               FROM decision_attribution ORDER BY created DESC LIMIT ?""",
            (int(limit),),
        )
        keys = [
            'client_id', 'created', 'symbol', 'decision', 'confidence', 'entry', 'stop', 'target',
            'position_ticket', 'closed', 'realized_pnl', 'realized_r', 'outcome', 'closed_at',
            'signals_json', 'meta_features_json',
        ]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(keys, row))
            d['signals'] = _parse_signals(d.get('signals_json'))
            d['meta_features'] = d.get('meta_features_json')
            rows.append(d)
        return rows

    def summary(self):
        row = self.db.execute(
            '''SELECT COUNT(*) total,
                      SUM(CASE WHEN closed=1 THEN 1 ELSE 0 END) closed,
                      SUM(CASE WHEN closed=1 AND realized_pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(CASE WHEN closed=1 AND realized_pnl<0 THEN 1 ELSE 0 END) losses,
                      COALESCE(SUM(CASE WHEN closed=1 THEN realized_pnl ELSE 0 END),0) pnl,
                      COALESCE(AVG(CASE WHEN closed=1 THEN realized_r END),0) avg_r
               FROM decision_attribution'''
        ).fetchone()
        total, closed, wins, losses, pnl, avg_r = row
        win_rate = (wins / closed * 100.0) if closed else 0.0
        return {
            'signals': int(total or 0),
            'closed': int(closed or 0),
            'wins': int(wins or 0),
            'losses': int(losses or 0),
            'win_rate': win_rate,
            'realized_pnl': float(pnl or 0),
            'avg_r': float(avg_r or 0),
        }

    def agent_summary(self):
        rows = {}
        cur = self.db.execute('SELECT signals_json,closed,realized_pnl FROM decision_attribution')
        for raw, closed, pnl in cur.fetchall():
            signals = _parse_signals(raw)
            for s in signals:
                if not isinstance(s, dict):
                    continue
                agent = str(s.get('agent') or s.get('name') or 'UNKNOWN')
                side = str(s.get('side') or 'FLAT')
                try:
                    score = float(s.get('score') or 0)
                except (TypeError, ValueError):
                    score = 0.0
                try:
                    conf = float(s.get('confidence') or 0)
                except (TypeError, ValueError):
                    conf = 0.0
                d = rows.setdefault(
                    agent,
                    {
                        'agent': agent,
                        'observations': 0,
                        'directional': 0,
                        'closed_trades': 0,
                        'weighted_score': 0.0,
                        'wins': 0,
                        'losses': 0,
                        'pnl': 0.0,
                    },
                )
                d['observations'] += 1
                d['weighted_score'] += score * conf
                if side in ('BUY', 'SELL'):
                    d['directional'] += 1
                if closed:
                    d['closed_trades'] += 1
                    d['pnl'] += float(pnl or 0)
                    if pnl and pnl > 0:
                        d['wins'] += 1
                    elif pnl and pnl < 0:
                        d['losses'] += 1
        out = []
        for d in rows.values():
            d['win_rate'] = (d['wins'] / d['closed_trades'] * 100.0) if d['closed_trades'] else 0.0
            d['directional_rate'] = (d['directional'] / d['observations'] * 100.0) if d['observations'] else 0.0
            out.append(d)
        return sorted(out, key=lambda x: abs(x['weighted_score']), reverse=True)

    def calibration(self):
        buckets = {}
        cur = self.db.execute(
            'SELECT confidence,closed,realized_pnl FROM decision_attribution WHERE closed=1'
        )
        for conf, closed, pnl in cur.fetchall():
            c = float(conf or 0.0)
            low = min(0.99, max(0.0, int(c * 10) / 10))
            high = min(1.0, low + 0.1)
            key = f"{low:.1f}-{high:.1f}"
            b = buckets.setdefault(key, {'bucket': key, 'n': 0, 'wins': 0, 'pnl': 0.0})
            b['n'] += 1
            b['pnl'] += float(pnl or 0)
            if pnl and pnl > 0:
                b['wins'] += 1
        out = []
        for b in buckets.values():
            b['win_rate'] = (b['wins'] / b['n'] * 100.0) if b['n'] else 0.0
            out.append(b)
        return sorted(out, key=lambda x: x['bucket'])

    def adaptive_snapshot(self) -> dict:
        return {
            'summary': self.summary(),
            'agents': self.agent_summary(),
            'calibration': self.calibration(),
        }

    def open_trade(self, ticket, symbol, side, volume, entry, stop, target, meta=None):
        """Ensure a position_ticket row exists for SL/TP close attribution.

        Prefer linking an existing decision row (with agent signals) via attach_position
        from the runtime. This fallback creates a row from meta when none exists.
        meta may include: client_id, confidence, signals, meta_features, mode.
        """
        try:
            ticket_s = str(ticket)
            meta = meta or {}
            client_id = str(meta.get("client_id") or ticket_s)
            existing = self.db.execute(
                "SELECT client_id FROM decision_attribution WHERE position_ticket=? OR client_id=? LIMIT 1",
                (ticket_s, client_id),
            ).fetchone()
            if existing:
                self.attach_position(str(existing[0]), ticket_s)
                # Optionally backfill meta_features if still empty
                if meta.get("meta_features"):
                    try:
                        self.db.execute(
                            "UPDATE decision_attribution SET meta_features_json=? "
                            "WHERE client_id=? AND (meta_features_json IS NULL OR meta_features_json='')",
                            (json.dumps(meta["meta_features"], default=str), str(existing[0])),
                        )
                        self.db.commit()
                    except Exception:
                        pass
                return
            conf = float(meta.get("confidence") or 0.0)
            signals = meta.get("signals") or []
            self.record_decision(
                client_id, symbol, side, conf, entry, stop, target, signals,
                meta_features=meta.get("meta_features"),
            )
            self.attach_position(client_id, ticket_s)
        except Exception:
            pass
