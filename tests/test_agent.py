import json
import unittest
from agent import _sanitize_json_fragment
from agent.tools import check_order_status
from unittest.mock import patch

class TestAgentHelpers(unittest.TestCase):
    def test_sanitize_json_fragment_clean(self):
        js = '{"order_id": "ORD-123"}'
        self.assertEqual(_sanitize_json_fragment(js), js)
        
    def test_sanitize_json_fragment_dirty(self):
        js = '{"order_id": "ORD-123"}\n\nHere is some reasoning the LLM appended.'
        clean = '{"order_id": "ORD-123"}'
        self.assertEqual(_sanitize_json_fragment(js), clean)
        # Should be able to parse it
        json.loads(_sanitize_json_fragment(js))

    def test_sanitize_json_fragment_no_braces(self):
        # Edge case: no braces at all. Should just return the raw string
        # (which will then fail json.loads, which is correct behavior for garbage input).
        js = "garbage text"
        self.assertEqual(_sanitize_json_fragment(js), js)


class TestTools(unittest.TestCase):
    @patch('agent.tools.db.get_order')
    def test_check_order_status_found(self, mock_get_order):
        mock_get_order.return_value = {
            "order_id": "ORD-001",
            "status": "processing",
            "last_updated": "2023-10-27T10:00:00Z"
        }
        res = check_order_status("ORD-001")
        self.assertIn("status='processing'", res)
        self.assertIn("10:00", res)
        mock_get_order.assert_called_once_with("ORD-001")

    @patch('agent.tools.db.get_order')
    def test_check_order_status_not_found(self, mock_get_order):
        mock_get_order.return_value = None
        res = check_order_status("ORD-999")
        self.assertEqual(res, "Order ORD-999 not found.")
