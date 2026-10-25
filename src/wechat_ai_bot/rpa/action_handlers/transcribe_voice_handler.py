"""Ask the desktop client to transcribe voice, verifying the exact DB message."""

from dataclasses import dataclass
from pathlib import Path
import re
import time

import wechat_ai_bot.utils.mouse as mouse
from .base_handler import BaseActionHandler, RPAAction, RPAActionType
from wechat_ai_bot.weixin.message_content import message_content_text
from wechat_ai_bot.weixin.parser.audio_parser import audio_transcript


@dataclass
class TranscribeVoiceAction(RPAAction):
    target: str = ""
    server_id: str = ""
    message_db_path: str = ""
    timeout: float = 20.0

    def __post_init__(self):
        self.action_type = RPAActionType.TRANSCRIBE_VOICE
        self.is_send_message = False


class TranscribeVoiceHandler(BaseActionHandler):
    def _read(self, action):
        row = self.controller.db.get_message_by_server_id(
            action.server_id, Path(action.message_db_path), action.target_username,
            fresh=True,
        )
        if not row or row[2] != 34:
            raise ValueError("Voice message not found in the specified conversation")
        return audio_transcript(message_content_text(row[12], row[15]), row[14])

    def _verified(self, action):
        text = self._read(action)
        if not text:
            return False
        action.execution_result.update(
            verified=True, voice_to_text=text, server_id=str(action.server_id),
        )
        return True

    def _title_matches(self, target):
        geometry = self.window_manager.refresh_window_geometry()
        if not geometry:
            return False
        region = self.window_manager.get_message_region()
        shot = self.image_processor.take_screenshot(
            region=[region[0], geometry["y"] + 30, region[2] - 120, 45],
        )
        if shot is None:
            return False
        normalize = lambda s: re.sub(r"\s+", "", str(s))
        # Chatroom titles can include the member count.
        return any(
            normalize(re.sub(r"[（(]\d+[)）]$", "", str(item.get("label", "")))) == normalize(target)
            for item in self.ocr_processor.process_image(image=shot)
        )

    def _incoming_bubbles(self, region):
        shot = self.image_processor.take_screenshot(region=region)
        if shot is None:
            return []
        return sorted(
            [item["pixel_bbox"] for item in self.image_processor.detect_objects(shot)
             if item.get("label") == "voice" and item.get("confidence", 0) >= 0.65
             and item["pixel_bbox"][0] < region[2] * 0.4],
            key=lambda box: box[1], reverse=True,
        )

    def _click_transcribe_menu(self):
        self._menu_visible = False
        # Menus can open above the pointer near the screen edge. Linux 4.1.13
        # calls this item 语音转文字, while the inline hint says 转文字.
        x, y = mouse.position()
        screen = mouse.size()
        left, top = max(0, int(x) - 220), max(0, int(y) - 380)
        shot = self.image_processor.take_screenshot(
            region=[left, top, min(600, screen.width - left), min(800, screen.height - top)],
        )
        if shot is None:
            return False
        items = self.ocr_processor.process_image(image=shot)
        # Bound diagnostic output: the screenshot may include surrounding chat.
        labels = [str(item.get("label", ""))[:80] for item in items[:30]]
        entries = [(self._menu_label(item), item.get("pixel_bbox")) for item in items]
        if not any(box and self._label_matches(label, expected)
                   for label, box in entries for expected in ("收藏", "多选", "删除")):
            self.logger.warning("Voice menu not identified; OCR labels=%r", labels)
            return False
        self._menu_visible = True
        # Prefer the actual menu label over a simultaneously visible inline
        # 转文字 hint behind the popup. Clicking the hint only dismisses it.
        anchors = [box for label, box in entries
                   if box and self._label_matches(label, "收藏")]
        for expected in ("语音转文字", "转文字"):
            for label, box in entries:
                if not box or not self._label_matches(label, expected):
                    continue
                x1, y1, x2, y2 = box
                if not any(abs(x1 - anchor[0]) < 24 and 0 < anchor[1] - y1 < 100
                           for anchor in anchors):
                    continue
                mouse.click(left + int((x1 + x2) / 2), top + int((y1 + y2) / 2))
                return True
        self.logger.warning("Voice transcription menu item not matched/aligned; OCR labels=%r", labels)
        return False

    @staticmethod
    def _menu_label(item):
        # OCR can merge the menu icon with its text, e.g. 图语音转文字 / 图收藏.
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(item.get("label", "")))

    @staticmethod
    def _label_matches(normalized, expected):
        # Require the full label, tolerating at most two leading icon characters.
        return normalized.endswith(expected) and len(normalized) <= len(expected) + 2

    def _flush_client_transcript(self, action):
        # Linux WeChat 4.1.13 can display a transcript without persisting it.
        # Leaving and returning to the conversation commits audioTxt. Switch to
        # the logged-in user's own chat; this sends no messages.
        db = self.controller.db
        own = db.get_contact_by_username(db.user_info.account)
        if not own or not own.display_name or own.username == action.target_username:
            raise RuntimeError("Cannot resolve own chat to persist WeChat transcription")
        if not self.window_manager.switch_session(own.display_name):
            raise RuntimeError("Cannot leave conversation to persist transcription")
        time.sleep(0.4)
        if not self.window_manager.switch_session(action.target) or not self._title_matches(action.target):
            raise RuntimeError("Cannot return to the transcription conversation")
        time.sleep(0.4)

    def execute(self, action):
        touched_ui = False
        action.execution_result.update(verified=False, triggered=False)
        try:
            if not all((action.target, action.target_username, action.server_id, action.message_db_path)):
                raise ValueError("target, target_username, server_id and message_db_path are required")
            if self._verified(action):
                return True
            touched_ui = True
            if not self.window_manager.switch_session(action.target) or not self._title_matches(action.target):
                raise RuntimeError("Cannot verify the selected WeChat conversation")
            region = self.window_manager.get_message_region()
            mouse.moveTo(region[0] + region[2] // 2, region[1] + region[3] // 2)
            mouse.scroll(-80)
            time.sleep(0.4)
            deadline = time.monotonic() + max(1, min(float(action.timeout), 30))
            # Transcription may reflow the entire chat. Re-detect after each menu
            # operation, visiting the newest incoming bubbles first. Never infer
            # the transcript/message association from OCR or bubble coordinates.
            for index in range(8):
                if self._verified(action):
                    return True
                if time.monotonic() >= deadline:
                    break
                boxes = self._incoming_bubbles(region)
                if index >= len(boxes):
                    break
                x1, y1, x2, y2 = boxes[index]
                mouse.click(region[0] + int((x1 + x2) / 2),
                            region[1] + int((y1 + y2) / 2), button="right")
                time.sleep(0.3)
                # Require the complete menu label and its position above 收藏.
                self._menu_visible = False
                clicked = self._click_transcribe_menu()
                if clicked:
                    action.execution_result["triggered"] = True
                    time.sleep(2)
                    self._flush_client_transcript(action)
                elif self._menu_visible:
                    mouse.press("esc")
                else:
                    raise RuntimeError("Voice context menu could not be identified")
            last_flush = time.monotonic()
            while time.monotonic() < deadline:
                if self._verified(action):
                    return True
                if time.monotonic() - last_flush >= 5:
                    self._flush_client_transcript(action)
                    last_flush = time.monotonic()
                time.sleep(0.5)
            raise RuntimeError("WeChat transcript unavailable before timeout; message may be outside the visible recent history")
        except Exception as exc:
            action.execution_result["error"] = str(exc)
            self.logger.warning("Voice transcription failed for %s (triggered=%s): %s",
                                action.server_id, action.execution_result["triggered"], exc)
            return False
        finally:
            if touched_ui:
                self._cleanup()
