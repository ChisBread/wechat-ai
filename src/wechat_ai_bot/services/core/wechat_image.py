"""Image variants and presentation of decrypted WeChat WXGF containers."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from PIL import Image

from wechat_ai_bot.services.core.wechat_dat import WeChatDatError, WeChatDatDecryptResult


def image_variants(path: str | Path, *, thumbnail: bool = False) -> list[Path]:
    path = Path(path)
    if path.suffix.lower() != ".dat":
        return [path]
    stem = re.sub(r"_(?:h|t)$", "", path.stem)
    names = [f"{stem}_t.dat"] if thumbnail else [f"{stem}_h.dat", f"{stem}.dat", f"{stem}_t.dat"]
    return [path.with_name(name) for name in names]


def image_tier(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() != ".dat":
        return "file"
    if path.stem.endswith("_h"):
        return "original"
    if path.stem.endswith("_t"):
        return "thumbnail"
    return "standard"


def present_dat_image(result: WeChatDatDecryptResult) -> WeChatDatDecryptResult:
    """Return a PNG preview for WXGF; preserve other decrypted formats.

    WXGF is a container, not a raw HEVC file. This preview uses the first
    decoded frame; it does not promise to preserve animation or alpha tracks.
    """
    data = result.data
    if not data.startswith(b"wxgf"):
        return result
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise WeChatDatError("WXGF conversion requires ffmpeg")
    # HEVC VPS Annex-B start code. Do not feed the WXGF header to the decoder.
    match = re.search(rb"\x00\x00(?:\x00)?\x01\x40\x01", data)
    if not match:
        raise WeChatDatError("WXGF does not contain a supported HEVC stream")
    try:
        process = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-threads", "1",
             "-max_pixels", "40000000", "-f", "hevc", "-i", "pipe:0", "-frames:v", "1", "-threads", "1",
             "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            input=data[match.start():], capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WeChatDatError(f"WXGF conversion failed: {type(exc).__name__}") from exc
    if process.returncode or not process.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise WeChatDatError("WXGF HEVC decoding failed")
    try:
        with Image.open(io.BytesIO(process.stdout)) as image:
            image.verify()
    except Exception as exc:
        raise WeChatDatError("WXGF conversion produced an invalid image") from exc
    return replace(result, data=process.stdout, media_type="image/png", extension=".png", conversion="wxgf_first_frame_png")
