"""Helpers for formatting WeChat message content stored in SQLCipher rows."""

from __future__ import annotations

from typing import Any


def message_content_text(value: Any, ct_flag: Any = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        text = ""
        if is_zstd_content(ct_flag) or is_zstd_payload(data):
            text = decompress_zstd_text(data)
        if not text:
            text = data.decode("utf-8", errors="replace").strip("\x00").strip()
        return text
    return str(value)


def decompress_zstd_text(data: bytes) -> str:
    try:
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompress(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def is_zstd_content(ct_flag: Any) -> bool:
    try:
        return int(ct_flag or 0) == 4
    except (TypeError, ValueError):
        return False


def is_zstd_payload(data: bytes) -> bool:
    return data.startswith(b"\x28\xb5\x2f\xfd")
