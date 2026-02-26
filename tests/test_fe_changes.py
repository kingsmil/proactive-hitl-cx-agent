"""Tests for the frontend refactoring changes in 260226-fe-changes.

Covers:
- Professionalized UI text (no magic/pickleball-themed language in templates)
- Priority-based session redirect on GET /
- Demo outreach endpoint
- mark_order_not_outreached DB helper
- emit_user_message SSE helper
- Template variable contracts
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import db


def _reset_connection():
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


class DBTestCase(unittest.TestCase):
    """Each test gets its own temporary SQLite database."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(self.tmpdir) / "test.db"
        _reset_connection()
        db.init_db()

    def tearDown(self):
        _reset_connection()
        db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# mark_order_not_outreached
# ---------------------------------------------------------------------------

class TestMarkOrderNotOutreached(DBTestCase):

    def test_resets_outreached_flag(self):
        db.mark_order_outreached("ORD-002")
        conn = db._conn()
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-002",)
        ).fetchone()
        self.assertEqual(row["outreached"], 1)

        db.mark_order_not_outreached("ORD-002")
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-002",)
        ).fetchone()
        self.assertEqual(row["outreached"], 0)

    def test_order_reappears_in_stale_query_after_reset(self):
        db.mark_order_outreached("ORD-002")
        stale = db.query_stale_delayed_orders(hours=24)
        outreached_ids = {o["order_id"] for o in stale}
        self.assertNotIn("ORD-002", outreached_ids)

        db.mark_order_not_outreached("ORD-002")
        stale = db.query_stale_delayed_orders(hours=24)
        outreached_ids = {o["order_id"] for o in stale}
        self.assertIn("ORD-002", outreached_ids)

    def test_does_not_affect_other_orders(self):
        db.mark_order_outreached("ORD-002")
        db.mark_order_outreached("ORD-004")
        db.mark_order_not_outreached("ORD-002")
        conn = db._conn()
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-004",)
        ).fetchone()
        self.assertEqual(row["outreached"], 1)

    def test_noop_on_already_not_outreached(self):
        """Calling mark_order_not_outreached on a fresh order should not raise."""
        db.mark_order_not_outreached("ORD-002")
        conn = db._conn()
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-002",)
        ).fetchone()
        self.assertEqual(row["outreached"], 0)


# ---------------------------------------------------------------------------
# Seed data: pickleball products
# ---------------------------------------------------------------------------

class TestSeedDataProducts(DBTestCase):

    def test_seed_contains_pickleball_products(self):
        """Verify seed orders use pickleball product names."""
        orders = db.get_all_orders()
        product_names = [o["product_name"] for o in orders]
        # At least one pickleball-related term should appear
        pickleball_terms = ["paddle", "pickleball", "court"]
        found = any(
            any(term in name.lower() for term in pickleball_terms)
            for name in product_names
        )
        self.assertTrue(found, f"Expected pickleball products, got: {product_names}")

    def test_seed_order_count(self):
        self.assertEqual(len(db.get_all_orders()), 8)


# ---------------------------------------------------------------------------
# Priority-based redirect on GET /
# ---------------------------------------------------------------------------

class TestRootRedirectPriority(DBTestCase):

    def _make_client(self):
        with patch("fastapi.staticfiles.StaticFiles.__init__", return_value=None):
            from fastapi.testclient import TestClient
            from main import app
            return TestClient(app, follow_redirects=False)

    def test_redirect_prefers_running_over_done(self):
        db.get_or_create_session("sess-done")
        db.set_session_status("sess-done", "DONE")
        db.get_or_create_session("sess-running")
        db.set_session_status("sess-running", "RUNNING")

        client = self._make_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 307)
        self.assertIn("sess-running", response.headers["location"])

    def test_redirect_prefers_paused_over_done(self):
        db.get_or_create_session("sess-done2")
        db.set_session_status("sess-done2", "DONE")
        db.get_or_create_session("sess-paused")
        db.set_session_status("sess-paused", "PAUSED")

        client = self._make_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 307)
        self.assertIn("sess-paused", response.headers["location"])

    def test_redirect_prefers_running_over_paused(self):
        db.get_or_create_session("sess-paused2")
        db.set_session_status("sess-paused2", "PAUSED")
        db.get_or_create_session("sess-run2")
        db.set_session_status("sess-run2", "RUNNING")

        client = self._make_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 307)
        self.assertIn("sess-run2", response.headers["location"])

    def test_redirect_to_new_uuid_when_no_sessions(self):
        # Clear all sessions
        conn = db._conn()
        conn.execute("DELETE FROM sessions")
        conn.commit()

        client = self._make_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 307)
        self.assertIn("/chat/", response.headers["location"])


# ---------------------------------------------------------------------------
# Template variable contracts — professionalized text
# ---------------------------------------------------------------------------

