import os
import tempfile
from pathlib import Path
from unittest.mock import patch

# The worktree filesystem is read-only, so we redirect the database to a temp dir.
_tmpdir = tempfile.mkdtemp()

# Patch StaticFiles and the DB path before any app module is imported.
with patch("fastapi.staticfiles.StaticFiles.__init__", return_value=None):
    import db
    db.DB_PATH = Path(_tmpdir) / "test_claw.db"
    db.init_db()

    from fastapi.testclient import TestClient
    from main import app

client = TestClient(app)


def test_whatsapp_webhook():
    test_phone = "+15551239999"
    test_msg = "Hello from WhatsApp unit test!"

    response = client.post(
        "/webhook/whatsapp",
        data={
            "From": test_phone,
            "Body": test_msg
        }
    )

    assert response.status_code == 200
    assert response.json() == {"status": "received"}

    session_id = f"whatsapp:{test_phone}"
    session = db.get_session(session_id)

    assert session is not None, "Session should have been created by the webhook"
    assert session["channel"] == "whatsapp"
    # TestClient executes background tasks synchronously, so the agent may have
    # already completed by the time we read. Both RUNNING and DONE are valid.
    assert session["status"] in ("RUNNING", "DONE", "PAUSED")

    history = db.get_history(session_id)
    assert any(
        m["role"] == "user" and m["content"] == test_msg
        for m in history
    ), "Inbound message should appear in session history"
