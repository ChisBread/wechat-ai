"""
Mouse utilities — pure xdotool, no pyautogui dependency.
"""
import math, random, re, subprocess, time

class ScreenSize:
    def __init__(self, w=1024, h=768): self.width = w; self.height = h

def _xdotool(*args):
    subprocess.run(["xdotool"] + list(args), capture_output=True, timeout=5)

def position():
    r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True, timeout=3)
    x = int(re.search(r"x:(\d+)", r.stdout).group(1))
    y = int(re.search(r"y:(\d+)", r.stdout).group(1))
    return (x, y)

def size(): return ScreenSize(1024, 768)

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

def press(k): _xdotool("key", str(k))
def hotkey(*keys):
    combo = "+".join(str(k).lower() for k in keys)
    _xdotool("key", combo)

def typewrite(text, interval=0.02):
    _xdotool("type", "--delay", str(int(interval*1000)), "--", text)

def human_like_mouse_move(tx, ty, speed_range=(700,1200), min_duration=0.1, max_duration=1.0, **kw):
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
