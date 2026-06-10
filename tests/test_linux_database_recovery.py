import unittest
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

from wechat_ai_bot.services.core.message_service import MessageService


@dataclass
class StubUserInfo:
    account: str = ""
    data_dir: str = ""


def load_linux_database_service():
    try:
        from wechat_ai_bot.models import UserInfo
        from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService

        return LinuxDatabaseService, UserInfo
    except ModuleNotFoundError as exc:
        if exc.name not in {"google", "xmltodict", "lxml", "zstandard"}:
            raise

    class Contact:
        pass

    class ChatRoom:
        pass

    class FMessage:
        pass

    module_names = [
        "wechat_ai_bot.models",
        "wechat_ai_bot.weixin.parser.util.common",
    ]
    previous_modules = {name: sys.modules.get(name) for name in module_names}
    try:
        models = types.ModuleType("wechat_ai_bot.models")
        models.UserInfo = StubUserInfo
        models.Contact = Contact
        models.ChatRoom = ChatRoom
        models.FMessage = FMessage
        sys.modules["wechat_ai_bot.models"] = models

        common = types.ModuleType("wechat_ai_bot.weixin.parser.util.common")
        common.get_md5_from_xml = lambda *_args, **_kwargs: None
        sys.modules["wechat_ai_bot.weixin.parser.util.common"] = common

        from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService

        return LinuxDatabaseService, StubUserInfo
    finally:
        for name, module in previous_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


LinuxDatabaseService, UserInfo = load_linux_database_service()


class FakeDatabaseError(Exception):
    pass


FakeDatabaseError.__module__ = "sqlite3"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FlakyConnection:
    def __init__(self, fail_query=False):
        self.fail_query = fail_query
        self.closed = False

    def execute(self, query, _params=()):
        if self.fail_query and query == "select 1":
            self.fail_query = False
            raise FakeDatabaseError("database is locked")
        return FakeCursor([(1,)])

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.connections = []

    def connect(self, _path, **_kwargs):
        conn = FlakyConnection(fail_query=not self.connections)
        self.connections.append(conn)
        return conn


class LinuxDatabaseRecoveryTest(unittest.TestCase):
    def test_execute_query_reopens_connection_once_on_database_error(self):
        service = LinuxDatabaseService(UserInfo(account="me"))
        driver = FakeDriver()
        db_path = Path("/tmp/message_0.db")
        service._driver = driver
        service._keys[db_path] = "00" * 32
        service._connection(db_path)

        rows = service.execute_query(db_path, "select 1")

        self.assertEqual(rows, [(1,)])
        self.assertEqual(len(driver.connections), 2)
        self.assertTrue(driver.connections[0].closed)

    def test_check_new_messages_refreshes_after_repeated_table_failures(self):
        service = LinuxDatabaseService(
            UserInfo(account="me"),
            key_retry_interval=0,
            message_map_refresh_interval=0,
        )
        service.is_available = True
        service._table_username_map[(Path("/tmp/message_0.db"), "Msg_bad")] = "alice"
        service._fetch_new_rows = lambda *_args: (_ for _ in ()).throw(
            FakeDatabaseError("file is not a database")
        )
        refreshes = []

        def fake_refresh():
            refreshes.append(True)
            service._consecutive_poll_errors = 0
            return {"available": True, "contacts": 1, "message_tables": 1}

        service.refresh = fake_refresh

        for _ in range(3):
            self.assertEqual(service.check_new_messages(), [])

        self.assertEqual(len(refreshes), 1)

    def test_refresh_preserving_sequences_does_not_swallow_internal_type_error(self):
        service = LinuxDatabaseService(UserInfo(account="me"))

        def fake_refresh(**_kwargs):
            raise TypeError("internal refresh type bug")

        service.refresh = fake_refresh

        with self.assertRaisesRegex(TypeError, "internal refresh type bug"):
            service._refresh_preserving_sequences()

    def test_prime_message_sequences_can_preserve_existing_offsets(self):
        service = LinuxDatabaseService(UserInfo(account="me"))
        key = (Path("/tmp/message_0.db"), "Msg_old")
        new_key = (Path("/tmp/message_0.db"), "Msg_new")
        service._table_username_map[key] = "alice"
        service._table_username_map[new_key] = "bob"
        max_id_calls = []

        service._max_local_id = lambda _db_path, table_name: {
            "Msg_old": 99,
            "Msg_new": 7,
        }[max_id_calls.append(table_name) or table_name]

        service._prime_message_sequences({key: 42})

        self.assertEqual(service._table_sequences[key], 42)
        self.assertEqual(service._table_sequences[new_key], 7)
        self.assertEqual(max_id_calls, ["Msg_new"])


class MessageServiceRecoveryTest(unittest.TestCase):
    def test_message_service_refreshes_after_repeated_top_level_errors(self):
        class FailingDb:
            key_retry_interval = 0

            def __init__(self):
                self.refresh_count = 0

            def check_new_messages(self):
                raise FakeDatabaseError("schema has changed")

            def refresh(self):
                self.refresh_count += 1
                return {"available": True, "contacts": 1, "message_tables": 1}

        db = FailingDb()
        service = MessageService(Queue(), db)

        for _ in range(3):
            try:
                raise FakeDatabaseError("schema has changed")
            except Exception:
                service._consecutive_errors += 1
                if service._consecutive_errors >= 3:
                    service._try_refresh_database()

        self.assertEqual(db.refresh_count, 1)
        self.assertEqual(service._consecutive_errors, 0)

    def test_message_service_refresh_preserves_database_sequences(self):
        class RecordingDb:
            key_retry_interval = 0

            def __init__(self):
                self.kwargs = []

            def refresh(self, **kwargs):
                self.kwargs.append(kwargs)
                return {"available": True}

        db = RecordingDb()
        service = MessageService(Queue(), db)
        service._consecutive_errors = 3

        service._try_refresh_database()

        self.assertEqual(db.kwargs, [{"preserve_sequences": True}])
        self.assertEqual(service._consecutive_errors, 0)

    def test_message_service_accepts_empty_refresh_status(self):
        class EmptyStatusDb:
            key_retry_interval = 0

            def __init__(self):
                self.refresh_count = 0

            def refresh(self):
                self.refresh_count += 1
                return None

        db = EmptyStatusDb()
        service = MessageService(Queue(), db)
        service._consecutive_errors = 3

        service._try_refresh_database()

        self.assertEqual(db.refresh_count, 1)
        self.assertEqual(service._consecutive_errors, 3)

    def test_message_service_does_not_swallow_internal_refresh_type_error(self):
        class BrokenDb:
            key_retry_interval = 0

            def __init__(self):
                self.refresh_count = 0

            def refresh(self, **_kwargs):
                self.refresh_count += 1
                raise TypeError("internal refresh type bug")

        db = BrokenDb()
        service = MessageService(Queue(), db)

        with self.assertRaisesRegex(TypeError, "internal refresh type bug"):
            service._call_refresh(db.refresh)

        self.assertEqual(db.refresh_count, 1)


if __name__ == "__main__":
    unittest.main()
