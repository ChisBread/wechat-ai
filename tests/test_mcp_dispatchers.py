import unittest
from queue import Queue
from threading import Thread

from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher, UnavailableCommandDispatcher
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.weixin.message_classes import MessageType


class DummyBot:
    def __init__(self):
        self.rpa_task_queue = Queue()


class QueueCommandDispatcherTest(unittest.TestCase):
    def test_dispatch_text_message_queues_send_text_action(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        dispatcher.dispatch(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.Text,
                "message_content": "hello",
                "nickname": "Alice",
                "is_chatroom": False,
                "at_list": [],
            },
        )

        action = bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.content, "hello")
        self.assertEqual(action.target, "Alice")
        self.assertFalse(action.is_chatroom)
        self.assertIsNone(action.at_user_name)
        self.assertEqual(action.action_type, RPAActionType.SEND_TEXT_MESSAGE)

    def test_dispatch_text_message_preserves_at_user(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        dispatcher.dispatch(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.Text,
                "message_content": "hello",
                "nickname": "Room",
                "is_chatroom": True,
                "at_list": ["Bob"],
            },
        )

        action = bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.target, "Room")
        self.assertTrue(action.is_chatroom)
        self.assertEqual(action.at_user_name, "Bob")

    def test_dispatch_rpa_queues_announcement(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        result = dispatcher.dispatch_rpa(
            RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT.value,
            {"target": "Room", "content": "notice", "force_edit": True},
        )

        action = bot.rpa_task_queue.get_nowait()
        self.assertIn("本地队列", result)
        self.assertEqual(action.target, "Room")
        self.assertEqual(action.content, "notice")
        self.assertTrue(action.force_edit)
        self.assertEqual(action.action_type, RPAActionType.PUBLIC_ROOM_ANNOUNCEMENT)

    def test_dispatch_rpa_queues_leave_room(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        dispatcher.dispatch_rpa(RPAActionType.LEAVE_ROOM.value, {"target": "Room"})

        action = bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.target, "Room")
        self.assertEqual(action.action_type, RPAActionType.LEAVE_ROOM)

    def test_dispatch_file_message_queues_send_file_action(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        dispatcher.dispatch(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.File,
                "file_path": "/tmp/report.pdf",
                "nickname": "Alice",
                "is_chatroom": False,
            },
        )

        action = bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.file_path, "/tmp/report.pdf")
        self.assertEqual(action.target, "Alice")
        self.assertFalse(action.is_chatroom)
        self.assertEqual(action.action_type, RPAActionType.SEND_FILE)

    def test_dispatch_pat_message_queues_pat_action(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        dispatcher.dispatch(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.Pat,
                "room_name": "Room",
                "user_name": "Bob",
                "is_chatroom": True,
            },
        )

        action = bot.rpa_task_queue.get_nowait()
        self.assertEqual(action.target, "Room")
        self.assertEqual(action.user_name, "Bob")
        self.assertTrue(action.is_chatroom)
        self.assertEqual(action.action_type, RPAActionType.PAT)

    def test_dispatch_rpa_queues_new_ported_actions(self):
        cases = [
            (
                RPAActionType.SEND_FILE.value,
                {"target": "Alice", "file_path": "/tmp/a.txt", "is_chatroom": False},
                RPAActionType.SEND_FILE,
                {"target": "Alice", "file_path": "/tmp/a.txt", "is_chatroom": False},
            ),
            (
                RPAActionType.PAT.value,
                {"target": "Room", "user_name": "Bob", "is_chatroom": True},
                RPAActionType.PAT,
                {"target": "Room", "user_name": "Bob", "is_chatroom": True},
            ),
            (
                RPAActionType.REMOVE_ROOM_MEMBER.value,
                {"room_name": "Room", "member_name": "Bob"},
                RPAActionType.REMOVE_ROOM_MEMBER,
                {"target": "Room", "user_name": "Bob"},
            ),
            (
                RPAActionType.INVITE_2_ROOM.value,
                {"room_name": "Room", "user_name": "Alice"},
                RPAActionType.INVITE_2_ROOM,
                {"target": "Room", "user_name": "Alice"},
            ),
            (
                RPAActionType.RENAME_ROOM_NAME.value,
                {"room_name": "Room", "new_name": "New Room"},
                RPAActionType.RENAME_ROOM_NAME,
                {"target": "Room", "name": "New Room"},
            ),
            (
                RPAActionType.RENAME_NAME_IN_ROOM.value,
                {"room_name": "Room", "new_name_in_room": "Me"},
                RPAActionType.RENAME_NAME_IN_ROOM,
                {"target": "Room", "name": "Me"},
            ),
        ]

        for action_type, action_data, expected_type, expected_fields in cases:
            with self.subTest(action_type=action_type):
                bot = DummyBot()
                dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

                dispatcher.dispatch_rpa(action_type, action_data)

                action = bot.rpa_task_queue.get_nowait()
                self.assertEqual(action.action_type, expected_type)
                for field_name, expected in expected_fields.items():
                    self.assertEqual(getattr(action, field_name), expected)

    def test_dispatch_wait_returns_execution_result(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        def worker():
            action = bot.rpa_task_queue.get(timeout=1)
            action.result_queue.put({"ok": True, "action_type": action.action_type.value})

        thread = Thread(target=worker)
        thread.start()
        result = dispatcher.dispatch_wait(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.Text,
                "message_content": "hello",
                "nickname": "Alice",
                "is_chatroom": False,
                "at_list": [],
            },
            timeout=1,
        )
        thread.join(timeout=1)

        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["completed"])
        self.assertEqual(result["result"]["action_type"], RPAActionType.SEND_TEXT_MESSAGE.value)

    def test_dispatch_wait_can_queue_without_waiting(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        result = dispatcher.dispatch_wait(
            "msg/me/rpa_action",
            {
                "local_type": MessageType.Text,
                "message_content": "hello",
                "nickname": "Alice",
                "is_chatroom": False,
                "at_list": [],
            },
            timeout=0,
        )

        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["completed"])
        self.assertFalse(bot.rpa_task_queue.empty())

    def test_dispatch_rpa_wait_returns_execution_result(self):
        bot = DummyBot()
        dispatcher = QueueCommandDispatcher(bot, UserInfo(account="me"))

        def worker():
            action = bot.rpa_task_queue.get(timeout=1)
            action.result_queue.put({"ok": True, "action_type": action.action_type.value})

        thread = Thread(target=worker)
        thread.start()
        result = dispatcher.dispatch_rpa_wait(
            RPAActionType.RENAME_ROOM_NAME.value,
            {"room_name": "Room", "new_name": "New Room"},
            timeout=1,
        )
        thread.join(timeout=1)

        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["completed"])
        self.assertEqual(result["result"]["action_type"], RPAActionType.RENAME_ROOM_NAME.value)

    def test_unavailable_dispatcher_reports_reason(self):
        dispatcher = UnavailableCommandDispatcher("missing dispatcher")

        with self.assertRaisesRegex(ConnectionError, "missing dispatcher"):
            dispatcher.dispatch("topic", {})
        with self.assertRaisesRegex(ConnectionError, "missing dispatcher"):
            dispatcher.dispatch_wait("topic", {}, timeout=1)
        with self.assertRaisesRegex(ConnectionError, "missing dispatcher"):
            dispatcher.dispatch_rpa("action", {})
        with self.assertRaisesRegex(ConnectionError, "missing dispatcher"):
            dispatcher.dispatch_rpa_wait("action", {}, timeout=1)


if __name__ == "__main__":
    unittest.main()
