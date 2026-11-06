"""Image variants and presentation of decrypted WeChat WXGF containers."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageStat

from wechat_ai_bot.services.core.wechat_dat import WeChatDatError, WeChatDatDecryptResult

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WXGF_MAX_FRAMES = 3
_WXGF_BLANK_STDDEV = 1.0


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


def _png_frame_bounds(png_stream: bytes) -> list[tuple[int, int]]:
    """Return (start, end) offsets of every complete PNG image in a stream."""
    bounds: list[tuple[int, int]] = []
    position = 0
    while True:
        start = png_stream.find(PNG_SIGNATURE, position)
        if start < 0:
            break
        cursor = start + len(PNG_SIGNATURE)
        end = None
        while cursor + 8 <= len(png_stream):
            length = int.from_bytes(png_stream[cursor:cursor + 4], "big")
            chunk_type = png_stream[cursor + 4:cursor + 8]
            cursor += 8 + length + 4
            if chunk_type == b"IEND":
                if cursor <= len(png_stream):
                    end = cursor
                break
        if end is None:
            break
        bounds.append((start, end))
        position = end
    return bounds


def _frame_stddev(png_bytes: bytes) -> float:
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            return float(ImageStat.Stat(image.convert("L")).stddev[0] or 0.0)
    except Exception:
        return -1.0


def _pick_png_frame(png_stream: bytes) -> bytes:
    """Pick the most detailed frame from a WXGF decode.

    Some WXGF images start with a uniform placeholder frame followed by the
    real picture, so a plain first-frame decode returns a blank image.
    """
    bounds = _png_frame_bounds(png_stream)
    if not bounds:
        raise WeChatDatError("WXGF conversion produced an invalid image")
    frames = [png_stream[start:end] for start, end in bounds]
    if len(frames) == 1:
        return frames[0]
    scores = [_frame_stddev(frame) for frame in frames]
    best = max(range(len(frames)), key=lambda index: scores[index])
    if scores[best] <= _WXGF_BLANK_STDDEV:
        return frames[0]
    return frames[best]


def present_dat_image(result: WeChatDatDecryptResult) -> WeChatDatDecryptResult:
    """Return a PNG preview for WXGF; preserve other decrypted formats.

    WXGF is a container, not a raw HEVC file. This preview decodes a few
    frames and keeps the most detailed one, because some images carry a blank
    placeholder frame before the real picture; animation and alpha tracks are
    still not preserved.
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
             "-max_pixels", "40000000", "-f", "hevc", "-i", "pipe:0", "-frames:v", str(_WXGF_MAX_FRAMES),
             "-threads", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            input=data[match.start():], capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WeChatDatError(f"WXGF conversion failed: {type(exc).__name__}") from exc
    if process.returncode or not process.stdout.startswith(PNG_SIGNATURE):
        raise WeChatDatError("WXGF HEVC decoding failed")
    frame = _pick_png_frame(process.stdout)
    try:
        with Image.open(io.BytesIO(frame)) as image:
            image.verify()
    except Exception as exc:
        raise WeChatDatError("WXGF conversion produced an invalid image") from exc
    return replace(result, data=frame, media_type="image/png", extension=".png", conversion="wxgf_first_frame_png")
