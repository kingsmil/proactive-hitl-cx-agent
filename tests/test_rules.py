"""Unit tests for Rules AI feature — DB helpers, scheduled task CRUD, and orchestrator."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import db


def _reset_connection():
    """Close and remove any thread-local connection so the next _conn() call
    opens a fresh one against whatever db.DB_PATH is currently set to."""
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


class DBTestCase(unittest.TestCase):
    """Base class: each test gets its own temporary SQLite database and
    an isolated scheduledTasks directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = db.DB_PATH
        self._orig_tasks_dir = db._TASKS_DIR
        db.DB_PATH = Path(self.tmpdir) / "test.db"
        db._TASKS_DIR = Path(self.tmpdir) / "scheduledTasks"
        _reset_connection()
        db.init_db()

    def tearDown(self):
        _reset_connection()
        db.DB_PATH = self._orig_db_path
        db._TASKS_DIR = self._orig_tasks_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rules chat DB helpers
# ---------------------------------------------------------------------------

class TestAppendRulesChatMessage(DBTestCase):

    def test_returns_message_id(self):
        msg_id = db.append_rules_chat_message("user", "hello")
        self.assertIsInstance(msg_id, str)
        self.assertTrue(len(msg_id) > 0)

    def test_persists_user_message(self):
        db.append_rules_chat_message("user", "create a rule")
        rows = db.get_rules_chat_display()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "user")
        self.assertEqual(rows[0]["content"], "create a rule")

    def test_persists_assistant_message_with_tool_calls(self):
        tool_calls = json.dumps([{"id": "tc1", "function": {"name": "list_rules"}}])
        db.append_rules_chat_message("assistant", "", tool_calls=tool_calls)
        rows = db.get_rules_chat_display()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_calls"], tool_calls)

    def test_persists_tool_result(self):
        db.append_rules_chat_message("tool", "No rules configured.", tool_call_id="tc1")
        rows = db.get_rules_chat_display()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_call_id"], "tc1")


class TestGetRulesChatHistory(DBTestCase):

    def test_empty_history(self):
        history = db.get_rules_chat_history()
        self.assertEqual(history, [])

    def test_formats_for_llm(self):
        db.append_rules_chat_message("user", "list rules")
        tool_calls = [{"id": "tc1", "function": {"name": "list_rules", "arguments": "{}"}}]
        db.append_rules_chat_message("assistant", "", tool_calls=json.dumps(tool_calls))
        db.append_rules_chat_message("tool", "No rules configured.", tool_call_id="tc1")

        history = db.get_rules_chat_history()
        self.assertEqual(len(history), 3)

        # User message: just role + content
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "list rules")
        self.assertNotIn("tool_call_id", history[0])

        # Assistant message: includes parsed tool_calls
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIsInstance(history[1]["tool_calls"], list)
        self.assertEqual(history[1]["tool_calls"][0]["id"], "tc1")

        # Tool result: includes tool_call_id
        self.assertEqual(history[2]["role"], "tool")
        self.assertEqual(history[2]["tool_call_id"], "tc1")

    def test_preserves_chronological_order(self):
        db.append_rules_chat_message("user", "first")
        db.append_rules_chat_message("assistant", "second")
        db.append_rules_chat_message("user", "third")
        history = db.get_rules_chat_history()
        contents = [m["content"] for m in history]
        self.assertEqual(contents, ["first", "second", "third"])


class TestClearRulesChatHistory(DBTestCase):

    def test_clears_all_messages(self):
        db.append_rules_chat_message("user", "hello")
        db.append_rules_chat_message("assistant", "hi there")
        db.clear_rules_chat_history()
        self.assertEqual(db.get_rules_chat_history(), [])

    def test_clear_on_empty_is_safe(self):
        db.clear_rules_chat_history()
        self.assertEqual(db.get_rules_chat_history(), [])


class TestGetRulesChatDisplay(DBTestCase):

    def test_includes_all_fields(self):
        db.append_rules_chat_message("user", "test message")
        rows = db.get_rules_chat_display()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("message_id", row)
        self.assertIn("role", row)
        self.assertIn("content", row)
        self.assertIn("created_at", row)
        self.assertIn("tool_calls", row)
        self.assertIn("tool_call_id", row)


