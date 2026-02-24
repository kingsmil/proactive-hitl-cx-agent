import unittest
from agent.tools import sanitize_json_fragment

class TestSanitizeJsonFragment(unittest.TestCase):
    def test_valid_json(self):
        self.assertEqual(sanitize_json_fragment('{"a": 1}'), '{"a": 1}')
        
    def test_json_with_trailing_whitespace(self):
        self.assertEqual(sanitize_json_fragment('{"a": 1}   \n '), '{"a": 1}')
        
    def test_json_with_garbage(self):
        self.assertEqual(sanitize_json_fragment('{"a": {"b": 2}} missing_text'), '{"a": {"b": 2}}')
        
    def test_no_closing_brace(self):
        self.assertEqual(sanitize_json_fragment('{"a": 1'), '{"a": 1')
        
    def test_empty_string(self):
        self.assertEqual(sanitize_json_fragment(''), '')

if __name__ == "__main__":
    unittest.main()
