"""매 실행 후 상태를 GitHub로 푸시 (Cowork 원격 분석용)."""
import json
import sqlite3
import subprocess

conn = sqlite3.connect("carry_state.db")
events = [dict(ts=r[0], kind=r[1], detail=json.loads(r[2]))
          for r in conn.execute("SELECT * FROM events ORDER BY ts")]
state = {k: json.loads(v) for k, v in conn.execute("SELECT * FROM state")}
json.dump({"state": state, "events": events[-200:]}, open("status.json", "w"),
          indent=1, ensure_ascii=False)
subprocess.run(["git", "pull", "--rebase", "-q", "origin", "main"], capture_output=True)
subprocess.run(["git", "add", "status.json"])
subprocess.run(["git", "commit", "-qm", "status update"], capture_output=True)
subprocess.run(["git", "push", "-q"], capture_output=True)
