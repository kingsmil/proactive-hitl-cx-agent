"""Unit tests for order event timeline — CustomerClaw."""
import unittest

import db
from tests.test_db import DBTestCase


class TestLogOrderEvent(DBTestCase):

    def test_returns_uuid_string(self):
        eid = db.log_order_event("ORD-001", "order_lookup", "Looked up order")
        self.assertIsInstance(eid, str)
        self.assertEqual(len(eid), 36)

    def test_event_persisted(self):
        db.log_order_event("ORD-001", "refund_requested", "Refund of $50 requested", actor="agent")
        events = db.get_order_timeline("ORD-001")
        # seed_orders creates initial events; find ours
        matching = [e for e in events if e["event_type"] == "refund_requested"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["description"], "Refund of $50 requested")
        self.assertEqual(matching[0]["actor"], "agent")

    def test_session_id_stored(self):
        db.log_order_event("ORD-002", "refund_approved", "Approved", actor="operator", session_id="sess-123")
        events = db.get_order_timeline("ORD-002")
        matching = [e for e in events if e["event_type"] == "refund_approved"]
        self.assertEqual(matching[0]["session_id"], "sess-123")

    def test_default_actor_is_system(self):
        db.log_order_event("ORD-003", "status_changed", "Changed to shipped")
        events = db.get_order_timeline("ORD-003")
        matching = [e for e in events if e["description"] == "Changed to shipped"]
        self.assertEqual(matching[0]["actor"], "system")

    def test_multiple_events_per_order(self):
        db.log_order_event("ORD-001", "order_lookup", "Lookup 1")
        db.log_order_event("ORD-001", "refund_requested", "Refund requested")
        db.log_order_event("ORD-001", "refund_approved", "Approved")
        events = db.get_order_timeline("ORD-001")
        types = [e["event_type"] for e in events]
        self.assertIn("order_lookup", types)
        self.assertIn("refund_requested", types)
        self.assertIn("refund_approved", types)


class TestGetOrderTimeline(DBTestCase):

    def test_returns_list(self):
        events = db.get_order_timeline("ORD-001")
        self.assertIsInstance(events, list)

    def test_events_ordered_chronologically(self):
        events = db.get_order_timeline("ORD-001")
        timestamps = [e["created_at"] for e in events]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_returns_empty_for_unknown_order(self):
        events = db.get_order_timeline("ORD-NONEXISTENT")
        self.assertEqual(events, [])

    def test_events_have_required_fields(self):
        events = db.get_order_timeline("ORD-001")
        self.assertGreater(len(events), 0)
        for event in events:
            self.assertIn("event_id", event)
            self.assertIn("order_id", event)
            self.assertIn("event_type", event)
            self.assertIn("description", event)
            self.assertIn("actor", event)
            self.assertIn("created_at", event)


class TestGetAllOrdersWithEventCount(DBTestCase):

    def test_returns_all_orders(self):
        orders = db.get_all_orders_with_event_count()
        self.assertEqual(len(orders), 8)

    def test_has_event_count_field(self):
        orders = db.get_all_orders_with_event_count()
        for order in orders:
            self.assertIn("event_count", order)
            self.assertIsInstance(order["event_count"], int)

    def test_event_counts_match_timeline(self):
        orders = db.get_all_orders_with_event_count()
        for order in orders:
            timeline = db.get_order_timeline(order["order_id"])
            self.assertEqual(order["event_count"], len(timeline))


class TestSeedOrderEvents(DBTestCase):

    def test_all_orders_have_placed_event(self):
        """Every seeded order should have at least an order_placed event."""
        all_orders = db.get_all_orders()
        for order in all_orders:
            events = db.get_order_timeline(order["order_id"])
            placed = [e for e in events if e["event_type"] == "order_placed"]
            self.assertGreaterEqual(len(placed), 1,
                "Missing order_placed event for {}".format(order["order_id"]))

    def test_non_processing_orders_have_status_changed(self):
        """Orders not in 'processing' status should have a status_changed event."""
        all_orders = db.get_all_orders()
        for order in all_orders:
            if order["status"] != "processing":
                events = db.get_order_timeline(order["order_id"])
                status_events = [e for e in events if e["event_type"] == "status_changed"]
                self.assertGreaterEqual(len(status_events), 1,
                    "Missing status_changed event for {} (status={})".format(
                        order["order_id"], order["status"]))

    def test_processing_orders_have_only_placed_event(self):
        """Orders still in 'processing' should only have the placed event."""
        all_orders = db.get_all_orders()
        for order in all_orders:
            if order["status"] == "processing":
                events = db.get_order_timeline(order["order_id"])
                self.assertEqual(len(events), 1,
                    "Processing order {} has unexpected extra events".format(order["order_id"]))

    def test_seed_events_idempotent(self):
        """Calling init_db again should not duplicate events."""
        db.init_db()
        events = db.get_order_timeline("ORD-001")
        placed = [e for e in events if e["event_type"] == "order_placed"]
        self.assertEqual(len(placed), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
