import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_ai_bot.mcp.debug import _dashboard_html, build_layout_png, mask_value, redact_text, tail_file
from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.utils import size_config


class DummySize:
    width = 640
    height = 480


class DummyWindowManager:
    size_config = DummySize()
    SIDE_BAR_WIDTH = 64
    SESSION_LIST_WIDTH = 250
    MSG_TOP_X = 314
    MSG_TOP_Y = 70
    MSG_WIDTH = 326
    MSG_HEIGHT = 280
    ICON_CONFIGS = {"send_button": {"position": [540, 430, 620, 465]}}
    current_window = {"region": [0, 0, 640, 480]}
    target_window_size = (640, 480)
    actual_window_geometry = {"x": 0, "y": 0, "width": 640, "height": 480}
    window_aligned = True
    window_id = "123"
    last_window_state = "ready"

    def refresh_window_geometry(self):
        return self.actual_window_geometry

    def ensure_action_ready(self):
        self.last_window_state = "ready"
        self.window_aligned = True
        return True


class DummyBot:
    window_manager = DummyWindowManager()
    chat_window_ready = False


class DummyContact:
    id = 1
    username = "alice"
    local_type = 0
    alias = "alice-alias"
    delete_flag = 0
    verify_flag = 0
    chat_room_notify = 0
    head_img_md5 = "md5"
    description = ""
    remark = "Alice"
    nick_name = "Alice Nick"
    room_remark = ""

    @property
    def display_name(self):
        return self.remark or self.nick_name or self.username

    @property
    def is_chatroom(self):
        return self.username.endswith("@chatroom")


class DummyRoomContact(DummyContact):
    id = 2
    username = "room@chatroom"
    remark = "Room"
    nick_name = "Room Nick"
    alias = ""


class DummyDatabaseService:
    is_available = True
    last_error = ""

    def __init__(self):
        self.contact = DummyContact()
        self.room = DummyRoomContact()
        self._contact_by_username = {
            self.contact.username: self.contact,
            self.room.username: self.room,
        }
        self._message_username_map = {
            self.contact.username: {Path("/tmp/message_0.db")},
            self.room.username: {Path("/tmp/message_0.db")},
        }
        self.refreshed = False

    def get_contact_by_username(self, username):
        return self._contact_by_username.get(username)

    def get_contact_by_display_name(self, name):
        if name in {"Alice", "alice"}:
            return [self.contact]
        if name in {"Room", "room"}:
            return [self.room]
        return []

    def get_contact_by_sender_id(self, sender_id, message_db_path=None):
        return self.contact

    def get_messages_by_username(self, username, count=10, order="desc"):
        return [
            (
                1,
                1001,
                1,
                10,
                1,
                1780998000,
                0,
                0,
                0,
                0,
                0,
                "",
                "hello",
                None,
                None,
                None,
                None,
                "/tmp/message_0.db",
            )
        ][:count]

    def query_text_messages(self, username, query=None, start_timestamp=None, end_timestamp=None, limit=10):
        return [("hello", "alice", "/tmp/message_0.db", 1780998000, 1001)]

    def get_room_member_list(self, username):
        if username == self.room.username:
            return [self.contact]
        return []

    def get_status(self):
        return {"available": self.is_available, "contacts": 1, "message_tables": 1}

    def refresh(self):
        self.refreshed = True
        return self.get_status()


class DummyMessageService:
    is_running = True
    is_paused = False
    message_queue = None

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False


class DummyMcpBot(DummyBot):
    def __init__(self):
        self.window_manager = DummyWindowManager()
        self.database_service = DummyDatabaseService()
        self.message_service = DummyMessageService()
        self.message_factory_service = None
        self.rpa_task_queue = None
        self.chat_window_ready = False


