"""Bounded decoding of images supplied by MCP callers."""
import base64
import binascii
import re
from pathlib import PurePosixPath

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


def decode_image_upload(image_base64: str, file_name: str | None = None) -> tuple[bytes, dict]:
    """Use the actual file signature, never a caller's MIME or filename extension."""
    if not isinstance(image_base64, str) or not image_base64.strip():
        raise ValueError("image_base64 is required and must not be empty")
    if len(image_base64) > MAX_IMAGE_BASE64_CHARS + 128:
        raise ValueError("image exceeds the 10 MiB decoded size limit")
    encoded = image_base64.strip()
    if encoded.lower().startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not re.fullmatch(r"data:image/[a-z0-9.+-]+;base64", header, re.I):
            raise ValueError("expected a base64 image data URI")
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        raise ValueError("image exceeds the 10 MiB decoded size limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 is not valid base64") from exc
    if not data:
        raise ValueError("decoded image must not be empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds the 10 MiB decoded size limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, extension = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime, extension = "image/jpeg", ".jpg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime, extension = "image/gif", ".gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime, extension = "image/webp", ".webp"
    else:
        raise ValueError("unsupported image format; expected PNG, JPEG, WebP or GIF")
    # The caller's name is metadata only; it never determines the on-disk path.
    name = PurePosixPath(str(file_name or "image").replace("\\", "/")).name
    stem = PurePosixPath(name).stem
    stem = "".join(c for c in stem if c.isprintable())[:100].strip(" .") or "image"
    return data, {"mime_type": mime, "bytes": len(data), "file_name": stem + extension}
