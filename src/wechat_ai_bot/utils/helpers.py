"""
Utility helper functions — Linux-compatible version.
Adapted from omni-bot-sdk, Windows-specific functions replaced with Linux equivalents.
"""

import hashlib
import logging
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


def get_center_point(
    bbox: List[int], offset: Tuple[int, int] = (0, 0)
) -> Tuple[int, int]:
    """Get the center point of a bounding box with optional offset."""
    x1, y1, x2, y2 = bbox
    center_x = (x1 + x2) // 2 + offset[0]
    center_y = (y1 + y2) // 2 + offset[1]
    return center_x, center_y


def ensure_dir_exists(dir_path: str):
    """Ensure a directory exists, creating it if necessary."""
    Path(dir_path).mkdir(parents=True, exist_ok=True)


def random_sleep(min_seconds: float = 0.1, max_seconds: float = 0.5):
    """Sleep for a random duration to simulate human-like delays."""
    time.sleep(random.uniform(min_seconds, max_seconds))


# ---- Clipboard (Linux) ----

def set_clipboard_text(text: str) -> bool:
    """Set clipboard text content using xclip/pyperclip."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception as e:
        logger.warning(f"pyperclip failed: {e}, trying xclip...")
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=text.encode("utf-8"),
                check=False,
            )
            return proc.returncode == 0
        except FileNotFoundError:
            logger.error("Neither pyperclip nor xclip available for clipboard")
            return False


def get_clipboard_text() -> str:
    """Get clipboard text content."""
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True, check=False,
            )
            return result.stdout
        except FileNotFoundError:
            return ""


def save_clipboard_image_to_temp(filename: str = None) -> Optional[str]:
    """Save clipboard image to a temp file. Returns file path or None."""
    if filename is None:
        filename = f"clipboard_{int(time.time())}.png"
    filepath = os.path.join(tempfile.gettempdir(), filename)
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            stdout=open(filepath, "wb"), check=False,
        )
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return filepath
    except FileNotFoundError:
        pass
    return None


def copy_file_to_clipboard(file_path: str) -> bool:
    """Copy a file to clipboard as URI list (for file paste operations)."""
    file_uri = Path(file_path).absolute().as_uri()
    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i"],
            input=file_uri.encode("utf-8"), check=False,
        )
        return True
    except FileNotFoundError:
        return False


# ---- File / Image Utilities ----

def read_temp_image(image_path: str) -> Optional[bytes]:
    """Read a temporary image file and return its bytes."""
    try:
        with open(image_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Failed to read temp image {image_path}: {e}")
        return None


def generate_random_filename(prefix: str = "file", ext: str = "png") -> str:
    """Generate a random filename."""
    rand = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    return f"{prefix}_{rand}.{ext}"


# ---- DingTalk Notifications ----

def send_dingtalk_notification(webhook_url: str, title: str, text: str) -> bool:
    """Send a DingTalk notification via webhook."""
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"## {title}\n\n{text}",
            },
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"DingTalk notification failed: {e}")
        return False


# ---- WeChat Path Detection (Linux) ----

def get_wechat_install_path() -> Optional[str]:
    """Detect WeChat install path on Linux."""
    # Check common locations
    paths_to_check = [
        "/usr/bin/wechat",
        "/opt/wechat/wechat",
        "/usr/local/bin/wechat",
    ]
    for p in paths_to_check:
        if os.path.exists(p):
            return p
    # Try which
    try:
        result = subprocess.run(["which", "wechat"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def launch_wechat():
    """Launch WeChat as a background process."""
    wechat_path = get_wechat_install_path()
    if wechat_path:
        subprocess.Popen([wechat_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Launched WeChat from {wechat_path}")
    else:
        logger.error("Cannot find WeChat executable")


# ---- YAML helpers ----

def load_yaml(file_path: str) -> dict:
    """Load a YAML file."""
    yaml = YAML()
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.load(f)


def save_yaml(file_path: str, data: dict):
    """Save data to a YAML file."""
    yaml = YAML()
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


def download_file_if_url(path_or_url: str, dest_dir: str = "/tmp") -> str:
    """Download a file if it's a URL, otherwise return the path as-is."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        import os
        filename = os.path.basename(urlparse(path_or_url).path) or "download"
        dest = os.path.join(dest_dir, filename)
        try:
            resp = requests.get(path_or_url, timeout=60, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return dest
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return path_or_url
    return path_or_url
