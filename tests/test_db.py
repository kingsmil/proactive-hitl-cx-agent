"""Unit tests for db — CustomerClaw state layer."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

import db


def _reset_connection():
    """Close and remove any thread-local connection so the next _conn() call
    opens a fresh one against whatever db.DB_PATH is currently set to."""
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


class DBTestCase(unittest.TestCase):
    """Base class: each test gets its own temporary SQLite database."""

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
# init_db / seed_orders
# ---------------------------------------------------------------------------

class TestInitDb(DBTestCase):

    def test_tables_exist(self):
        conn = db._conn()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("sessions", tables)
        self.assertIn("pending_actions", tables)
        self.assertIn("orders", tables)

    def test_seed_inserts_eight_orders(self):
        conn = db._conn()
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(count, 8)

    def test_seed_expected_order_ids(self):
        conn = db._conn()
        ids = {
            row[0]
            for row in conn.execute("SELECT order_id FROM orders").fetchall()
        }
        self.assertEqual(ids, {
            "ORD-001", "ORD-002", "ORD-003", "ORD-004",
            "ORD-005", "ORD-006", "ORD-007", "ORD-008",
        })

    def test_seed_is_idempotent(self):
        """Calling init_db (and thus seed_orders) a second time must not duplicate rows."""
        db.init_db()
        conn = db._conn()
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(count, 8)

    def test_delayed_orders_are_old_enough(self):
        """ORD-002, ORD-004, and ORD-007 must be stamped >24 h ago so the poller picks them up."""
        stale = db.query_stale_delayed_orders(hours=24)
        stale_ids = {o["order_id"] for o in stale}
        self.assertIn("ORD-002", stale_ids)
        self.assertIn("ORD-004", stale_ids)
        self.assertIn("ORD-007", stale_ids)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

class TestGetOrCreateSession(DBTestCase):

    def test_creates_with_defaults(self):
        s = db.get_or_create_session("sess-1")
        self.assertEqual(s["session_id"], "sess-1")
        self.assertEqual(s["status"], "RUNNING")
        self.assertEqual(s["channel"], "web")
        self.assertEqual(json.loads(s["message_history"]), [])

    def test_custom_channel_stored(self):
        s = db.get_or_create_session("sess-2", channel="proactive")
        self.assertEqual(s["channel"], "proactive")

    def test_is_idempotent(self):
        """Second call with the same ID must return the existing row unchanged."""
        db.get_or_create_session("sess-3")
        db.set_session_status("sess-3", "DONE")
        s = db.get_or_create_session("sess-3")
        # Must NOT reset status back to RUNNING
        self.assertEqual(s["status"], "DONE")

    def test_returns_dict(self):
        s = db.get_or_create_session("sess-4")
        self.assertIsInstance(s, dict)


class TestGetSession(DBTestCase):

    def test_returns_none_for_missing(self):
        self.assertIsNone(db.get_session("does-not-exist"))

    def test_returns_session_for_existing(self):
        db.get_or_create_session("sess-5")
        s = db.get_session("sess-5")
        self.assertIsNotNone(s)
        self.assertEqual(s["session_id"], "sess-5")


class TestSetSessionStatus(DBTestCase):

    def test_updates_status(self):
        db.get_or_create_session("sess-6")
        db.set_session_status("sess-6", "PAUSED")
        self.assertEqual(db.get_session("sess-6")["status"], "PAUSED")

    def test_does_not_affect_other_sessions(self):
        db.get_or_create_session("sess-7a")
        db.get_or_create_session("sess-7b")
        db.set_session_status("sess-7a", "DONE")
        self.assertEqual(db.get_session("sess-7b")["status"], "RUNNING")

    def test_all_valid_transitions(self):
        db.get_or_create_session("sess-8")
        for status in ("PAUSED", "RUNNING", "DONE"):
            db.set_session_status("sess-8", status)
            self.assertEqual(db.get_session("sess-8")["status"], status)


class TestTryTransitionSession(DBTestCase):

    def test_succeeds_when_status_matches(self):
        db.get_or_create_session("cas-1")
        db.set_session_status("cas-1", "PAUSED")
        result = db.try_transition_session("cas-1", "PAUSED", "RUNNING")
        self.assertTrue(result)
        self.assertEqual(db.get_session("cas-1")["status"], "RUNNING")

    def test_fails_when_status_does_not_match(self):
        """Simulates the second of two concurrent approve/reject requests."""
        db.get_or_create_session("cas-2")
        db.set_session_status("cas-2", "PAUSED")
        # First caller wins
        db.try_transition_session("cas-2", "PAUSED", "RUNNING")
        # Second caller — session is now RUNNING, not PAUSED
        result = db.try_transition_session("cas-2", "PAUSED", "RUNNING")
        self.assertFalse(result)
        self.assertEqual(db.get_session("cas-2")["status"], "RUNNING")

    def test_approve_and_reject_race_only_one_wins(self):
        """Approve and reject compete; exactly one must succeed."""
        db.get_or_create_session("cas-3")
        db.set_session_status("cas-3", "PAUSED")
        approve_won = db.try_transition_session("cas-3", "PAUSED", "RUNNING")
        reject_won  = db.try_transition_session("cas-3", "PAUSED", "RUNNING")
        # XOR — exactly one wins
        self.assertNotEqual(approve_won, reject_won)

    def test_does_not_affect_other_sessions(self):
        db.get_or_create_session("cas-4a")
        db.get_or_create_session("cas-4b")
        db.set_session_status("cas-4a", "PAUSED")
        db.try_transition_session("cas-4a", "PAUSED", "RUNNING")
        self.assertEqual(db.get_session("cas-4b")["status"], "RUNNING")


class TestAppendMessageAndGetHistory(DBTestCase):

    def test_single_message(self):
        db.get_or_create_session("sess-9")
        db.append_message("sess-9", "user", "hello")
        h = db.get_history("sess-9")
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["role"], "user")
        self.assertEqual(h[0]["content"], "hello")
        self.assertIn("timestamp", h[0])

    def test_multiple_messages_preserve_order(self):
        db.get_or_create_session("sess-10")
        db.append_message("sess-10", "user", "first")
        db.append_message("sess-10", "assistant", "second")
        db.append_message("sess-10", "tool", "third")
        h = db.get_history("sess-10")
        self.assertEqual(len(h), 3)
        self.assertEqual(h[0]["role"], "user")
        self.assertEqual(h[1]["role"], "assistant")
        self.assertEqual(h[2]["role"], "tool")

    def test_get_history_empty_for_new_session(self):
        db.get_or_create_session("sess-11")
        self.assertEqual(db.get_history("sess-11"), [])

    def test_get_history_returns_empty_list_for_unknown_session(self):
        """Should return [] not None — safe to pass directly to the LLM SDK."""
        result = db.get_history("ghost-session")
        self.assertEqual(result, [])

    def test_message_content_round_trips(self):
        db.get_or_create_session("sess-12")
        content = 'Tricky content: "quotes" and \'apostrophes\' and newlines\n'
        db.append_message("sess-12", "user", content)
        h = db.get_history("sess-12")
        self.assertEqual(h[0]["content"], content)


# ---------------------------------------------------------------------------
# Pending-action helpers
# ---------------------------------------------------------------------------

class TestPendingActions(DBTestCase):

    def setUp(self):
        super().setUp()
        db.get_or_create_session("pa-sess")

    def test_save_returns_string_uuid(self):
        aid = db.save_pending_action("pa-sess", "issue_refund", {"order_id": "ORD-001"}, "test reason")
        self.assertIsInstance(aid, str)
        self.assertEqual(len(aid), 36)  # uuid4 canonical form

    def test_get_returns_none_when_empty(self):
        self.assertIsNone(db.get_pending_action("pa-sess"))

    def test_get_returns_correct_fields(self):
        db.save_pending_action("pa-sess", "issue_refund", {"order_id": "ORD-001", "amount": 49.99}, "Customer asked")
        pa = db.get_pending_action("pa-sess")
        self.assertEqual(pa["tool_name"], "issue_refund")
        self.assertEqual(pa["reasoning"], "Customer asked")

    def test_arguments_deserialized_to_dict(self):
        db.save_pending_action("pa-sess", "issue_refund", {"order_id": "ORD-001"}, "r")
        pa = db.get_pending_action("pa-sess")
        self.assertIsInstance(pa["arguments"], dict)
        self.assertEqual(pa["arguments"]["order_id"], "ORD-001")

    def test_get_returns_most_recent_when_multiple(self):
        """Two actions for the same session — get must return the latest one."""
        db.save_pending_action("pa-sess", "check_order_status", {"order_id": "ORD-001"}, "first")
        db.save_pending_action("pa-sess", "issue_refund", {"order_id": "ORD-002", "amount": 10.0}, "second")
        pa = db.get_pending_action("pa-sess")
        self.assertEqual(pa["tool_name"], "issue_refund")

    def test_delete_removes_action(self):
        db.save_pending_action("pa-sess", "issue_refund", {"order_id": "ORD-001"}, "r")
        db.delete_pending_action("pa-sess")
        self.assertIsNone(db.get_pending_action("pa-sess"))

    def test_delete_is_noop_when_empty(self):
        """Deleting when nothing exists must not raise."""
        db.delete_pending_action("pa-sess")  # should not throw


# ---------------------------------------------------------------------------
# HITL queue — get_all_paused_sessions
# ---------------------------------------------------------------------------

class TestGetAllPausedSessions(DBTestCase):

    def test_empty_when_no_paused_sessions(self):
        self.assertEqual(db.get_all_paused_sessions(), [])

    def test_excludes_running_sessions(self):
        db.get_or_create_session("running-sess")
        # still RUNNING, no pending action
        self.assertEqual(db.get_all_paused_sessions(), [])

    def test_excludes_done_sessions(self):
        db.get_or_create_session("done-sess")
        db.set_session_status("done-sess", "DONE")
        self.assertEqual(db.get_all_paused_sessions(), [])

    def test_includes_paused_session(self):
        db.get_or_create_session("paused-sess")
        db.save_pending_action("paused-sess", "issue_refund", {"order_id": "ORD-001", "amount": 50.0}, "reason")
        db.set_session_status("paused-sess", "PAUSED")
        paused = db.get_all_paused_sessions()
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["session_id"], "paused-sess")

    def test_correct_nested_shape(self):
        db.get_or_create_session("shape-sess", channel="web")
        db.save_pending_action("shape-sess", "issue_refund", {"order_id": "ORD-001", "amount": 99.0}, "reasoning text")
        db.set_session_status("shape-sess", "PAUSED")
        paused = db.get_all_paused_sessions()
        item = paused[0]
        self.assertIn("session_id", item)
        self.assertIn("channel", item)
        self.assertIn("pending_action", item)
        pa = item["pending_action"]
        self.assertIn("tool_name", pa)
        self.assertIn("arguments", pa)
        self.assertIn("reasoning", pa)
        self.assertIsInstance(pa["arguments"], dict)

    def test_multiple_paused_sessions(self):
        for i in range(3):
            sid = "multi-{}".format(i)
            db.get_or_create_session(sid)
            db.save_pending_action(sid, "issue_refund", {"order_id": "ORD-00{}".format(i + 1)}, "r")
            db.set_session_status(sid, "PAUSED")
        paused = db.get_all_paused_sessions()
        self.assertEqual(len(paused), 3)


# ---------------------------------------------------------------------------
# CRM / poller helpers
# ---------------------------------------------------------------------------

class TestQueryStaleDelayedOrders(DBTestCase):

    def test_returns_delayed_orders_older_than_threshold(self):
        stale = db.query_stale_delayed_orders(hours=24)
        self.assertEqual(len(stale), 3)

    def test_excludes_non_delayed_orders(self):
        stale = db.query_stale_delayed_orders(hours=24)
        statuses = {o["status"] for o in stale}
        self.assertEqual(statuses, {"delayed"})

    def test_excludes_outreached_orders(self):
        stale = db.query_stale_delayed_orders(hours=24)
        order_id = stale[0]["order_id"]
        db.mark_order_outreached(order_id)
        stale_after = db.query_stale_delayed_orders(hours=24)
        self.assertEqual(len(stale_after), 2)
        outreached_ids = {o["order_id"] for o in stale_after}
        self.assertNotIn(order_id, outreached_ids)

    def test_stricter_threshold_returns_only_oldest(self):
        """With a 60-hour threshold only ORD-004 (72 h old) should match."""
        stale = db.query_stale_delayed_orders(hours=60)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["order_id"], "ORD-004")

    def test_very_large_threshold_returns_nothing(self):
        stale = db.query_stale_delayed_orders(hours=9999)
        self.assertEqual(stale, [])

    def test_returns_list_of_dicts(self):
        stale = db.query_stale_delayed_orders(hours=24)
        self.assertIsInstance(stale, list)
        for item in stale:
            self.assertIsInstance(item, dict)


class TestMarkOrderOutreached(DBTestCase):

    def test_sets_outreached_flag(self):
        db.mark_order_outreached("ORD-002")
        conn = db._conn()
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-002",)
        ).fetchone()
        self.assertEqual(row["outreached"], 1)

    def test_marked_order_absent_from_stale_query(self):
        db.mark_order_outreached("ORD-002")
        db.mark_order_outreached("ORD-004")
        db.mark_order_outreached("ORD-007")
        stale = db.query_stale_delayed_orders(hours=24)
        self.assertEqual(stale, [])

    def test_does_not_affect_other_orders(self):
        db.mark_order_outreached("ORD-002")
        conn = db._conn()
        row = conn.execute(
            "SELECT outreached FROM orders WHERE order_id = ?", ("ORD-004",)
        ).fetchone()
        self.assertEqual(row["outreached"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
