import time
import unittest
from dataclasses import dataclass
from queue import Queue

from wechat_ai_bot.rpa.rpa_action import RPAAction, RPAActionType
from wechat_ai_bot.services.core.rpa_service import RPAService


@dataclass
class TestAction(RPAAction):
    def __post_init__(self):
        self.action_type = RPAActionType.SEND_TEXT_MESSAGE
        self.is_send_message = True


class DummyController:
    def __init__(self, result=True):
        self.result = result
        self.actions = []

    def execute_action(self, action):
        self.actions.append(action)
        return self.result


class RPAServiceTest(unittest.TestCase):
    def test_result_queue_receives_execution_status(self):
        task_queue = Queue()
        result_queue = Queue(maxsize=1)
        controller = DummyController(result=True)
        service = RPAService(task_queue, controller)

        service.start()
        try:
            action = TestAction(result_queue=result_queue)
            task_queue.put(action)
            result = result_queue.get(timeout=3)
            task_queue.join()
        finally:
            service.stop()

        self.assertTrue(result["ok"])
        self.assertEqual(result["action_type"], "send_text_message")
        self.assertGreater(result["finished_at"], time.time() - 10)
        self.assertEqual(controller.actions, [action])


if __name__ == "__main__":
    unittest.main()