class TestTemplateText(unittest.TestCase):
    """Ensure magic/fantasy themed text has been replaced with professional text."""

    def _read_template(self, relative_path: str) -> str:
        template_dir = Path(__file__).parent.parent / "frontend" / "templates"
        return (template_dir / relative_path).read_text()

    def test_dashboard_no_sanctum_text(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("Sanctum", content)
        self.assertIn("Control Panel", content)

    def test_dashboard_no_in_play_text(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("In\n                    Play", content)
        self.assertIn("Online", content)

    def test_dashboard_session_label(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("Match\n                    No.", content)
        self.assertIn("Session", content)

    def test_dashboard_customer_chat_pane(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("Court Comms", content)
        self.assertIn("Customer Chat", content)

    def test_dashboard_agent_trace_pane(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("Replay Analysis", content)
        self.assertIn("Agent Trace", content)

    def test_dashboard_approvals_tab(self):
        content = self._read_template("dashboard.html")
        self.assertNotIn("Scores", content)
        self.assertIn("Approvals", content)

    def test_dashboard_pending_badge(self):
        content = self._read_template("dashboard.html")
        self.assertIn("pending", content)
        # Badge should not say "awaiting" anymore
        self.assertNotIn("awaiting{% endif %}", content)

    def test_action_card_professional_labels(self):
        content = self._read_template("partials/action_card.html")
        self.assertNotIn("High Risk", content)
        self.assertIn("Requires Approval", content)
        self.assertNotIn("Spell", content)
        self.assertIn("Action", content)
        self.assertNotIn("Grant", content)
        self.assertIn("Approve", content)
        self.assertNotIn("Deny", content)
        self.assertIn("Reject", content)

    def test_action_decision_professional_text(self):
        content = self._read_template("partials/action_decision.html")
        self.assertNotIn("Seal granted", content)
        self.assertIn("Approved", content)
        self.assertNotIn("Seal denied", content)
        self.assertIn("Denied", content)

    def test_action_queue_no_magic_text(self):
        content = self._read_template("partials/action_queue.html")
        self.assertNotIn("realm is at peace", content)
        self.assertNotIn("seals await breaking", content)
        self.assertIn("No actions pending approval", content)

    def test_chat_exchange_no_awaiting_seal(self):
        content = self._read_template("partials/chat_exchange.html")
        self.assertNotIn("awaiting the seal", content)
        self.assertIn("processing", content)

    def test_settings_modal_professional(self):
        content = self._read_template("partials/settings_modal.html")
        self.assertNotIn("Arcane Configuration", content)
        self.assertIn("Settings", content)
        self.assertNotIn("Seal Configuration", content)
        self.assertIn("Save Settings", content)

    def test_settings_saved_text(self):
        content = self._read_template("partials/settings_saved.html")
        self.assertNotIn("Sealed", content)
        self.assertIn("Saved", content)

    def test_inbox_no_magic_icons(self):
        content = self._read_template("partials/inbox.html")
        self.assertNotIn("Inject ✦", content)
        self.assertIn("Send", content)

    def test_chat_pane_no_magic_language(self):
        content = self._read_template("partials/chat_pane.html")
        self.assertNotIn("awaiting the seal", content)
        self.assertNotIn("weaving a", content)
        self.assertIn("processing", content)

    def test_action_decision_has_processing_bubble(self):
        """Verify the processing bubble OOB swap from master is included."""
        content = self._read_template("partials/action_decision.html")
        self.assertIn("hx-swap-oob=\"beforeend\"", content)
        self.assertIn("reply-body-{{ session_id }}", content)

    def test_dashboard_auto_loads_chat_pane(self):
        """Verify chat pane loads via HTMX on page load."""
        content = self._read_template("dashboard.html")
        self.assertIn('hx-trigger="load"', content)
        self.assertIn("/pane", content)


# ---------------------------------------------------------------------------
# Demo outreach endpoint
# ---------------------------------------------------------------------------

class TestDemoOutreachEndpoint(DBTestCase):

    def _make_client(self):
        with patch("fastapi.staticfiles.StaticFiles.__init__", return_value=None):
            from fastapi.testclient import TestClient
            from main import app
            return TestClient(app)

    @patch("api.routes.demo.run_agent", new_callable=AsyncMock)
    def test_trigger_returns_triggered_status(self, mock_agent):
        client = self._make_client()
        response = client.post("/demo/trigger-outreach", data={"phone": ""})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "triggered")
        self.assertGreater(body["count"], 0)

    @patch("api.routes.demo.run_agent", new_callable=AsyncMock)
    def test_trigger_creates_proactive_sessions(self, mock_agent):
        client = self._make_client()
        response = client.post("/demo/trigger-outreach", data={"phone": ""})
        body = response.json()
        for s in body["sessions"]:
            session = db.get_session(s["session_id"])
            self.assertIsNotNone(session)
            self.assertEqual(session["channel"], "proactive")

    @patch("api.routes.demo.run_agent", new_callable=AsyncMock)
    def test_trigger_with_phone_filter(self, mock_agent):
        client = self._make_client()
        response = client.post("/demo/trigger-outreach", data={"phone": "+1-555-0102"})
        body = response.json()
        self.assertEqual(body["status"], "triggered")
        # Should only trigger for Bob Martinez's order (ORD-002)
        order_ids = [s["order_id"] for s in body["sessions"]]
        self.assertIn("ORD-002", order_ids)

    @patch("api.routes.demo.run_agent", new_callable=AsyncMock)
    def test_trigger_resets_outreached_flag(self, mock_agent):
        # Mark all delayed orders as outreached
        db.mark_order_outreached("ORD-002")
        db.mark_order_outreached("ORD-004")
        db.mark_order_outreached("ORD-007")

        client = self._make_client()
        response = client.post("/demo/trigger-outreach", data={"phone": ""})
        body = response.json()
        # Should still trigger because the endpoint resets the flags
        self.assertEqual(body["status"], "triggered")
        self.assertGreater(body["count"], 0)


# ---------------------------------------------------------------------------
# Inbox template: demo outreach form present
# ---------------------------------------------------------------------------

class TestInboxDemoForm(unittest.TestCase):

    def test_inbox_has_outreach_trigger(self):
        content = (
            Path(__file__).parent.parent
            / "frontend" / "templates" / "partials" / "inbox.html"
        ).read_text()
        self.assertIn("/demo/trigger-outreach", content)
        self.assertIn("Trigger Outreach", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
