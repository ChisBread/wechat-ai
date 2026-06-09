"""Database-backed message polling service."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional


class MessageService:
    """Poll DatabaseService.check_new_messages and enqueue new DB messages."""

    def __init__(
        self,
        message_queue: Queue,
        db,
        *,
        poll_interval: float = 0.75,
    ):
        self.logger = logging.getLogger(__name__)
        self.message_queue = message_queue
        self.db = db
        self.poll_interval = poll_interval
        self.is_running = False
        self.is_paused = False
        self.thread: Optional[threading.Thread] = None
        self.callback: Optional[Callable] = None

    def start(self) -> bool:
        if self.is_running:
            self.logger.warning("消息监听器已经在运行")
            return False
        if not self.db or not getattr(self.db, "is_available", False):
            self.logger.warning("数据库服务不可用，消息监听器未启动")
            return False

        self.is_running = True
        self.thread = threading.Thread(
            target=self._message_loop,
            daemon=True,
            name="DatabaseMessageService",
        )
        self.thread.start()
        self.logger.info("数据库消息监听器已启动")
        return True

    def stop(self) -> bool:
        if not self.is_running:
            return False
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.logger.info("数据库消息监听器已停止")
        return True

    def set_callback(self, callback: Callable) -> None:
        self.callback = callback

    def pause(self) -> None:
        if not self.is_running or self.is_paused:
            return
        self.is_paused = True
        self.logger.info("数据库消息监听器已暂停")

    def resume(self) -> None:
        if not self.is_running or not self.is_paused:
            return
        self.is_paused = False
        self.logger.info("数据库消息监听器已恢复")

    def _message_loop(self) -> None:
        while self.is_running:
            if self.is_paused:
                time.sleep(1)
                continue
            try:
                messages = self.db.check_new_messages()
                if messages:
                    for msg in messages:
                        self.logger.info(
                            "新数据库消息入队: db=%s table=%s type=%s",
                            Path(msg[1][-1]).name,
                            msg[0],
                            msg[1][2] if len(msg[1]) > 2 else "",
                        )
                        self.message_queue.put(msg)
                    if self.callback:
                        self.callback(messages)
                time.sleep(self.poll_interval)
            except Empty:
                time.sleep(1)
            except Exception as exc:
                if self.is_running:
                    self.logger.error("数据库消息监听出错: %s", exc, exc_info=True)
                time.sleep(1)

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "queue_size": self.message_queue.qsize(),
            "poll_interval": self.poll_interval,
        }
