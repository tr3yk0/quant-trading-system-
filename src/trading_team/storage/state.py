from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH
import sqlite3
from datetime import datetime, timezone

class AccountState:
    def __init__(self, path=str(DEFAULT_DB_PATH)):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('''CREATE TABLE IF NOT EXISTS account_state (
            id INTEGER PRIMARY KEY CHECK(id=1), equity REAL, balance REAL,
            daily_pnl REAL, exposure REAL, open_positions INTEGER,
            updated TEXT, margin_used REAL DEFAULT 0)''')
        try:
            self.db.execute('ALTER TABLE account_state ADD COLUMN margin_used REAL DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def save(self, equity, balance=None, daily_pnl=0.0, exposure=0.0, open_positions=0, margin_used=0.0):
        balance = equity if balance is None else balance
        self.db.execute('''INSERT INTO account_state
            (id,equity,balance,daily_pnl,exposure,open_positions,updated,margin_used)
            VALUES(1,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
            equity=excluded.equity,balance=excluded.balance,daily_pnl=excluded.daily_pnl,
            exposure=excluded.exposure,open_positions=excluded.open_positions,
            updated=excluded.updated,margin_used=excluded.margin_used''',
            (equity,balance,daily_pnl,exposure,open_positions,
             datetime.now(timezone.utc).isoformat(),margin_used))
        self.db.commit()

    def load(self):
        r = self.db.execute('''SELECT equity,balance,daily_pnl,exposure,open_positions,updated,margin_used
                               FROM account_state WHERE id=1''').fetchone()
        if not r:
            return None
        return dict(zip(['equity','balance','daily_pnl','exposure','open_positions','updated','margin_used'], r))
