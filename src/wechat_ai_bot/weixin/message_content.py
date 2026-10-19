"""Helpers for formatting WeChat message content stored in SQLCipher rows."""

from __future__ import annotations

from typing import Any


def chat_message_text(value: Any, ct_flag: Any = None, *, chat_username: str = "", sender_username: str = "") -> str:
    """Decode content and remove only an exact group sender envelope."""
    text = message_content_text(value, ct_flag)
    if chat_username.endswith("@chatroom") and sender_username:
        for prefix in (f"{sender_username}:\r\n", f"{sender_username}:\n"):
            if text.startswith(prefix):
                return text[len(prefix):]
    return text


def contact_in_chat(database: Any, sender: Any, chat_username: str) -> Any:
    """Use room-scoped contact metadata when the database supports it."""
    lookup = getattr(database, "get_contact_in_room", None)
    if sender and chat_username.endswith("@chatroom") and callable(lookup):
        return lookup(chat_username, sender.username) or sender
    return sender


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
