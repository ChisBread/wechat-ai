"""Mouse utilities backed by X11 xdotool."""

import math
import os
import random
import re
import subprocess
import time

_KEY_ALIASES = {
    "backspace": "BackSpace",
    "ctrl": "ctrl",
    "control": "ctrl",
    "delete": "Delete",
    "del": "Delete",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "return": "Return",
    "space": "space",
    "tab": "Tab",
}


class ScreenSize:
    def __init__(self, w=1024, h=768):
        self.width = w
        self.height = h

def _xdotool(*args):
    return subprocess.run(
        ["xdotool"] + list(args),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )


def _normalize_key(key):
    value = str(key)
    return _KEY_ALIASES.get(value.lower(), value)

def _run_display_command(cmd):
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":1")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env)

def position():
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, timeout=3)
    x = int(re.search(r"x:(\d+)", r.stdout).group(1))
    y = int(re.search(r"y:(\d+)", r.stdout).group(1))
    return (x, y)

def size():
    try:
        result = _run_display_command(["xdotool", "getdisplaygeometry"])
        parts = result.stdout.strip().split()
        if result.returncode == 0 and len(parts) == 2:
            return ScreenSize(int(parts[0]), int(parts[1]))
    except Exception:
        pass

    try:
        result = _run_display_command(["xdpyinfo"])
        match = re.search(r"dimensions:\s+(\d+)x(\d+)\s+pixels", result.stdout)
        if result.returncode == 0 and match:
            return ScreenSize(int(match.group(1)), int(match.group(2)))
    except Exception:
        pass

    return ScreenSize(1024, 768)

def moveTo(x, y, duration=0.1, tween=None):
    _xdotool("mousemove", str(x), str(y))
    time.sleep(duration * 0.5)

def click(x=None, y=None, button="left", clicks=1):
    if x is not None: moveTo(x, y)
    btn = {"left": "1", "right": "3", "middle": "2"}.get(button, "1")
    for _ in range(clicks): _xdotool("click", btn); time.sleep(0.05)

def mouseDown(b="left"):
    _xdotool("mousedown", {"left":"1","right":"3"}.get(b,"1"))

def mouseUp(b="left"):
    _xdotool("mouseup", {"left":"1","right":"3"}.get(b,"1"))

def scroll(clicks, x=None, y=None):
    if x is not None: moveTo(x, y)
    d = "4" if clicks > 0 else "5"
    for _ in range(abs(clicks)): _xdotool("click", d); time.sleep(0.02)

def press(k): _xdotool("key", _normalize_key(k))
def hotkey(*keys):
    combo = "+".join(_normalize_key(k) for k in keys)
    _xdotool("key", combo)

def typewrite(text, interval=0.02):
    _xdotool("type", "--delay", str(int(interval*1000)), "--", text)

def human_like_mouse_move(tx=None, ty=None, speed_range=(700,1200), min_duration=0.1, max_duration=1.0, **kw):
    tx = kw.get("target_x", tx)
    ty = kw.get("target_y", ty)
    if tx is None or ty is None:
        return
    cx, cy = position()
    d = math.sqrt((tx-cx)**2 + (ty-cy)**2)
    if d == 0: return
    s = random.uniform(*speed_range); dur = max(min_duration, min(max_duration, d/s * random.uniform(0.75, 1.25)))
    steps = max(5, int(dur/0.02))
    for i in range(1, steps+1):
        frac = i/steps; eased = 1 - (1-frac)**3
        x = int(cx + (tx-cx)*eased); y = int(cy + (ty-cy)*eased)
        _xdotool("mousemove", str(x), str(y)); time.sleep(dur/steps)
    time.sleep(0.05)
