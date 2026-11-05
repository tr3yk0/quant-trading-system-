from trading_team.core.paths import DB_PATH as DEFAULT_DB_PATH
import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone
class Journal:
    def __init__(self,path=str(DEFAULT_DB_PATH)):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path)
        self.db.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.commit()
    def write(self,event):
        payload=json.dumps(event,default=str)
        self.db.execute("INSERT INTO events(ts,type,payload) VALUES(?,?,?)",(datetime.now(timezone.utc).isoformat(),event.get("type","event"),payload)); self.db.commit()
