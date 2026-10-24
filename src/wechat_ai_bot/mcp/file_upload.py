"""Bounded decoding of files supplied by MCP callers."""

import base64
import binascii
import re
from pathlib import PurePosixPath

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_BASE64_CHARS = 4 * ((MAX_FILE_BYTES + 2) // 3)


def decode_file_upload(file_base64: str, file_name: str | None = None) -> tuple[bytes, dict]:
    """Decode an upload and return a safe basename for the staged file."""
    if not isinstance(file_base64, str) or not file_base64.strip():
        raise ValueError("file_base64 is required and must not be empty")
    if len(file_base64) > MAX_FILE_BASE64_CHARS + 128:
        raise ValueError("file exceeds the 10 MiB decoded size limit")
    encoded = file_base64.strip()
    if encoded.lower().startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not re.fullmatch(
            r"data:(?:[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+)?"
            r"(?:;[a-z0-9!#$&^_.+-]+=[a-z0-9!#$&^_.+-]+)*;base64", header, re.I
        ):
            raise ValueError("expected a base64 file data URI")
    if len(encoded) > MAX_FILE_BASE64_CHARS:
        raise ValueError("file exceeds the 10 MiB decoded size limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("file_base64 is not valid base64") from exc
    if not data:
        raise ValueError("decoded file must not be empty")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 10 MiB decoded size limit")
    name = PurePosixPath(str(file_name or "file.bin").replace("\\", "/")).name
    name = "".join(c for c in name if c.isprintable()).strip(" .") or "file.bin"
    if len(name.encode("utf-8")) > 255:
        raise ValueError("file_name must not exceed 255 UTF-8 bytes")
    return data, {"bytes": len(data), "file_name": name}
