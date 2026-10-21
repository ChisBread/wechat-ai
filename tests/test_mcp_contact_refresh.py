"""Exercise MCP lookup and dispatch against a stale, mocked contact cache."""

import base64
import inspect
import json
import unittest
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_debug_routes import DummyConfig, DummyMcpBot
from tests.test_group_message_context import fixture as group_fixture
from tests.test_wechat_dat import LONG_PNG_BYTES
from wechat_ai_bot.mcp.app import AppContext, create_app
from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher


class McpContactRefreshTest(unittest.TestCase):
    def setUp(self):
        self.db, self.sender, self.room, _ = group_fixture()
        contacts = dict(self.db._contact_by_username)
        rooms = dict(self.db._chat_rooms)
        room_hashes = dict(self.db._room_by_md5)
        self.db._contact_by_username.clear()
        self.db._chat_rooms.clear()
        self.db._room_by_md5.clear()
        self.db.load_contacts = Mock(
            side_effect=lambda: self.db._contact_by_username.update(contacts)
        )

        def load_rooms():
            self.db._chat_rooms.update(rooms)
            self.db._room_by_md5.update(room_hashes)

        self.db.load_chat_rooms = Mock(side_effect=load_rooms)
        self.bot = DummyMcpBot()
        self.bot.database_service = self.db
        self.bot.rpa_task_queue = Queue()
        dispatcher = QueueCommandDispatcher(self.bot, self.db.user_info)
        self.dispatcher = Mock(wraps=dispatcher)
        self.context = SimpleNamespace(request_context=SimpleNamespace(
            lifespan_context=AppContext(self.db.user_info, self.dispatcher, {}, {})
        ))
        app = create_app(
            self.db.user_info, DummyConfig({"debug": {"enabled": False}}), bot=self.bot
        )
        self.tools = app._tool_manager._tools
        enabled = patch.dict("os.environ", {"WECHAT_AI_MCP_WRITE_ENABLED": "true"})
        enabled.start()
        self.addCleanup(enabled.stop)

    def call(self, tool, **kwargs):
        return json.loads(self.tools[tool].fn(self.context, **kwargs))

    def test_resolve_rpa_target_never_fabricates_unknown_usernames(self):
        # The resolver is local to create_app; inspect the actual tool closure.
        send = inspect.unwrap(self.tools["send_text_msg"].fn)
        resolve = inspect.getclosurevars(send).nonlocals["_resolve_rpa_target"]
        for name in ("missing", "missing group", "missing@chatroom", "未知群", ""):
            with self.subTest(name=name):
                result = resolve(name)
                self.assertEqual(result["error"], "contact_not_found")
                self.assertEqual(result["input"], name)
                self.assertNotIn("username", result)
                self.assertNotIn("target", result)
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_all_resolved_rpa_tools_reject_missing_targets_without_dispatch(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "synthetic.txt"
            file.write_text("synthetic")
            cases = [
                ("send_text_msg", {"recipient_name": "missing", "message": "hello"}),
                ("send_file_msg", {"recipient_name": "missing", "file_path": str(file)}),
                ("send_image_msg", {"recipient_name": "missing", "image_base64": base64.b64encode(LONG_PNG_BYTES).decode()}),
                ("send_pat_msg", {"user_name": "missing"}),
                ("remove_room_member", {"room_name": "missing", "member_name": "synthetic", "confirm": True}),
                ("invite_room_member", {"room_name": "missing", "user_name": "synthetic", "confirm": True}),
                ("rename_room_name", {"room_name": "missing", "new_name": "synthetic", "confirm": True}),
                ("rename_name_in_room", {"room_name": "missing", "new_name_in_room": "synthetic", "confirm": True}),
            ]
            with patch("wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory") as stage:
                for tool, kwargs in cases:
                    for dry_run in (False, True):
                        with self.subTest(tool=tool, dry_run=dry_run):
                            result = self.call(tool, dry_run=dry_run, **kwargs)
                            self.assertEqual(result["status"], "not_found")
                            self.assertEqual(result["error"], "contact_not_found")
                            self.assertFalse(result["queued"])
                            self.assertTrue(self.bot.rpa_task_queue.empty())
                stage.assert_not_called()
        self.assertEqual(self.dispatcher.mock_calls, [])

    def test_new_group_resolves_by_username_and_queues_real_target(self):
        result = self.call(
            "send_text_msg", recipient_name=self.room.username,
            message="synthetic message", wait_seconds=0,
        )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["resolved"]["username"], self.room.username)
        self.assertEqual(result["resolved"]["target"], self.room.display_name)
        action = self.bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.target, self.room.display_name)
        self.assertEqual(action.target_username, self.room.username)
        self.assertTrue(action.is_chatroom)
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_new_group_resolves_by_display_name_after_refresh(self):
        result = self.call(
            "send_text_msg", recipient_name=self.room.display_name,
            message="synthetic message", dry_run=True,
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["resolved"]["username"], self.room.username)
        self.assertEqual(result["resolved"]["contact"]["display_name"], self.room.display_name)
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()
        self.assertEqual(self.dispatcher.mock_calls, [])

    def test_recent_chats_includes_refreshed_group_payload_and_sender(self):
        result = self.call("get_recent_chats")
        self.assertEqual(result["status"], "ok")
        chat = result["chats"][0]
        self.assertEqual(chat["contact"]["username"], self.room.username)
        self.assertEqual(chat["contact"]["display_name"], self.room.display_name)
        self.assertTrue(chat["contact"]["is_chatroom"])
        self.assertIs(self.db.get_contact_by_sender_id(3, "synthetic.db"), self.sender)
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_recent_messages_recovers_new_group(self):
        result = self.call("get_recent_messages", contact_name=self.room.username)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["contact"]["display_name"], self.room.display_name)
        self.db.load_contacts.assert_called_once_with()

    def test_member_lookup_recovers_room_and_global_sender_name(self):
        result = self.call("query_room_member_list", room_name=self.room.display_name)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["members"][0]["username"], self.sender.username)
        self.assertEqual(result["members"][0]["nick_name"], self.sender.nick_name)
        self.assertEqual(self.db.get_contact_by_username(self.sender.username).display_name, "Alice")
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
