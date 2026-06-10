import unittest
import base64
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_ai_bot.mcp.debug import _dashboard_html, build_layout_png, mask_value, redact_text, tail_file
from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.utils import size_config
from tests.test_wechat_dat import LONG_PNG_BYTES, make_dat


@contextmanager
def temporary_env(**values):
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def dashboard_auth_headers(username="admin", password="secret"):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def dashboard_mutation_headers(username="admin", password="secret"):
    return {**dashboard_auth_headers(username, password), "X-WeChat-AI-Dashboard": "1"}


def dashboard_auth_env(username="admin", password="secret"):
    return temporary_env(
        WECHAT_AI_DASHBOARD_USERNAME=username,
        WECHAT_AI_DASHBOARD_PASSWORD=password,
    )


def mcp_token_env(token="secret-token"):
    return temporary_env(WECHAT_AI_MCP_TOKEN=token)


def mcp_auth_headers(token="secret-token"):
    return {"Authorization": f"Bearer {token}"}


def mcp_admin_env(enabled=True):
    return temporary_env(WECHAT_AI_MCP_ADMIN_ENABLED="true" if enabled else None)


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
    small_head_url = "https://example.test/alice-small.jpg"
    big_head_url = "https://example.test/alice-big.jpg"

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


class DummyMediaContact(DummyContact):
    id = 3
    username = "media"
    remark = "Media"
    nick_name = "Media Nick"
    alias = ""


