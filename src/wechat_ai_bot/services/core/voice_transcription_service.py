"""Defer incoming voice delivery until the serialized RPA transcription finishes."""

import logging
from collections import OrderedDict
from threading import Lock

from wechat_ai_bot.rpa.action_handlers.transcribe_voice_handler import TranscribeVoiceAction


class VoiceTranscriptionService:
    def __init__(self, task_queue, *, enabled=False, timeout=20):
        self.task_queue = task_queue
        self.enabled = enabled
        self.timeout = max(1, min(float(timeout), 30))
        self.logger = logging.getLogger(__name__)
        self._lock = Lock()
        self._pending = set()
        self._completed = OrderedDict()
        self._retried = 0
        self._failed = 0

    def submit(self, message, deliver):
        """Return True when delivery is deferred or this message is a duplicate."""
        if (not self.enabled or getattr(message, "local_type", None) != 34
                or message.is_self or message.audio_text):
            return False
        contact = message.room if message.is_chatroom else message.contact
        if not contact or not message.server_id or not message.message_db_path:
            return False
        key = (message.user_info.account, message.message_db_path,
               contact.username, str(message.server_id))
        with self._lock:
            if key in self._pending or key in self._completed:
                return True
            if len(self._pending) >= 64:
                self.logger.warning("Voice transcription queue is full; delivering original message")
                return False
            self._pending.add(key)

        def make_action(attempt):
            action = TranscribeVoiceAction(
                target=contact.display_name, target_username=contact.username,
                server_id=str(message.server_id), message_db_path=message.message_db_path,
                timeout=self.timeout,
            )
            handled = False

            def finish():
                nonlocal handled
                with self._lock:
                    if handled or key not in self._pending:
                        return
                    handled = True
                result = action.execution_result
                text = result.get("voice_to_text") or ""
                success = bool(result.get("verified") and text.strip())
                if not success and attempt == 0:
                    self.logger.warning("Retrying voice transcription for %s after: %s",
                                        message.server_id, result.get("error", "RPA unavailable"))
                    try:
                        # Keep the key pending and use a fresh action/callback.
                        # Queue at the tail so other RPA work can run first.
                        self.task_queue.put_nowait(make_action(1))
                    except Exception:
                        self.logger.exception("Could not enqueue voice transcription retry")
                    else:
                        with self._lock:
                            self._retried += 1
                        return
                with self._lock:
                    self._pending.remove(key)
                    self._completed[key] = None
                    if not success:
                        self._failed += 1
                    while len(self._completed) > 1024:
                        self._completed.popitem(last=False)
                if success:
                    message.audio_text = text
                else:
                    self.logger.warning("Voice transcription incomplete for %s: %s",
                                        message.server_id, result.get("error", "RPA unavailable"))
                # Deliver once after success or the final failure.
                deliver(message)

            action.on_complete = finish
            return action

        action = make_action(0)
        try:
            self.task_queue.put_nowait(action)
        except Exception:
            with self._lock:
                self._pending.discard(key)
            self.logger.exception("Could not enqueue voice transcription")
            return False
        return True

    def get_status(self):
        with self._lock:
            return {"enabled": self.enabled, "pending": len(self._pending),
                    "completed": len(self._completed), "retried": self._retried,
                    "failed": self._failed}