# ---------------------------------------------------------------------------
# Scheduled task file-based CRUD
# ---------------------------------------------------------------------------

class TestListScheduledTasks(DBTestCase):

    def test_empty_when_no_files(self):
        tasks = db.list_scheduled_tasks()
        self.assertEqual(tasks, [])

    def test_reads_json_files(self):
        task = {"task_id": "test_rule", "enabled": True, "cron": "0 * * * *"}
        db.save_scheduled_task(task)
        tasks = db.list_scheduled_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], "test_rule")

    def test_skips_corrupt_json(self):
        db._TASKS_DIR.mkdir(exist_ok=True)
        (db._TASKS_DIR / "bad.json").write_text("{invalid json")
        tasks = db.list_scheduled_tasks()
        self.assertEqual(tasks, [])

    def test_returns_sorted_by_filename(self):
        db.save_scheduled_task({"task_id": "beta", "cron": "0 * * * *"})
        db.save_scheduled_task({"task_id": "alpha", "cron": "0 * * * *"})
        tasks = db.list_scheduled_tasks()
        self.assertEqual([t["task_id"] for t in tasks], ["alpha", "beta"])


class TestGetScheduledTask(DBTestCase):

    def test_returns_none_for_missing(self):
        self.assertIsNone(db.get_scheduled_task("nonexistent"))

    def test_returns_task_dict(self):
        task = {"task_id": "my_rule", "enabled": True, "cron": "0 10 * * *"}
        db.save_scheduled_task(task)
        result = db.get_scheduled_task("my_rule")
        self.assertIsNotNone(result)
        self.assertEqual(result["task_id"], "my_rule")
        self.assertTrue(result["enabled"])


class TestSaveScheduledTask(DBTestCase):

    def test_creates_file(self):
        task = {"task_id": "new_rule", "enabled": False, "cron": "*/5 * * * *"}
        db.save_scheduled_task(task)
        fp = db._TASKS_DIR / "new_rule.json"
        self.assertTrue(fp.exists())
        with open(fp) as f:
            saved = json.load(f)
        self.assertEqual(saved["task_id"], "new_rule")

    def test_overwrites_existing(self):
        db.save_scheduled_task({"task_id": "rule1", "cron": "old"})
        db.save_scheduled_task({"task_id": "rule1", "cron": "new"})
        result = db.get_scheduled_task("rule1")
        self.assertEqual(result["cron"], "new")

    def test_creates_directory_if_missing(self):
        # Remove the dir created by setUp so save has to re-create it
        tasks_dir = db._TASKS_DIR
        if tasks_dir.exists():
            shutil.rmtree(tasks_dir)
        db.save_scheduled_task({"task_id": "auto_dir", "cron": "0 0 * * *"})
        self.assertTrue(tasks_dir.exists())


class TestDeleteScheduledTask(DBTestCase):

    def test_returns_false_for_missing(self):
        self.assertFalse(db.delete_scheduled_task("ghost"))

    def test_returns_true_and_removes_file(self):
        db.save_scheduled_task({"task_id": "doomed", "cron": "0 0 * * *"})
        self.assertTrue(db.delete_scheduled_task("doomed"))
        self.assertIsNone(db.get_scheduled_task("doomed"))

    def test_second_delete_returns_false(self):
        db.save_scheduled_task({"task_id": "once", "cron": "0 0 * * *"})
        db.delete_scheduled_task("once")
        self.assertFalse(db.delete_scheduled_task("once"))


class TestToggleScheduledTask(DBTestCase):

    def test_returns_none_for_missing(self):
        self.assertIsNone(db.toggle_scheduled_task("nope", True))

    def test_enables_disabled_task(self):
        db.save_scheduled_task({"task_id": "t1", "enabled": False, "cron": "0 0 * * *"})
        result = db.toggle_scheduled_task("t1", True)
        self.assertTrue(result["enabled"])
        # Verify persisted
        self.assertTrue(db.get_scheduled_task("t1")["enabled"])

    def test_disables_enabled_task(self):
        db.save_scheduled_task({"task_id": "t2", "enabled": True, "cron": "0 0 * * *"})
        result = db.toggle_scheduled_task("t2", False)
        self.assertFalse(result["enabled"])


# ---------------------------------------------------------------------------
# Rules AI tool implementations
# ---------------------------------------------------------------------------

