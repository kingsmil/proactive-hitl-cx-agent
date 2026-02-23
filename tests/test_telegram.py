"""Integration tests for the Telegram webhook endpoint."""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import db


def _reset_connection():
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


class _DBFixture:
    """Manages a per-test temporary SQLite database."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "test.db"
        _reset_connection()
        db.init_db()

    def teardown_method(self):
        _reset_connection()
        db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _tg_update(chat_id: int, text: str) -> dict:
    """Build a minimal Telegram Update JSON payload."""
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


class TestTelegramWebhook(_DBFixture):

    def _make_client(self):
        """Build a TestClient with StaticFiles patched out (no static/ dir needed)."""
        with patch("fastapi.staticfiles.StaticFiles.__init__", return_value=None):
            from fastapi.testclient import TestClient
            from main import app
            return TestClient(app)

    def test_returns_200_and_received_status(self):
        client = self._make_client()
        response = client.post(
            "/webhook/telegram",
            json=_tg_update(123456789, "Hello from Telegram unit test"),
        )
        assert response.status_code == 200
        assert response.json() == {"status": "received"}

    def test_session_created_with_telegram_channel(self):
        client = self._make_client()
        client.post(
            "/webhook/telegram",
            json=_tg_update(123456789, "Test message"),
        )
        session = db.get_session("telegram:123456789")
        assert session is not None, "Session should have been created"
        assert session["channel"] == "telegram"

    def test_inbound_message_stored_in_history(self):
        client = self._make_client()
        client.post(
            "/webhook/telegram",
            json=_tg_update(123456789, "Check order ORD-001"),
        )
        history = db.get_history("telegram:123456789")
        assert any(
            m["role"] == "user" and m["content"] == "Check order ORD-001"
            for m in history
        ), "Inbound message should appear in session history"

    def test_no_duplicate_telegram_prefix_in_session_id(self):
        """Session ID must always be 'telegram:{chat_id}' — exactly one prefix."""
        client = self._make_client()
        client.post(
            "/webhook/telegram",
            json=_tg_update(987654321, "Hi"),
        )
        # The correct session key
        session = db.get_session("telegram:987654321")
        assert session is not None, "Session ID must be 'telegram:987654321'"
        # Confirm no double-prefix variant was stored
        bad = db.get_session("telegram:telegram:987654321")
        assert bad is None

    def test_session_status_is_valid_after_processing(self):
        client = self._make_client()
        client.post(
            "/webhook/telegram",
            json=_tg_update(123456789, "Any message"),
        )
        session = db.get_session("telegram:123456789")
        # TestClient runs background tasks synchronously; all terminal states are valid.
        assert session["status"] in ("RUNNING", "DONE", "PAUSED")

    def test_update_without_message_is_ignored(self):
        """Non-message updates (e.g. edited_message) must return 200 + ignored status."""
        client = self._make_client()
        response = client.post(
            "/webhook/telegram",
            json={"update_id": 99},  # no 'message' key
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    def test_message_without_text_is_ignored(self):
        """Photo / sticker messages without text must return 200 + ignored status."""
        client = self._make_client()
        response = client.post(
            "/webhook/telegram",
            json={
                "update_id": 100,
                "message": {
                    "message_id": 2,
                    "chat": {"id": 123, "type": "private"},
                    # no 'text' key — e.g. a photo message
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
