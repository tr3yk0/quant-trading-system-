from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH
import sqlite3, json
from datetime import datetime, timezone

class PositionStore:
    def __init__(self, path=str(DEFAULT_DB_PATH)):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS positions (
            ticket TEXT PRIMARY KEY, symbol TEXT, side TEXT, volume REAL, price_open REAL,
            price_current REAL, sl REAL, tp REAL, profit REAL, swap REAL, magic INTEGER,
            time_open TEXT, time_update TEXT, source TEXT)''')
        self.db.commit()

    def replace_all(self, positions):
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute('DELETE FROM positions')
            for p in positions:
                self.db.execute('''INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                    str(p.get('ticket')), p.get('symbol',''), p.get('side',''), float(p.get('volume',0)),
                    float(p.get('price_open',0)), float(p.get('price_current',0)), float(p.get('sl',0)),
                    float(p.get('tp',0)), float(p.get('profit',0)), float(p.get('swap',0)), int(p.get('magic',0)),
                    p.get('time_open'), p.get('time_update',now), p.get('source','MT5')))

    _keys = ['ticket','symbol','side','volume','price_open','price_current','sl','tp','profit','swap','magic','time_open','time_update','source']

    def list(self, limit=None, offset=0):
        sql = 'SELECT ticket,symbol,side,volume,price_open,price_current,sl,tp,profit,swap,magic,time_open,time_update,source FROM positions ORDER BY time_update DESC'
        args = []
        if limit is not None:
            limit = max(1, min(1000, int(limit)))
            offset = max(0, int(offset))
            sql += ' LIMIT ? OFFSET ?'
            args.extend([limit, offset])
        cur = self.db.execute(sql, args)
        return [dict(zip(self._keys,row)) for row in cur.fetchall()]

    def count(self):
        row = self.db.execute('SELECT COUNT(*) FROM positions').fetchone()
        return int(row[0] if row else 0)
