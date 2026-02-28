"""Integration tests for the refund rule engine and order state machine.

Covers:
  1. ORDER_STATUS_GRAPH enforces valid/invalid transitions
  2. validate_refund uses the graph to allow/reject refunds
  3. issue_refund marks the order as 'refunded' via the graph
  4. Double-refund prevention (first succeeds, second rejected)
  5. Approve-time re-validation catches state changes after the initial request
  6. FastAPI /actions/approve auto-rejects ineligible refunds
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db
from db import ORDER_STATUS_GRAPH, InvalidOrderTransition
from agent.tools import validate_refund, issue_refund


# ---------------------------------------------------------------------------
# Shared test base — fresh temporary DB per test
# ---------------------------------------------------------------------------

def _reset_connection():
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


class DBTestCase(unittest.TestCase):
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


# ===========================================================================
# Part 1: Order State Machine Graph
# ===========================================================================

class TestOrderStatusGraph(DBTestCase):
    """Verify the ORDER_STATUS_GRAPH defines correct terminal and non-terminal states."""

    def test_cancelled_is_terminal(self):
        self.assertEqual(ORDER_STATUS_GRAPH["cancelled"], set())

    def test_refunded_is_terminal(self):
        self.assertEqual(ORDER_STATUS_GRAPH["refunded"], set())

    def test_processing_can_reach_refunded(self):
        self.assertIn("refunded", ORDER_STATUS_GRAPH["processing"])

    def test_delayed_can_reach_refunded(self):
        self.assertIn("refunded", ORDER_STATUS_GRAPH["delayed"])

    def test_shipped_can_reach_refunded(self):
        self.assertIn("refunded", ORDER_STATUS_GRAPH["shipped"])

    def test_delivered_can_reach_refunded(self):
        self.assertIn("refunded", ORDER_STATUS_GRAPH["delivered"])

    def test_cancelled_cannot_reach_refunded(self):
        self.assertNotIn("refunded", ORDER_STATUS_GRAPH["cancelled"])

    def test_refunded_cannot_reach_refunded(self):
        self.assertNotIn("refunded", ORDER_STATUS_GRAPH["refunded"])

    def test_all_statuses_present_in_graph(self):
        expected = {"processing", "delayed", "shipped", "delivered", "cancelled", "refunded"}
        self.assertEqual(set(ORDER_STATUS_GRAPH.keys()), expected)


class TestUpdateOrderStatus(DBTestCase):
    """Test that update_order_status enforces the state graph."""

    def test_valid_transition_updates_status(self):
        db.update_order_status("ORD-001", "refunded")  # processing → refunded
        order = db.get_order("ORD-001")
        self.assertEqual(order["status"], "refunded")

    def test_valid_transition_updates_timestamp(self):
        old_ts = db.get_order("ORD-001")["last_updated"]
        db.update_order_status("ORD-001", "shipped")
        new_ts = db.get_order("ORD-001")["last_updated"]
        self.assertNotEqual(old_ts, new_ts)

    def test_cancelled_to_refunded_raises(self):
        """ORD-008 is cancelled — cannot transition to refunded."""
        with self.assertRaises(InvalidOrderTransition) as ctx:
            db.update_order_status("ORD-008", "refunded")
        self.assertIn("cancelled", str(ctx.exception))
        self.assertIn("refunded", str(ctx.exception))

    def test_refunded_to_anything_raises(self):
        db.update_order_status("ORD-001", "refunded")
        for target in ("processing", "shipped", "cancelled", "refunded"):
            with self.assertRaises(InvalidOrderTransition):
                db.update_order_status("ORD-001", target)

    def test_cancelled_to_anything_raises(self):
        for target in ("processing", "shipped", "refunded", "delayed"):
            with self.assertRaises(InvalidOrderTransition):
                db.update_order_status("ORD-008", target)

    def test_processing_to_shipped(self):
        db.update_order_status("ORD-001", "shipped")
        self.assertEqual(db.get_order("ORD-001")["status"], "shipped")

    def test_delayed_to_shipped(self):
        db.update_order_status("ORD-002", "shipped")  # ORD-002 is delayed
        self.assertEqual(db.get_order("ORD-002")["status"], "shipped")

    def test_shipped_to_delivered(self):
        db.update_order_status("ORD-006", "delivered")  # ORD-006 is shipped
        self.assertEqual(db.get_order("ORD-006")["status"], "delivered")

    def test_nonexistent_order_raises(self):
        with self.assertRaises(ValueError):
            db.update_order_status("ORD-999", "refunded")

    def test_does_not_affect_other_orders(self):
        db.update_order_status("ORD-001", "refunded")
        self.assertEqual(db.get_order("ORD-002")["status"], "delayed")


# ===========================================================================
# Part 2: validate_refund (graph-driven)
# ===========================================================================

class TestValidateRefund(DBTestCase):

    def test_nonexistent_order_rejected(self):
        err = validate_refund("ORD-999", "+1-555-0101")
        self.assertIsNotNone(err)
        self.assertIn("not found", err)

    def test_wrong_phone_rejected(self):
        err = validate_refund("ORD-001", "+1-000-0000")
        self.assertIsNotNone(err)
        self.assertIn("does not match", err)

    def test_cancelled_order_rejected(self):
        err = validate_refund("ORD-008", "+1-555-0108")
        self.assertIsNotNone(err)
        self.assertIn("cancelled", err)

    def test_refunded_order_rejected(self):
        db.update_order_status("ORD-001", "refunded")
        err = validate_refund("ORD-001", "+1-555-0101")
        self.assertIsNotNone(err)
        self.assertIn("already been refunded", err)

    def test_processing_order_allowed(self):
        self.assertIsNone(validate_refund("ORD-001", "+1-555-0101"))

    def test_delayed_order_allowed(self):
        self.assertIsNone(validate_refund("ORD-002", "+1-555-0102"))

    def test_delivered_order_allowed(self):
        self.assertIsNone(validate_refund("ORD-003", "+1-555-0103"))

    def test_shipped_order_allowed(self):
        self.assertIsNone(validate_refund("ORD-006", "+1-555-0106"))


# ===========================================================================
# Part 3: issue_refund execution
# ===========================================================================

class TestIssueRefund(DBTestCase):

    def test_successful_refund_marks_order_refunded(self):
        result = issue_refund("ORD-001", 129.99, "Customer requested", "+1-555-0101")
        self.assertIn("Refund of $129.99 issued", result)
        self.assertEqual(db.get_order("ORD-001")["status"], "refunded")

    def test_successful_refund_logs_event(self):
        issue_refund("ORD-001", 129.99, "Customer requested", "+1-555-0101")
        events = db.get_order_timeline("ORD-001")
        refund_events = [e for e in events if e["event_type"] == "refund_executed"]
        self.assertEqual(len(refund_events), 1)
        self.assertIn("129.99", refund_events[0]["description"])

    def test_cancelled_order_refund_rejected(self):
        result = issue_refund("ORD-008", 179.99, "Want money back", "+1-555-0108")
        self.assertIn("Cannot issue refund", result)
        self.assertIn("cancelled", result)
        self.assertEqual(db.get_order("ORD-008")["status"], "cancelled")

    def test_refunded_order_refund_rejected(self):
        issue_refund("ORD-001", 129.99, "First refund", "+1-555-0101")
        result = issue_refund("ORD-001", 129.99, "Second refund", "+1-555-0101")
        self.assertIn("Cannot issue refund", result)
        self.assertIn("already been refunded", result)

    def test_wrong_phone_refund_rejected(self):
        result = issue_refund("ORD-001", 129.99, "Refund please", "+1-000-0000")
        self.assertIn("Cannot issue refund", result)
        self.assertEqual(db.get_order("ORD-001")["status"], "processing")

    def test_nonexistent_order_refund_rejected(self):
        result = issue_refund("ORD-999", 50.00, "Refund", "+1-555-0101")
        self.assertIn("Cannot issue refund", result)
        self.assertIn("not found", result)


# ===========================================================================
# Part 4: Double-refund prevention
# ===========================================================================

class TestDoubleRefundPrevention(DBTestCase):

    def test_first_refund_succeeds_second_rejected(self):
        result_a = issue_refund("ORD-003", 35.97, "Session A", "+1-555-0103")
        self.assertIn("Refund of $35.97 issued", result_a)
        self.assertEqual(db.get_order("ORD-003")["status"], "refunded")

        result_b = issue_refund("ORD-003", 35.97, "Session B", "+1-555-0103")
        self.assertIn("Cannot issue refund", result_b)
        self.assertIn("already been refunded", result_b)


# ===========================================================================
# Part 5: Approve-time validation (state changes between request & approval)
# ===========================================================================

class TestApproveTimeValidation(DBTestCase):

    def test_order_cancelled_between_request_and_approval(self):
        self.assertIsNone(validate_refund("ORD-001", "+1-555-0101"))

        db.get_or_create_session("test-sess-cancel")
        db.save_pending_action(
            "test-sess-cancel", "issue_refund",
            {"order_id": "ORD-001", "amount": 129.99, "reason": "test", "customer_phone": "+1-555-0101"},
            reasoning="test",
        )
        db.set_session_status("test-sess-cancel", db.PAUSED)

        # Order gets cancelled externally
        db.update_order_status("ORD-001", "cancelled")

        # Re-validation at approval time
        pending = db.get_pending_action("test-sess-cancel")
        args = pending["arguments"]
        err = validate_refund(args["order_id"], args["customer_phone"])
        self.assertIsNotNone(err)
        self.assertIn("cancelled", err)

    def test_order_refunded_between_request_and_approval(self):
        self.assertIsNone(validate_refund("ORD-005", "+1-555-0105"))

        db.get_or_create_session("test-sess-refund")
        db.save_pending_action(
            "test-sess-refund", "issue_refund",
            {"order_id": "ORD-005", "amount": 159.00, "reason": "test", "customer_phone": "+1-555-0105"},
            reasoning="test",
        )
        db.set_session_status("test-sess-refund", db.PAUSED)

        # Another session refunds the order first
        issue_refund("ORD-005", 159.00, "Earlier refund", "+1-555-0105")
        self.assertEqual(db.get_order("ORD-005")["status"], "refunded")

        pending = db.get_pending_action("test-sess-refund")
        args = pending["arguments"]
        err = validate_refund(args["order_id"], args["customer_phone"])
        self.assertIsNotNone(err)
        self.assertIn("already been refunded", err)

    def test_order_still_valid_at_approval(self):
        self.assertIsNone(validate_refund("ORD-006", "+1-555-0106"))

        db.get_or_create_session("test-sess-ok")
        db.save_pending_action(
            "test-sess-ok", "issue_refund",
            {"order_id": "ORD-006", "amount": 89.00, "reason": "test", "customer_phone": "+1-555-0106"},
            reasoning="test",
        )
        db.set_session_status("test-sess-ok", db.PAUSED)

        pending = db.get_pending_action("test-sess-ok")
        args = pending["arguments"]
        self.assertIsNone(validate_refund(args["order_id"], args["customer_phone"]))


# ===========================================================================
# Part 6: FastAPI /actions/approve route integration
# ===========================================================================

class TestApproveRouteValidation(DBTestCase):

    def setUp(self):
        super().setUp()
        from api.app import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def _setup_paused_refund_session(self, session_id, order_id, phone, amount=100.0):
        db.get_or_create_session(session_id)
        db.save_pending_action(
            session_id, "issue_refund",
            {"order_id": order_id, "amount": amount, "reason": "test", "customer_phone": phone},
            reasoning="test",
            tool_call_id="call_test123",
        )
        db.set_session_status(session_id, db.PAUSED)

    @patch("api.routes.actions.run_agent", new_callable=AsyncMock)
    def test_approve_cancelled_order_auto_rejects(self, mock_run):
        self._setup_paused_refund_session("approve-cancel", "ORD-001", "+1-555-0101")
        db.update_order_status("ORD-001", "cancelled")

        resp = self.client.post("/actions/approve/approve-cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("rejected", resp.text)

        self.assertIsNone(db.get_pending_action("approve-cancel"))
        events = db.get_order_timeline("ORD-001")
        auto_reject = [e for e in events if e["event_type"] == "refund_auto_rejected"]
        self.assertEqual(len(auto_reject), 1)

    @patch("api.routes.actions.run_agent", new_callable=AsyncMock)
    def test_approve_refunded_order_auto_rejects(self, mock_run):
        self._setup_paused_refund_session("approve-refunded", "ORD-006", "+1-555-0106")
        db.update_order_status("ORD-006", "refunded")  # shipped → refunded

        resp = self.client.post("/actions/approve/approve-refunded")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("rejected", resp.text)
        self.assertIsNone(db.get_pending_action("approve-refunded"))

    @patch("api.routes.actions.run_agent", new_callable=AsyncMock)
    def test_approve_valid_order_succeeds(self, mock_run):
        self._setup_paused_refund_session("approve-ok", "ORD-003", "+1-555-0103", amount=35.97)

        resp = self.client.post("/actions/approve/approve-ok")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("approved", resp.text)
        mock_run.assert_called_once_with("approve-ok")

    @patch("api.routes.actions.run_agent", new_callable=AsyncMock)
    def test_reject_route_still_works(self, mock_run):
        self._setup_paused_refund_session("reject-test", "ORD-003", "+1-555-0103")

        resp = self.client.post("/actions/reject/reject-test", data={"reason": "Not justified"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("rejected", resp.text)
        self.assertIsNone(db.get_pending_action("reject-test"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