class DummyConfig(dict):
    def get(self, key, default=None):
        if "." not in key:
            return super().get(key, default)
        value = self
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class DebugRoutesTest(unittest.TestCase):
    def test_mask_value_keeps_edges(self):
        self.assertEqual(mask_value("wxid_abcdef"), "wx***ef")
        self.assertEqual(mask_value("abc"), "a***")
        self.assertEqual(mask_value(""), "")

    def test_redact_text_masks_common_secret_values(self):
        redacted = redact_text("api_key=sk-test password: hunter2 dbkey=abcdef")

        self.assertIn("api_key=***", redacted)
        self.assertIn("password: ***", redacted)
        self.assertIn("dbkey=***", redacted)
        self.assertNotIn("hunter2", redacted)

    def test_tail_file_returns_last_lines(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("\n".join(f"line-{i}" for i in range(20)), encoding="utf-8")

            self.assertEqual(tail_file(path, lines=3), "line-17\nline-18\nline-19")

    def test_build_layout_png_returns_png_bytes(self):
        png = build_layout_png(DummyBot())

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 100)

    def test_create_app_registers_debug_routes_when_enabled(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
            bot=DummyBot(),
        )
        paths = {route.path for route in app.streamable_http_app().routes}

        self.assertIn("/dashboard", paths)
        self.assertIn("/dashboard/api/status", paths)
        self.assertIn("/dashboard/api/chats", paths)
        self.assertIn("/dashboard/api/contact/detail", paths)
        self.assertIn("/dashboard/api/messages/recent", paths)
        self.assertIn("/dashboard/api/contacts", paths)
        self.assertIn("/dashboard/api/messages", paths)
        self.assertIn("/dashboard/api/rpa/send_text", paths)
        self.assertIn("/dashboard/api/rpa/action", paths)
        self.assertIn("/dashboard/api/window/reset", paths)
        self.assertIn("/debug", paths)
        self.assertIn("/debug/api/status", paths)
        self.assertIn("/debug/api/contacts", paths)
        self.assertIn("/debug/api/messages", paths)
        self.assertIn("/debug/api/rpa/send_text", paths)
        self.assertIn("/debug/api/window/reset", paths)

    def test_dashboard_html_loads_manager_template(self):
        html = _dashboard_html()

        self.assertIn("WeChat-AI 管理台", html)
        self.assertIn('data-tab="wechat"', html)
        self.assertIn("wechatChatList", html)
        self.assertIn('data-wechat-mode="contacts"', html)
        self.assertIn('data-wechat-mode="rooms"', html)
        self.assertIn("重置窗口尺寸", html)

    def test_create_app_skips_debug_routes_when_disabled(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyBot(),
        )
        paths = {route.path for route in app.streamable_http_app().routes}

        self.assertNotIn("/dashboard", paths)
        self.assertNotIn("/debug", paths)

    def test_create_app_registers_manager_mcp_tools(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )
        tools = set(app._tool_manager._tools)

        self.assertIn("get_database_status", tools)
        self.assertIn("refresh_database", tools)
        self.assertIn("get_contact_detail", tools)
        self.assertIn("search_text_messages", tools)
        self.assertIn("get_recent_media_messages", tools)
        self.assertIn("get_wechat_window_status", tools)
        self.assertIn("reset_wechat_window", tools)
        self.assertIn("set_message_polling", tools)

    def test_mcp_database_and_window_tools_return_json_status(self):
        bot = DummyMcpBot()
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=bot,
        )

        database = json.loads(app._tool_manager._tools["get_database_status"].fn(None))
        window = json.loads(app._tool_manager._tools["get_wechat_window_status"].fn(None))
        reset = json.loads(app._tool_manager._tools["reset_wechat_window"].fn(None))
        pause = json.loads(app._tool_manager._tools["set_message_polling"].fn(None, True))

        self.assertEqual(database["status"], "ok")
        self.assertEqual(window["status"], "ok")
        self.assertEqual(window["state"], "ready")
        self.assertEqual(reset["status"], "ok")
        self.assertTrue(bot.chat_window_ready)
        self.assertEqual(pause["status"], "ok")
        self.assertTrue(pause["paused"])

    def test_mcp_contact_and_message_tools_return_structured_payloads(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )

        contact = json.loads(app._tool_manager._tools["get_contact_detail"].fn(None, "Alice"))
        recent = json.loads(app._tool_manager._tools["get_recent_messages"].fn(None, "Alice", 5))
        searched = json.loads(app._tool_manager._tools["search_text_messages"].fn(None, "Alice", "hello"))

        self.assertEqual(contact["status"], "ok")
        self.assertTrue(contact["contact"]["has_message_table"])
        self.assertEqual(recent["status"], "ok")
        self.assertEqual(recent["messages"][0]["text"], "hello")
        self.assertEqual(searched["status"], "ok")
        self.assertEqual(searched["messages"][0]["text"], "hello")

    def test_dashboard_wechat_api_payloads(self):
        from starlette.testclient import TestClient

        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )
        client = TestClient(app.streamable_http_app())

        chats = client.get("/dashboard/api/chats")
        contacts = client.get("/dashboard/api/contacts?q=&type=contact")
        rooms = client.get("/dashboard/api/contacts?q=&type=chatroom")
        status = client.get("/dashboard/api/status")
        detail = client.get("/dashboard/api/contact/detail?username=room@chatroom")
        recent = client.get("/dashboard/api/messages/recent?username=alice")

        self.assertEqual(chats.status_code, 200)
        self.assertTrue(chats.json()["chats"])
        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(contacts.json()["contacts"][0]["username"], "alice")
        self.assertEqual(rooms.status_code, 200)
        self.assertEqual(rooms.json()["contacts"][0]["username"], "room@chatroom")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["debug"]["show_sensitive"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["member_count"], 1)
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.json()["messages"][0]["text"], "hello")

    def test_ported_write_tools_return_dry_run_payloads(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )
        tools = app._tool_manager._tools

        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "report.txt"
            file_path.write_text("hello", encoding="utf-8")
            cases = [
                (
                    "send_file_msg",
                    (None, "Alice", str(file_path), True),
                    "send_file",
                ),
                (
                    "send_pat_msg",
                    (None, "Alice", None, True),
                    "pat",
                ),
                (
                    "remove_room_member",
                    (None, "Room", "Alice", True),
                    "remove_room_member",
                ),
                (
                    "invite_room_member",
                    (None, "Room", "Alice", True),
                    "invice_2_room",
                ),
                (
                    "rename_room_name",
                    (None, "Room", "New Room", True),
                    "rename_room_name",
                ),
                (
                    "rename_name_in_room",
                    (None, "Room", "Me", True),
                    "rename_name_in_room",
                ),
            ]

            for tool_name, args, action_type in cases:
                with self.subTest(tool_name=tool_name):
                    payload = json.loads(tools[tool_name].fn(*args))

                    self.assertEqual(payload["status"], "dry_run")
                    self.assertEqual(payload["tool"], tool_name)
                    self.assertEqual(payload["action_type"], action_type)
                    self.assertNotEqual(payload["status"], "unavailable")

    def test_suggest_size_aligns_configured_window_to_factor(self):
        original_size = size_config.pyautogui.size
        size_config.pyautogui.size = lambda: type("Screen", (), {"width": 3840, "height": 1916})()
        try:
            suggested = size_config.suggest_size(
                {
                    "window": {
                        "width": 1008,
                        "height": 0,
                        "min_height": 812,
                        "max_height": 2000,
                        "align_factor": 28,
                    }
                }
            )
        finally:
            size_config.pyautogui.size = original_size

        self.assertEqual(suggested.width, 1008)
        self.assertEqual(suggested.height, 1820)


if __name__ == "__main__":
    unittest.main()