class TestRulesAiToolImplementations(DBTestCase):

    def test_exec_list_rules_empty(self):
        from agent.rules_ai import _exec_list_rules
        result = _exec_list_rules()
        self.assertEqual(result, "No rules configured yet.")

    def test_exec_list_rules_shows_tasks(self):
        from agent.rules_ai import _exec_list_rules
        db.save_scheduled_task({
            "task_id": "delay_check",
            "enabled": True,
            "cron": "0 * * * *",
            "filters": {"status": "delayed"},
        })
        result = _exec_list_rules()
        self.assertIn("delay_check", result)
        self.assertIn("enabled", result)

    def test_exec_get_rule_found(self):
        from agent.rules_ai import _exec_get_rule
        db.save_scheduled_task({"task_id": "r1", "cron": "0 0 * * *", "enabled": True})
        result = _exec_get_rule("r1")
        parsed = json.loads(result)
        self.assertEqual(parsed["task_id"], "r1")

    def test_exec_get_rule_not_found(self):
        from agent.rules_ai import _exec_get_rule
        result = _exec_get_rule("missing")
        self.assertIn("not found", result)

    @patch("agent.rules_ai.reload_scheduler", create=True)
    def test_exec_create_rule_with_valid_cron(self, _mock_reload):
        from agent.rules_ai import _exec_create_or_update_rule
        # Patch poller import inside the function
        with patch.dict("sys.modules", {"poller": MagicMock(reload_scheduler=MagicMock())}):
            result = _exec_create_or_update_rule(
                task_id="new_rule",
                cron="0 10 * * *",
                filters={"status": "delayed"},
                system_prompt_override="Be helpful.",
                enabled=True,
            )
        self.assertIn("created", result)
        saved = db.get_scheduled_task("new_rule")
        self.assertIsNotNone(saved)
        self.assertTrue(saved["enabled"])

    @patch("agent.rules_ai.reload_scheduler", create=True)
    def test_exec_create_rule_reports_updated_for_existing(self, _mock_reload):
        from agent.rules_ai import _exec_create_or_update_rule
        db.save_scheduled_task({
            "task_id": "existing",
            "cron": "0 0 * * *",
            "enabled": False,
            "filters": {},
            "system_prompt_override": "",
        })
        with patch.dict("sys.modules", {"poller": MagicMock(reload_scheduler=MagicMock())}):
            result = _exec_create_or_update_rule(
                task_id="existing",
                cron="0 12 * * *",
                filters={},
                system_prompt_override="Updated.",
                enabled=True,
            )
        self.assertIn("updated", result)

    def test_exec_create_rule_rejects_invalid_cron(self):
        from agent.rules_ai import _exec_create_or_update_rule
        result = _exec_create_or_update_rule(
            task_id="bad",
            cron="not a cron",
            filters={},
            system_prompt_override="",
            enabled=True,
        )
        self.assertIn("Invalid cron", result)
        # Should NOT have saved the task
        self.assertIsNone(db.get_scheduled_task("bad"))

    @patch("agent.rules_ai.reload_scheduler", create=True)
    def test_exec_delete_rule_found(self, _mock_reload):
        from agent.rules_ai import _exec_delete_rule
        db.save_scheduled_task({"task_id": "del_me", "cron": "0 0 * * *"})
        with patch.dict("sys.modules", {"poller": MagicMock(reload_scheduler=MagicMock())}):
            result = _exec_delete_rule("del_me")
        self.assertIn("deleted", result)
        self.assertIsNone(db.get_scheduled_task("del_me"))

    def test_exec_delete_rule_not_found(self):
        from agent.rules_ai import _exec_delete_rule
        result = _exec_delete_rule("ghost")
        self.assertIn("not found", result)

    @patch("agent.rules_ai.reload_scheduler", create=True)
    def test_exec_toggle_rule(self, _mock_reload):
        from agent.rules_ai import _exec_toggle_rule
        db.save_scheduled_task({"task_id": "tog", "enabled": True, "cron": "0 0 * * *"})
        with patch.dict("sys.modules", {"poller": MagicMock(reload_scheduler=MagicMock())}):
            result = _exec_toggle_rule("tog", False)
        self.assertIn("disabled", result)

    def test_exec_toggle_rule_not_found(self):
        from agent.rules_ai import _exec_toggle_rule
        result = _exec_toggle_rule("nope", True)
        self.assertIn("not found", result)


