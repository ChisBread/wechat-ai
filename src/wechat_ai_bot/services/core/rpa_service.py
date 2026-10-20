"""
RPA 服务模块。
提供 RPA 相关的服务接口。
"""

import logging
import threading
import time
from collections import OrderedDict
from queue import Empty, Queue
from typing import TYPE_CHECKING, Optional

from wechat_ai_bot.rpa.rpa_action import RPAAction
from wechat_ai_bot.rpa.send_verifier import NO_RETRY, SEND_ACTIONS

if TYPE_CHECKING:
    from wechat_ai_bot.rpa.controller import RPAController


class RPAService:
    """
    RPA服务【任务消费者】。
    它作为一个后台服务，在独立的线程中运行。
    其唯一职责是从RPA任务队列中按顺序取出任务，并将其交给RPAController执行。
    """

    def __init__(self, rpa_task_queue: Queue, rpa_controller: "RPAController"):
        """
        【轻量级初始化】只接收并保存已创建好的依赖项。

        Args:
            rpa_task_queue (Queue[RPAAction]): 用于接收RPA任务的队列。
            rpa_controller (RPAController): 负责实际执行RPA操作的控制器实例。
        """
        self.logger = logging.getLogger(self.__class__.__name__)

        # --- 1. 保存注入的依赖 ---
        self.task_queue: Queue = rpa_task_queue
        self.controller: "RPAController" = rpa_controller

        # --- 2. 初始化自身状态 ---
        self.is_running: bool = False
        self.thread: Optional[threading.Thread] = None
        self._results = OrderedDict()
        self._results_lock = threading.Lock()

    def track_action(self, action):
        self._save_result(action.action_id, {
            "action_id": action.action_id, "status": "queued", "completed": False,
            "action_type": action.action_type.value, "retryable": False, "guidance": NO_RETRY,
        })

    def _save_result(self, action_id, result):
        with self._results_lock:
            self._results[action_id] = dict(result)
            self._results.move_to_end(action_id)
            while len(self._results) > 1000:
                self._results.popitem(last=False)

    def get_result(self, action_id):
        with self._results_lock:
            result = self._results.get(action_id)
            return dict(result) if result is not None else {
                "action_id": action_id, "status": "unknown", "completed": False,
                "retryable": False,
                "guidance": "Result missing or expired. Check recent messages before considering another send.",
            }

    def start(self):
        """
        启动RPA服务的后台线程。
        由Bot的生命周期管理器调用。
        """
        if self.is_running:
            self.logger.warning("RPAService is already running.")
            return

        self.logger.info("Starting RPAService...")
        self.is_running = True
        self.thread = threading.Thread(
            target=self._execute_loop, name="RPAWorkerThread"
        )
        self.thread.daemon = True
        self.thread.start()
        self.logger.info("RPAService has started.")

    def stop(self):
        """
        停止RPA服务的后台线程。
        由Bot的生命周期管理器调用。
        """
        if not self.is_running:
            self.logger.warning("RPAService is not running.")
            return

        self.logger.info("Stopping RPAService...")
        self.is_running = False
        if self.thread and self.thread.is_alive():
            # 等待线程自然结束，可以设置一个超时
            self.thread.join(timeout=5.0)
            if self.thread.is_alive():
                self.logger.warning("RPAWorkerThread did not terminate gracefully.")
        self.thread = None
        self.logger.info("RPAService has stopped.")

    def _execute_loop(self):
        """
        执行循环。这是运行在后台线程中的核心逻辑。
        """
        self.logger.info("RPA worker loop started.")
        while self.is_running:
            try:
                # 从队列中阻塞式地获取任务，设置超时以响应停止信号
                task = self.task_queue.get(block=True, timeout=1.0)

                if task:
                    self.logger.debug(f"Processing RPA action: {task.action_type.name}")
                    result = {
                        "ok": False, "action_id": task.action_id,
                        "action_type": task.action_type.value,
                        "retryable": False, "guidance": NO_RETRY,
                    }
                    self._save_result(task.action_id, {**result, "status": "running", "completed": False})
                    try:
                        result["ok"] = bool(self.controller.execute_action(task))
                        result.update(task.execution_result)
                        if task.action_type in SEND_ACTIONS and not result.get("verified"):
                            result["ok"] = False
                            result.setdefault("error", "Send was not verified in the database")
                        if not result["ok"]:
                            result.setdefault("error", "RPA execution failed")
                    except Exception:
                        self.logger.exception("RPA action failed")
                        result["ok"] = False
                        result["error"] = "RPA execution raised an exception"
                    finally:
                        try:
                            cleanup = getattr(task, "on_complete", None)
                            if cleanup is not None:
                                cleanup()
                        except Exception:
                            self.logger.exception("RPA action cleanup failed")
                        finally:
                            task.on_complete = None
                            result["finished_at"] = time.time()
                            result["status"] = "executed" if result["ok"] else "failed"
                            result["completed"] = True
                            self._save_result(task.action_id, result)
                            if not result["ok"]:
                                self.logger.error(
                                    "RPA action %s failed: %s. %s",
                                    task.action_id, result.get("error"), NO_RETRY,
                                )
                            try:
                                result_queue = getattr(task, "result_queue", None)
                                if result_queue is not None:
                                    result_queue.put_nowait(result)
                            finally:
                                self.task_queue.task_done()

            except Empty:
                # 队列为空是正常情况，继续循环以检查 is_running 状态
                continue
            except Exception as e:
                self.logger.error(
                    f"An unhandled exception occurred in RPA worker loop: {e}",
                    exc_info=True,
                )
                # 发生错误时等待一小段时间，避免CPU空转
                time.sleep(1)
        self.logger.info("RPA worker loop finished.")

    # 移除了 submit_task 方法，因为任务提交的职责现在完全由Bot类承担，
    # Bot会直接访问它持有的 rpa_task_queue 来提交任务。
    # 这确保了任务提交的入口是唯一的。

    # get_status 方法可以保留，用于监控
    def get_status(self) -> dict:
        """
        获取RPA服务的当前状态。
        """
        status = {
            "is_running": self.is_running,
            "queue_size": self.task_queue.qsize(),
        }
        # 如果RPAController有get_status方法，也可以在这里调用
        if hasattr(self.controller, "get_status"):
            status.update(self.controller.get_status())
        return status
