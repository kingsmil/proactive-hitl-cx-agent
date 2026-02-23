import db
import json

sessions = db.get_all_sessions()
print("Sessions:")
for s in sessions:
    if s["channel"] == "telegram":
        print(s["session_id"], s["status"], s["channel"], len(json.loads(s.get("message_history", "[]"))))
        history = json.loads(s.get("message_history", "[]"))
        for m in history:
            print("  ", m.get("role"), m.get("content")[:50] if m.get("content") else m)