# ---------------------------------------------------------------------------
# Rules AI orchestrator
# ---------------------------------------------------------------------------

class TestRunRulesAi(DBTestCase):

    @patch("agent.rules_ai.call_llm_with_custom_prompt")
    def test_returns_text_reply(self, mock_llm):
        """Orchestrator returns LLM's text reply and persists it."""
        mock_llm.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Here are your rules."},
            }]
        }
        import asyncio
        from agent.rules_ai import run_rules_ai

        result = asyncio.get_event_loop().run_until_complete(run_rules_ai("list all rules"))
        self.assertEqual(result, "Here are your rules.")

        # Should have persisted user + assistant messages
        history = db.get_rules_chat_history()
        roles = [m["role"] for m in history]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    @patch("agent.rules_ai.call_llm_with_custom_prompt")
    def test_executes_tool_then_returns_reply(self, mock_llm):
        """Orchestrator executes tools and then returns the final text reply."""
        # First call: LLM returns a tool call
        tool_call_response = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tc_1",
                        "function": {
                            "name": "list_rules",
                            "arguments": "{}",
                        },
                    }],
                },
            }]
        }
        # Second call: LLM returns a text reply
        text_response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "No rules found."},
            }]
        }
        mock_llm.side_effect = [tool_call_response, text_response]

        import asyncio
        from agent.rules_ai import run_rules_ai

        result = asyncio.get_event_loop().run_until_complete(run_rules_ai("show rules"))
        self.assertEqual(result, "No rules found.")

        # Verify tool result was persisted
        history = db.get_rules_chat_history()
        tool_msgs = [m for m in history if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("No rules configured", tool_msgs[0]["content"])

    @patch("agent.rules_ai.call_llm_with_custom_prompt")
    def test_max_iterations_safety(self, mock_llm):
        """Orchestrator returns fallback after MAX_ITERATIONS of tool calls."""
        # Always return a tool call — never a text reply
        mock_llm.return_value = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tc_loop",
                        "function": {"name": "list_rules", "arguments": "{}"},
                    }],
                },
            }]
        }

        import asyncio
        from agent.rules_ai import run_rules_ai

        result = asyncio.get_event_loop().run_until_complete(run_rules_ai("loop forever"))
        self.assertIn("maximum number of steps", result)

    @patch("agent.rules_ai.call_llm_with_custom_prompt")
    def test_unknown_tool_handled_gracefully(self, mock_llm):
        """Orchestrator handles unknown tool names without crashing."""
        tool_call_response = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tc_bad",
                        "function": {
                            "name": "nonexistent_tool",
                            "arguments": "{}",
                        },
                    }],
                },
            }]
        }
        text_response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Sorry, something went wrong."},
            }]
        }
        mock_llm.side_effect = [tool_call_response, text_response]

        import asyncio
        from agent.rules_ai import run_rules_ai

        result = asyncio.get_event_loop().run_until_complete(run_rules_ai("do something"))
        self.assertEqual(result, "Sorry, something went wrong.")

        # Verify the unknown tool error was persisted
        history = db.get_rules_chat_history()
        tool_msgs = [m for m in history if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("Unknown tool", tool_msgs[0]["content"])


# ---------------------------------------------------------------------------
# Shared proactive identity override
# ---------------------------------------------------------------------------

class TestProactiveIdentityOverride(unittest.TestCase):

    def test_override_is_in_proactive_system_prompt(self):
        from agent.llm_client import PROACTIVE_IDENTITY_OVERRIDE, PROACTIVE_SYSTEM_PROMPT
        self.assertIn(PROACTIVE_IDENTITY_OVERRIDE, PROACTIVE_SYSTEM_PROMPT)

    def test_proactive_prompt_does_not_ask_for_phone(self):
        from agent.llm_client import PROACTIVE_SYSTEM_PROMPT
        self.assertNotIn(
            "ask for their phone number for verification",
            PROACTIVE_SYSTEM_PROMPT,
        )

    def test_default_prompt_asks_for_phone(self):
        from agent.llm_client import SYSTEM_PROMPT
        self.assertIn("phone number", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