class DummyDatabaseService:
    is_available = True
    last_error = ""

    def __init__(self, root=None):
        self.root = Path(root or "/tmp")
        self._primary_account = type(
            "Account",
            (),
            {
                "account_id": "me",
                "account_dir": self.root,
                "db_storage_dir": self.root / "db_storage",
            },
        )()
        self.contact = DummyContact()
        self.room = DummyRoomContact()
        self.media = DummyMediaContact()
        self._contact_by_username = {
            self.contact.username: self.contact,
            self.room.username: self.room,
            self.media.username: self.media,
        }
        self._message_username_map = {
            self.contact.username: {Path("/tmp/message_0.db")},
            self.room.username: {Path("/tmp/message_0.db")},
            self.media.username: {Path("/tmp/message_0.db")},
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

    def _text_row(self):
        return (
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

    def _image_row(self):
        return (
            2,
            1002,
            3,
            11,
            1,
            1780998060,
            0,
            0,
            0,
            0,
            0,
            "",
            "<msg><img md5=\"image-md5\" /></msg>",
            None,
            None,
            None,
            None,
            "/tmp/message_0.db",
        )

    def get_messages_by_username(self, username, count=10, order="desc"):
        rows = [self._image_row() if username == "media" else self._text_row()]
        return rows[:count]

    def get_image(self, xml_content, message, up_dir="", md5=None, thumb=False, sender_wxid=""):
        return Path("msg/attach/alice/2026-06/Img/image_t.dat" if thumb else "msg/attach/alice/2026-06/Img/image.dat")

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


class DummyImageMessage:
    contact = DummyContact()
    room = None
    path = "msg/attach/alice/2026-06/Img/image.dat"
    thumb_path = "msg/attach/alice/2026-06/Img/image_t.dat"
    file_name = "image.dat"
    file_size = 2048
    file_type = "png"
    md5 = "image-md5"
    duration = 0

    def to_text(self):
        return "【图片】"

    def to_json(self):
        return {
            "text": "【图片】",
            "path": self.path,
            "thumb_path": self.thumb_path,
            "thumb_url": "https://example.test/thumb.gif",
            "desc": "sample image",
        }


class DummyMessageFactoryService:
    def create_message(self, message):
        return DummyImageMessage()


class DummyMcpBot(DummyBot):
    def __init__(self):
        self.window_manager = DummyWindowManager()
        self.database_service = DummyDatabaseService()
        self.message_service = DummyMessageService()
        self.message_factory_service = None
        self.rpa_task_queue = None
        self.chat_window_ready = False


class DummyMediaBot(DummyMcpBot):
    def __init__(self, root, user_info=None):
        self.window_manager = DummyWindowManager()
        self.database_service = DummyDatabaseService(root=root)
        self.message_service = DummyMessageService()
        self.message_factory_service = DummyMessageFactoryService()
        self.rpa_task_queue = None
        self.chat_window_ready = False
        self.user_info = user_info or UserInfo(account="me")


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
        self.assertIn("/dashboard/media", paths)
        self.assertIn("/dashboard/layout.png", paths)
        self.assertNotIn("/debug", paths)
        self.assertNotIn("/debug/api/status", paths)

    def test_dashboard_html_loads_manager_template(self):
        html = _dashboard_html()

        self.assertIn("WeChat-AI 管理台", html)
        self.assertIn('data-tab="wechat"', html)
        self.assertIn("wechatChatList", html)
        self.assertIn('data-wechat-mode="contacts"', html)
        self.assertIn('data-wechat-mode="rooms"', html)
        self.assertIn("重置窗口尺寸", html)
        self.assertIn("messageBodyHtml", html)
        self.assertIn("media-image", html)
        self.assertIn("profile-avatar", html)
        self.assertIn("locationMessageHtml", html)
        self.assertIn("图片 DAT Key", html)

    def test_create_app_skips_debug_routes_when_disabled(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyBot(),
        )
        paths = {route.path for route in app.streamable_http_app().routes}

        self.assertNotIn("/dashboard", paths)
        self.assertNotIn("/debug", paths)

    def test_dashboard_requires_non_default_basic_auth(self):
        from starlette.testclient import TestClient

        with dashboard_auth_env("wechat", "wechat"):
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            client = TestClient(app.streamable_http_app())

            missing = client.get("/dashboard")
            default_auth = client.get("/dashboard", headers=dashboard_auth_headers("wechat", "wechat"))

        self.assertEqual(missing.status_code, 401)
        self.assertIn("WWW-Authenticate", missing.headers)
        self.assertEqual(default_auth.status_code, 401)
        self.assertIn("default wechat/wechat is disabled", default_auth.text)

        with dashboard_auth_env("admin", "secret"):
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            client = TestClient(app.streamable_http_app())

            missing = client.get("/dashboard")
            wrong = client.get("/dashboard", headers=dashboard_auth_headers("admin", "wrong"))
            ok = client.get("/dashboard", headers=dashboard_auth_headers("admin", "secret"))
            api = client.get("/dashboard/api/status", headers=dashboard_auth_headers("admin", "secret"))
            old_debug = client.get("/debug", headers=dashboard_auth_headers("admin", "secret"))

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(api.status_code, 200)
        self.assertFalse(api.json()["debug"]["show_sensitive"])
        self.assertEqual(old_debug.status_code, 404)

    def test_dashboard_mutation_routes_require_dashboard_header(self):
        from starlette.testclient import TestClient

        with dashboard_auth_env("admin", "secret"):
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            client = TestClient(app.streamable_http_app())

            forbidden = client.post("/dashboard/api/message/pause", headers=dashboard_auth_headers())
            ok = client.post("/dashboard/api/message/pause", headers=dashboard_mutation_headers())

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["message"]["running"])

    def test_mcp_http_requires_non_default_bearer_token(self):
        from starlette.testclient import TestClient

        with mcp_token_env("wechat"):
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            with TestClient(app.streamable_http_app()) as client:
                missing = client.get("/mcp")
                default_auth = client.get("/mcp", headers=mcp_auth_headers("wechat"))

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(default_auth.status_code, 401)

        with mcp_token_env("secret-token"):
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            with TestClient(app.streamable_http_app()) as client:
                missing = client.get("/mcp")
                wrong = client.get("/mcp", headers=mcp_auth_headers("wrong"))
                authorized = client.get("/mcp", headers=mcp_auth_headers("secret-token"))

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertNotEqual(authorized.status_code, 401)

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
        with mcp_admin_env():
            reset = json.loads(app._tool_manager._tools["reset_wechat_window"].fn(None))
            pause = json.loads(app._tool_manager._tools["set_message_polling"].fn(None, True))

        self.assertEqual(database["status"], "ok")
        self.assertEqual(window["status"], "ok")
        self.assertEqual(window["state"], "ready")
        self.assertEqual(reset["status"], "ok")
        self.assertTrue(bot.chat_window_ready)
        self.assertEqual(pause["status"], "ok")
        self.assertTrue(pause["paused"])

    def test_mcp_admin_and_write_tools_are_blocked_by_default(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )
        tools = app._tool_manager._tools

        reset = json.loads(tools["reset_wechat_window"].fn(None))
        send = json.loads(tools["send_text_msg"].fn(None, "Alice", "hello", None, False))

        self.assertEqual(reset["status"], "blocked")
        self.assertEqual(reset["tool"], "reset_wechat_window")
        self.assertEqual(send["status"], "blocked")
        self.assertEqual(send["tool"], "send_text_msg")

    def test_mcp_high_impact_group_tools_require_confirmation(self):
        app = create_app(
            UserInfo(account="me"),
            DummyConfig({"debug": {"enabled": False}, "mcp": {"port": 8000}}),
            bot=DummyMcpBot(),
        )
        tools = app._tool_manager._tools

        payload = json.loads(tools["remove_room_member"].fn(None, "Room", "Alice", False))

        self.assertEqual(payload["status"], "confirmation_required")
        self.assertEqual(payload["tool"], "remove_room_member")
        self.assertEqual(payload["required_argument"], "confirm=true")

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

        with dashboard_auth_env():
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMcpBot(),
            )
            client = TestClient(app.streamable_http_app())
            headers = dashboard_auth_headers()

            chats = client.get("/dashboard/api/chats", headers=headers)
            contacts = client.get("/dashboard/api/contacts?q=&type=contact", headers=headers)
            rooms = client.get("/dashboard/api/contacts?q=&type=chatroom", headers=headers)
            status = client.get("/dashboard/api/status?show_sensitive=1", headers=headers)
            detail = client.get("/dashboard/api/contact/detail?username=room@chatroom", headers=headers)
            recent = client.get("/dashboard/api/messages/recent?username=alice", headers=headers)

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

    def test_dashboard_media_payload_and_proxy(self):
        from starlette.testclient import TestClient

        with TemporaryDirectory() as tmp, dashboard_auth_env():
            root = Path(tmp)
            png = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
                b"\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            image_dir = root / "msg" / "attach" / "alice" / "2026-06" / "Img"
            image_dir.mkdir(parents=True)
            (image_dir / "image.dat").write_bytes(png)
            (image_dir / "image_t.dat").write_bytes(png)
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMediaBot(root),
            )
            client = TestClient(app.streamable_http_app())
            headers = dashboard_auth_headers()

            recent = client.get("/dashboard/api/messages/recent?username=media", headers=headers)
            payload = recent.json()["messages"][0]
            media = client.get(payload["thumb_path_url"], headers=headers)

            self.assertEqual(recent.status_code, 200)
            self.assertEqual(payload["type_name"], "图片")
            self.assertEqual(payload["path_url"], "/dashboard/media?path=msg/attach/alice/2026-06/Img/image.dat")
            self.assertEqual(payload["thumb_path_url"], "/dashboard/media?path=msg/attach/alice/2026-06/Img/image_t.dat")
            self.assertEqual(payload["thumb_url"], "https://example.test/thumb.gif")
            self.assertEqual(payload["file_size_label"], "2.00 KB")
            self.assertEqual(payload["sender_avatar_url"], "https://example.test/alice-small.jpg")
            self.assertEqual(media.status_code, 200)
            self.assertEqual(media.headers["content-type"], "image/png")
            self.assertEqual(media.content, png)

    def test_dashboard_media_dat_requires_aes_key(self):
        from starlette.testclient import TestClient

        with TemporaryDirectory() as tmp, dashboard_auth_env():
            root = Path(tmp)
            image_dir = root / "msg" / "attach" / "alice" / "2026-06" / "Img"
            image_dir.mkdir(parents=True)
            dat_path = image_dir / "image.dat"
            dat_path.write_bytes(make_dat(plain=LONG_PNG_BYTES))
            app = create_app(
                UserInfo(account="me"),
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMediaBot(root),
            )
            client = TestClient(app.streamable_http_app())

            response = client.get(
                "/dashboard/media?path=msg/attach/alice/2026-06/Img/image.dat",
                headers=dashboard_auth_headers(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json()["dat"])
        self.assertIn("AES key", response.json()["error"])
        self.assertEqual(response.json()["inferred_dat_xor_key"], 60)

    def test_dashboard_media_dat_decrypts_with_user_info_keys(self):
        from starlette.testclient import TestClient

        key = "0123456789abcdef"
        plain = LONG_PNG_BYTES
        with TemporaryDirectory() as tmp, dashboard_auth_env():
            root = Path(tmp)
            image_dir = root / "msg" / "attach" / "alice" / "2026-06" / "Img"
            image_dir.mkdir(parents=True)
            dat_path = image_dir / "image.dat"
            dat_path.write_bytes(make_dat(plain=plain, key=key.encode("utf-8"), xor_key=60))
            user_info = UserInfo(account="me", dat_key=key, dat_xor_key=60)
            app = create_app(
                user_info,
                DummyConfig({"debug": {"enabled": True}, "mcp": {"port": 8000}}),
                bot=DummyMediaBot(root, user_info=user_info),
            )
            client = TestClient(app.streamable_http_app())

            response = client.get(
                "/dashboard/media?path=msg/attach/alice/2026-06/Img/image.dat",
                headers=dashboard_auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

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
