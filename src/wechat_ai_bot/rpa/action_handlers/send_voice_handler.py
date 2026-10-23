"""Send a native WeChat voice bubble using the desktop recording UI."""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Timer
import time

import cv2
import numpy as np

from wechat_ai_bot.rpa.action_handlers.base_handler import BaseActionHandler, RPAAction, RPAActionType
from wechat_ai_bot.rpa.voice_audio import PulseVoiceSource, voice_duration
from wechat_ai_bot.utils.helpers import set_clipboard_text, get_center_point
import wechat_ai_bot.utils.mouse as mouse


@dataclass
class SendVoiceAction(RPAAction):
    file_path: str = ""
    target: str = ""
    is_chatroom: bool = False
    recording_seconds: float = field(default=0, init=False)

    def __post_init__(self):
        self.action_type = RPAActionType.SEND_VOICE
        self.is_send_message = True


class VoiceControls:
    """Recognize the actual 4.1.13 toolbar controls before clicking anything.

    These small templates are cropped from WeChat at the project's 100% DPI.
    An unsupported theme/layout fails closed instead of using blind offsets.
    """

    def __init__(self, window_manager, image_processor):
        self.window_manager = window_manager
        self.image_processor = image_processor

    def locate(self, *names):
        geometry = self.window_manager.refresh_window_geometry()
        if not geometry or geometry["width"] < 600 or geometry["height"] < 400:
            raise RuntimeError("WeChat window geometry is unavailable")
        x = geometry["x"] + geometry["width"] - 320
        y = geometry["y"] + geometry["height"] - 70
        shot = self.image_processor.take_screenshot(region=[x, y, 320, 70])
        if shot is None:
            raise RuntimeError("Cannot inspect WeChat voice controls")
        pixels = cv2.cvtColor(np.asarray(shot.convert("RGB")), cv2.COLOR_RGB2BGR)
        points = []
        for name in names:
            template = cv2.imread(str(Path(__file__).parent.parent / "assets" / f"voice_{name}.png"))
            if template is None:
                raise RuntimeError("Voice control template is missing")
            scores = cv2.matchTemplate(pixels, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, position = cv2.minMaxLoc(scores)
            if score < 0.92:
                raise RuntimeError(f"WeChat voice {name} control not recognized; requires 4.1.13 light theme at 100% DPI")
            points.append((x + position[0] + template.shape[1] // 2,
                           y + position[1] + template.shape[0] // 2))
        return points


class SendVoiceHandler(BaseActionHandler):
    def _switch_session(self, action):
        if action.target_username != "filehelper" and action.target != "文件传输助手":
            return self.window_manager.switch_session(action.target)
        # In 4.1.13 File Transfer Assistant appears under "Functions", below
        # web suggestions. Pressing Enter opens web search, not this chat.
        mouse.hotkey("ctrl", "f")
        time.sleep(0.2)
        mouse.hotkey("ctrl", "a")
        if not set_clipboard_text("文件传输助手"):
            return False
        mouse.hotkey("ctrl", "v")
        geometry = self.window_manager.refresh_window_geometry()
        if not geometry:
            return False
        x, y = geometry["x"], geometry["y"] + 80
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.3)
            shot = self.image_processor.take_screenshot(region=[x, y, 440, min(800, geometry["height"] - 80)])
            results = self.ocr_processor.process_image(image=shot) if shot is not None else []
            sections = [r for r in results if str(r.get("label", "")).strip() == "功能"]
            if not sections:
                continue
            top = max(r["pixel_bbox"][3] for r in sections)
            matches = [r for r in results if "".join(str(r.get("label", "")).split()) == "文件传输助手"
                       and r["pixel_bbox"][1] > top]
            if len(matches) == 1:
                cx, cy = get_center_point(matches[0]["pixel_bbox"])
                mouse.click(x + cx, y + cy)
                time.sleep(0.3)
                return True
        raise RuntimeError("Could not locate File Transfer Assistant in WeChat's Functions search results")

    def execute(self, action):
        duration = voice_duration(action.file_path)
        if not action.target:
            raise ValueError("Voice recipient is required")
        controls = VoiceControls(self.window_manager, self.image_processor)
        if not self._switch_session(action):
            return False
        lock = Lock()
        recording = False
        expired = False
        cancel = None
        timer = None

        def abort():
            nonlocal recording, expired
            with lock:
                expired = True
                if recording and cancel is not None:
                    mouse.click(*cancel)
                    recording = False

        try:
            with PulseVoiceSource() as audio:
                try:
                    start, = controls.locate("start")
                    mouse.click(*start)
                    recording = True
                    started = time.monotonic()
                    time.sleep(0.2)
                    cancel, send = controls.locate("cancel", "send")
                    # Cancel before WeChat's automatic 60-second submission,
                    # even if a playback process or audio-server command hangs.
                    timer = Timer(max(0.1, 58 - (time.monotonic() - started)), abort)
                    timer.daemon = True
                    timer.start()
                    audio.bind_recording()
                    if time.monotonic() - started + duration > 56:
                        raise RuntimeError("Not enough recording time remains for the complete audio")
                    audio.play(action.file_path, duration)
                    time.sleep(0.15)
                    cancel, send = controls.locate("cancel", "send")
                    with lock:
                        if expired:
                            raise RuntimeError("Voice recording exceeded its deadline and was cancelled")
                        action.recording_seconds = time.monotonic() - started
                        if action.recording_seconds >= 58:
                            raise RuntimeError("Voice recording deadline exceeded")
                        mouse.click(*send)
                        recording = False
                    return True
                finally:
                    if timer:
                        timer.cancel()
                    with lock:
                        if recording:
                            if cancel is None:
                                # A missing recording toolbar is not permission
                                # to click the normal text Send button.
                                try:
                                    cancel, = controls.locate("cancel")
                                except RuntimeError:
                                    mouse.press("esc")
                            if cancel is not None:
                                mouse.click(*cancel)
                            recording = False
        finally:
            self._cleanup()
