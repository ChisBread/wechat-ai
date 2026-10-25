"""
消息发送模块。
提供自动化消息发送、@用户、剪贴板图片处理等能力。
"""

import logging
import time
from typing import Any, Dict, List, Optional

import wechat_ai_bot.utils.mouse as pyautogui
from Levenshtein import ratio
from PIL import ImageChops
from wechat_ai_bot.rpa.editor import enabled_send_button
from wechat_ai_bot.utils.helpers import (
    get_center_point,
    get_clipboard_text,
    read_temp_image,
    save_clipboard_image_to_temp,
    set_clipboard_text,
)


class MessageSender:
    """
    消息发送器。
    支持文本消息、@用户、剪贴板图片等自动化发送。
    """

    def __init__(self, window_manager: Any):
        """
        初始化 MessageSender。
        Args:
            window_manager (WindowManager): 窗口管理器。
        """
        self.logger = logging.getLogger(__name__)
        self.ocr_processor = None
        self.temp_image_path = None
        self.window_manager = window_manager

    def send_message(
        self,
        message: str,
        clear_input_box: bool = True,
        activate_input_box: bool = True,
    ) -> bool:
        """
        发送文本消息。
        Args:
            message (str): 消息内容。
            clear_input_box (bool): 是否先清空输入框。
            activate_input_box (bool): 是否先激活输入框。
        Returns:
            bool: 是否已执行发送点击；实际发送成功由 RPAController 回查数据库确认。
        """
        try:
            if activate_input_box and not self.window_manager.activate_input_box():
                return False
            if clear_input_box:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.3)
                pyautogui.press("delete")
                time.sleep(0.3)
            prefix = "" if clear_input_box else self._read_input_text()
            if prefix is None:
                self.logger.warning("Cannot read the existing draft; send aborted")
                return False
            if not set_clipboard_text(message):
                return False
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.3)
            actual = self._read_input_text()
            expected = prefix + message
            if actual is None or actual.replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
                self.logger.warning("Editor did not contain the intended text; send aborted")
                self.clear_input_box()
                return False
            send_button = enabled_send_button(self.window_manager)
            if send_button:
                center = get_center_point(send_button)
                pyautogui.click(center[0], center[1])
                time.sleep(0.3)
                return True
            return False
        except Exception as e:
            self.logger.error(f"发送消息时出错: {str(e)}")
            return False

    def _calc_similarity(
        self, search_text: str, formatted_results: List[Dict], score_cutoff: float = 0.6
    ) -> List[Dict]:
        """
        计算文本相似度。
        Args:
            search_text (str): 查询文本。
            formatted_results (List[Dict]): OCR 结果。
            score_cutoff (float): 相似度阈值。
        Returns:
            List[Dict]: 匹配结果。
        """
        for result in formatted_results:
            result["similarity"] = ratio(
                search_text,
                result["label"],
                processor=lambda x: x.lower().replace(" ", ""),
                score_cutoff=score_cutoff,
            )
        return [
            result
            for result in formatted_results
            if result["similarity"] >= float(score_cutoff)
        ]

    def read_temp_image(self, image_path: str) -> bool:
        """
        读取临时图片。
        Args:
            image_path (str): 图片路径。
        Returns:
            bool: 是否成功。
        """
        return read_temp_image(image_path)

    def save_clipboard_image_to_temp(self) -> Optional[str]:
        """
        保存剪贴板图片到临时文件。
        Returns:
            Optional[str]: 文件路径。
        """
        return save_clipboard_image_to_temp()

    def mention_user(self, at_str: str) -> bool:
        """Click a unique exact candidate and verify the inserted mention before sending."""
        mention_name = self._normalize_mention_name(at_str)
        if not mention_name:
            return False
        selected = False
        try:
            # Start with an empty draft, so copied text can verify the whole mention.
            if not self.clear_input_box():
                return False
            send_button = self.window_manager.get_icon_position("send_button")
            region = self._mention_candidate_region(send_button)
            if not region:
                return False
            before = self.window_manager.image_processor.take_screenshot(region=region)
            if before is None:
                return False

            # xdotool expects the X11 keysym, not a literal "@".
            pyautogui.press("at")
            time.sleep(0.3)
            if not set_clipboard_text(mention_name):
                return False
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            if not self._click_mention_candidate(mention_name, region, before):
                self.logger.warning("未找到唯一且精确匹配的 @ 候选: %s", mention_name)
                return False
            time.sleep(0.5)
            selected = self._input_contains_mention(mention_name)
            if not selected:
                self.logger.warning("选中的 @ 与目标不符或无法确认: %s", mention_name)
            return selected
        except Exception as exc:
            self.logger.warning("选择 @ 候选失败: %s", exc)
            return False
        finally:
            if not selected:
                # A failed click/search must not leave an unsafe draft for a later send.
                try:
                    # Escape hides Linux WeChat when no popup remains. Focus
                    # and clear the editor instead of hiding the main window.
                    self.clear_input_box()
                except Exception as exc:
                    self.logger.warning("清理未确认的 @ 草稿失败: %s", exc)

    def _read_input_text(self) -> Optional[str]:
        """Read the focused editor without leaving the draft selected."""
        if not set_clipboard_text(""):
            return None
        pyautogui.hotkey("ctrl", "a")
        try:
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.3)
            return get_clipboard_text()
        finally:
            pyautogui.press("Right")

    def _input_contains_mention(self, mention_name: str) -> bool:
        # Compare the complete text, not an OCR substring (@Yang contains @y).
        text = self._read_input_text()
        if text is None:
            return False
        # Linux WeChat copies mention chips with a leading U+2005 separator.
        text = text.removeprefix("\u2005")
        prefix = f"@{mention_name}"
        if not text.startswith(prefix):
            return False
        suffix = text[len(prefix):]
        # WeChat appends a separator when inserting a real mention. The search
        # text @y alone is insufficient proof that the candidate was selected.
        return bool(suffix) and all(char in " \u2005" for char in suffix)

    def _normalize_mention_name(self, at_str: str) -> str:
        # Spaces and case are part of the name, not fuzzy-search hints.
        return str(at_str or "").strip().lstrip("@").strip()

    def _click_mention_candidate(self, mention_name: str, region: List[int], before: Any) -> bool:
        candidate = self._find_mention_candidate(mention_name, region, before)
        if not candidate:
            return False
        self.logger.info(
            "Clicking exact @ candidate %r bbox=%s region=%s",
            candidate["label"], candidate["pixel_bbox"], region,
        )
        center_x, center_y = get_center_point(candidate["pixel_bbox"])
        pyautogui.click(region[0] + int(center_x), region[1] + int(center_y))
        return True

    def _find_mention_candidate(
        self, mention_name: str, region: List[int], before: Any,
    ) -> Optional[Dict]:
        image_processor = self.window_manager.image_processor
        ocr_processor = self.window_manager.ocr_processor
        screenshot = image_processor.take_screenshot(
            region=region,
            save_path="/config/runtime_images/mention_candidates.png",
        )
        if screenshot is None or screenshot.size != before.size:
            return None
        results = ocr_processor.process_image(image=screenshot)
        candidates = self._mention_candidates(
            mention_name, results, region_width=region[2], region_height=region[3],
        )
        candidates = [item for item in candidates if self._candidate_changed(
            before, screenshot, item["pixel_bbox"],
        )]
        if not candidates:
            # The detector misses tiny one-character labels in a large capture.
            # Isolate the changed popup before enlarging it; enlarging the whole
            # screen gets downscaled again by the OCR detector.
            changed = ImageChops.difference(before.convert("RGB"), screenshot.convert("RGB"))
            box = changed.convert("L").point(lambda value: 255 if value > 8 else 0).getbbox()
            if box and 20 <= box[2] - box[0] <= 480 and 20 <= box[3] - box[1] <= 520:
                crop = screenshot.crop(box)
                zoomed = crop.resize((crop.width * 3, crop.height * 3))
                zoom_results = ocr_processor.process_image(image=zoomed)
                translated = []
                for item in zoom_results or []:
                    bbox = item.get("pixel_bbox")
                    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                        translated.append({**item, "pixel_bbox": [
                            box[0] + bbox[0] / 3, box[1] + bbox[1] / 3,
                            box[0] + bbox[2] / 3, box[1] + bbox[3] / 3,
                        ]})
                candidates = self._mention_candidates(
                    mention_name, translated, region_width=region[2], region_height=region[3],
                )
        # The capture is bounded above the editor. Unchanged history in that
        # capture is not a popup row, even if its text exactly matches the name.
        candidates = [item for item in candidates if self._candidate_changed(
            before, screenshot, item["pixel_bbox"],
        )]
        if len(candidates) != 1:
            return None
        return candidates[0]

    @staticmethod
    def _candidate_changed(before: Any, after: Any, bbox: List[int]) -> bool:
        box = tuple(int(value) for value in bbox)
        difference = ImageChops.difference(
            before.crop(box).convert("RGB"), after.crop(box).convert("RGB"),
        ).convert("L")
        histogram = difference.histogram()
        # Ignore tiny rendering noise; require a visible change inside the label.
        changed = sum(histogram[24:])
        return changed >= max(2, difference.width * difference.height * 0.02)

    def _mention_candidate_region(self, send_button: List[int]) -> Optional[List[int]]:
        try:
            _send_x, send_y = get_center_point(send_button)
            chat_x = int(self.window_manager.MSG_TOP_X)
            # The popup can extend left of the chat pane (including short names).
            x = max(0, chat_x - 240)
            top = int(self.window_manager.MSG_TOP_Y)
            width = int(self.window_manager.MSG_WIDTH) + chat_x - x
            # MSG_HEIGHT ends at the editor's top, regardless of editor height.
            bottom = top + int(self.window_manager.MSG_HEIGHT)
            if width <= 0 or not top < bottom < send_y:
                return None
            y = max(top, bottom - 520)
            return [x, y, width, bottom - y]
        except (AttributeError, TypeError, ValueError):
            return None

    def _mention_candidates(
        self,
        mention_name: str,
        results: List[Dict],
        region_width: int,
        region_height: int,
    ) -> List[Dict]:
        candidates = []
        for result in results or []:
            if str(result.get("label") or "").strip() != mention_name:
                continue
            bbox = result.get("pixel_bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            try:
                x1, y1, x2, y2 = map(float, bbox)
            except (TypeError, ValueError):
                continue
            if not (0 <= x1 < x2 <= region_width and 0 <= y1 < y2 <= region_height):
                continue
            candidates.append(result)
        return candidates

    def clear_input_box(self) -> bool:
        """
        清空输入框。
        Returns:
            bool: 是否成功。
        """
        if not self.window_manager.activate_input_box():
            return False
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.3)
        pyautogui.press("backspace")
        time.sleep(0.3)
        return True
