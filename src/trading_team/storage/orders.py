from __future__ import annotations
from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH
import sqlite3
from datetime import datetime, timezone, timedelta

class OrderStore:
    def __init__(self, path=str(DEFAULT_DB_PATH)):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS orders (
            client_id TEXT PRIMARY KEY,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            volume REAL NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            max_loss REAL NOT NULL DEFAULT 0,
            ticket TEXT,
            deal TEXT,
            position_ticket TEXT,
            retcode INTEGER,
            message TEXT,
            raw TEXT
        )''')
        self.db.commit()

    def create(self, client_id, symbol, side, volume, entry, stop, target, max_loss=0.0,
               status='PRECHECK'):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute('''INSERT OR REPLACE INTO orders
            (client_id,created,updated,status,symbol,side,volume,entry,stop,target,max_loss)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (client_id, now, now, status, symbol, side, float(volume), float(entry), float(stop),
             float(target), float(max_loss)))
        self.db.commit()

    def update(self, client_id, **fields):
        if not fields:
            return
        fields['updated'] = datetime.now(timezone.utc).isoformat()
        allowed = {'status','ticket','deal','position_ticket','retcode','message','raw','volume','entry','stop','target'}
        fields = {k:v for k,v in fields.items() if k in allowed or k == 'updated'}
        assignments = ', '.join(f'{k}=?' for k in fields)
        values = list(fields.values()) + [client_id]
        self.db.execute(f'UPDATE orders SET {assignments} WHERE client_id=?', values)
        self.db.commit()

    def recent_same_signal(self, symbol, side, seconds=300):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0, seconds))).isoformat()
        row = self.db.execute('''SELECT client_id,status,created FROM orders
            WHERE symbol=? AND side=? AND created>=?
            ORDER BY created DESC LIMIT 1''', (symbol, side, cutoff)).fetchone()
        return row

    def list(self, limit=100, offset=0):
        limit = max(1, min(1000, int(limit)))
        offset = max(0, int(offset))
        cur = self.db.execute('''SELECT client_id,created,updated,status,symbol,side,volume,entry,stop,target,
            max_loss,ticket,deal,position_ticket,retcode,message,raw FROM orders
            ORDER BY created DESC LIMIT ? OFFSET ?''', (limit, offset))
        keys = ['client_id','created','updated','status','symbol','side','volume','entry','stop','target',
                'max_loss','ticket','deal','position_ticket','retcode','message','raw']
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def count(self):
        row = self.db.execute('SELECT COUNT(*) FROM orders').fetchone()
        return int(row[0] if row else 0)
