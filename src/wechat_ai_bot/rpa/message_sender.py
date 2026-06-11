"""
消息发送模块。
提供自动化消息发送、@用户、剪贴板图片处理等能力。
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import wechat_ai_bot.utils.mouse as pyautogui
from Levenshtein import ratio
from wechat_ai_bot.utils.helpers import (
    get_center_point,
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

    def send_message(self, message: str, clear_input_box: bool = True) -> bool:
        """
        发送文本消息。
        Args:
            message (str): 消息内容。
            clear_input_box (bool): 是否先清空输入框。
        Returns:
            bool: 是否发送成功。
        """
        try:
            if not self.window_manager.activate_input_box():
                return False
            if clear_input_box:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.3)
                pyautogui.press("delete")
                time.sleep(0.3)
            if not set_clipboard_text(message):
                return False
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.3)
            send_button = self.window_manager.get_icon_position("send_button")
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
        """
        @用户。
        Args:
            at_str (str): 用户名。
        Returns:
            bool: 是否成功。
        """
        if not at_str:
            return False
        mention_name = self._normalize_mention_name(at_str)
        if not mention_name:
            return False
        if not self.window_manager.activate_input_box():
            return False
        # xdotool expects the X11 keysym name. Passing "@" is ignored on some
        # layouts, which leaves plain text like "Bread_" in the input box.
        pyautogui.press("at")
        time.sleep(0.3)
        if not set_clipboard_text(mention_name):
            self.logger.error("设置剪贴板文本时出错")
            return False
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.5)
        if not self._click_mention_candidate(mention_name):
            self.logger.warning("未能确认 @ 候选: %s", mention_name)
            return False
        time.sleep(0.3)
        return True

    def _normalize_mention_name(self, at_str: str) -> str:
        name = str(at_str or "").strip().lstrip("@").strip()
        if not name:
            return ""
        parts = [part.strip() for part in name.split() if part.strip()]
        return max(parts, key=len) if parts else name

    def _click_mention_candidate(self, mention_name: str) -> bool:
        image_processor = getattr(self.window_manager, "image_processor", None)
        ocr_processor = getattr(self.window_manager, "ocr_processor", None)
        send_button = self.window_manager.get_icon_position("send_button")
        if not image_processor or not ocr_processor or not send_button:
            return False

        region = self._mention_candidate_region(send_button)
        if not region:
            return False
        try:
            screenshot = image_processor.take_screenshot(
                region=region,
                save_path="/config/runtime_images/mention_candidates.png",
            )
        except TypeError:
            screenshot = image_processor.take_screenshot(region=region)
        if screenshot is None:
            return False

        results = ocr_processor.process_image(image=screenshot)
        candidates = self._mention_candidates(
            mention_name,
            results,
            region_width=region[2],
            region_height=region[3],
        )
        if not candidates:
            return False

        best = candidates[0]
        self.logger.info(
            "Selecting @ candidate %r bbox=%s score=%.3f",
            best.get("label"),
            best.get("pixel_bbox"),
            best.get("mention_score", 0),
        )
        center_x, center_y = get_center_point(best["pixel_bbox"])
        pyautogui.click(region[0] + int(center_x), region[1] + int(center_y))
        return True

    def _mention_candidate_region(self, send_button: List[int]) -> Optional[List[int]]:
        try:
            _send_x, send_y = get_center_point(send_button)
            x = int(getattr(self.window_manager, "MSG_TOP_X", 0) or 0)
            y = max(int(getattr(self.window_manager, "MSG_TOP_Y", 0) or 0), int(send_y) - 520)
            width = int(getattr(self.window_manager, "MSG_WIDTH", 0) or 0)
            height = max(80, int(send_y) - y - 40)
            if width <= 0 or height <= 0:
                return None
            return [x, y, width, height]
        except Exception:
            return None

    def _mention_candidates(
        self,
        mention_name: str,
        results: List[Dict],
        region_width: int | None = None,
        region_height: int | None = None,
    ) -> List[Dict]:
        needle = mention_name.lower().replace(" ", "")
        candidates = []
        for result in results or []:
            label = str(result.get("label") or "")
            compact = label.lower().replace(" ", "")
            if not compact:
                continue
            if compact.startswith("@"):
                continue
            normalized = compact
            similarity = ratio(needle, normalized, score_cutoff=0.6)
            if needle not in normalized and normalized not in needle and similarity < 0.6:
                continue
            bbox = result.get("pixel_bbox", [0, 0, 0, 0])
            x_center = (float(bbox[0]) + float(bbox[2])) / 2 if len(bbox) >= 4 else 0
            y_center = (float(bbox[1]) + float(bbox[3])) / 2 if len(bbox) >= 4 else 0
            if region_width and x_center > float(region_width) * 0.65:
                continue
            if region_height and y_center < float(region_height) * 0.45:
                continue
            exact_bonus = 1.0 if normalized == needle else 0.0
            left_bonus = 0.1 if region_width and x_center < float(region_width) * 0.35 else 0
            lower_bonus = (y_center / float(region_height)) * 0.25 if region_height else 0
            item = dict(result)
            item["similarity"] = float(similarity)
            item["mention_score"] = float(similarity) + exact_bonus + left_bonus + lower_bonus
            candidates.append(item)
        candidates.sort(
            key=lambda item: (
                item.get("mention_score", 0),
                float(item.get("pixel_bbox", [0, 0, 0, 0])[1]),
            ),
            reverse=True,
        )
        return candidates

    def clear_input_box(self) -> bool:
        """
        清空输入框。
        Returns:
            bool: 是否成功。
        """
        self.window_manager.activate_input_box()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.3)
        pyautogui.press("backspace")
        time.sleep(0.3)
        return True
