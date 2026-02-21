"""Integration tests for the WhatsApp webhook endpoint."""
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


class TestWhatsappWebhook(_DBFixture):

    def _make_client(self):
        """Build a TestClient with StaticFiles patched out (no static/ dir needed)."""
        with patch("fastapi.staticfiles.StaticFiles.__init__", return_value=None):
            from fastapi.testclient import TestClient
            from main import app
            return TestClient(app)

    def test_returns_200_and_received_status(self):
        client = self._make_client()
        response = client.post(
            "/webhook/whatsapp",
            data={"From": "+15551239999", "Body": "Hello from unit test"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "received"}

    def test_session_created_with_whatsapp_channel(self):
        client = self._make_client()
        client.post(
            "/webhook/whatsapp",
            data={"From": "+15551239999", "Body": "Test message"},
        )
        session = db.get_session("whatsapp:+15551239999")
        assert session is not None, "Session should have been created"
        assert session["channel"] == "whatsapp"

    def test_inbound_message_stored_in_history(self):
        client = self._make_client()
        client.post(
            "/webhook/whatsapp",
            data={"From": "+15551239999", "Body": "Check order ORD-001"},
        )
        history = db.get_history("whatsapp:+15551239999")
        assert any(
            m["role"] == "user" and m["content"] == "Check order ORD-001"
            for m in history
        ), "Inbound message should appear in session history"

    def test_twilio_prefix_stripped_from_from_field(self):
        """Twilio sends From as 'whatsapp:+15551239999'; session_id must not double-prefix."""
        client = self._make_client()
        client.post(
            "/webhook/whatsapp",
            data={"From": "whatsapp:+15559876543", "Body": "Hi"},
        )
        session = db.get_session("whatsapp:+15559876543")
        assert session is not None, "Session ID must not be 'whatsapp:whatsapp:+15559876543'"
        assert session["channel"] == "whatsapp"

    def test_session_status_is_valid_after_processing(self):
        client = self._make_client()
        client.post(
            "/webhook/whatsapp",
            data={"From": "+15551239999", "Body": "Any message"},
        )
        session = db.get_session("whatsapp:+15551239999")
        # TestClient runs background tasks synchronously; all terminal states are valid.
        assert session["status"] in ("RUNNING", "DONE", "PAUSED")
