"""Helpers for Linux WeChat image DAT files."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


DAT_V1_SIGNATURE = b"\x07\x08V1\x08\x07"
DAT_V2_SIGNATURE = b"\x07\x08V2\x08\x07"
DAT_HEADER_SIZE = 15
DAT_XOR_PREFIX_SIZE = 16
IMAGE_MAGIC_TYPES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF8", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
    (b"BM", "image/bmp", ".bmp"),
    (b"II*\x00", "image/tiff", ".tif"),
    (b"wxgf", "video/hevc", ".hevc"),
)


class WeChatDatError(ValueError):
    """Raised when a WeChat DAT file cannot be parsed or decrypted."""


@dataclass(frozen=True)
class WeChatDatInfo:
    version: Literal["plain", "v1", "v2"]
    signature: bytes
    aes_size: int
    xor_size: int
    payload_size: int
    file_size: int
    encrypted_aes_size: int = 0
    raw_size: int = 0
    xor_prefix_size: int = DAT_XOR_PREFIX_SIZE

    @property
    def encrypted_payload_size(self) -> int:
        return self.encrypted_aes_size + self.raw_size + self.xor_size

    @property
    def expected_file_size(self) -> int:
        return DAT_HEADER_SIZE + self.encrypted_payload_size

    @property
    def decoded_size_without_aes(self) -> int:
        return self.raw_size + self.xor_size

    def to_dict(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "signature": self.signature.hex(),
            "aes_size": self.aes_size,
            "encrypted_aes_size": self.encrypted_aes_size,
            "raw_size": self.raw_size,
            "xor_size": self.xor_size,
            "payload_size": self.payload_size,
            "file_size": self.file_size,
            "xor_prefix_size": self.xor_prefix_size,
            "expected_file_size": self.expected_file_size,
        }


@dataclass(frozen=True)
class WeChatDatDecryptResult:
    data: bytes
    media_type: str
    extension: str
    info: WeChatDatInfo


def parse_dat_info(data: bytes) -> WeChatDatInfo | None:
    """Parse a Linux WeChat image DAT header, returning None for plain files."""
    if len(data) < DAT_HEADER_SIZE:
        return None
    signature = data[:6]
    if signature not in {DAT_V1_SIGNATURE, DAT_V2_SIGNATURE}:
        return None
    try:
        parsed_signature, aes_size, xor_size = struct.unpack("<6sLLx", data[:DAT_HEADER_SIZE])
    except struct.error as exc:
        raise WeChatDatError(f"invalid DAT header: {exc}") from exc
    if parsed_signature != signature:
        raise WeChatDatError("invalid DAT signature")
    encrypted_aes_size = aligned_aes_block_size(aes_size)
    min_size = DAT_HEADER_SIZE + encrypted_aes_size + xor_size
    if len(data) < min_size:
        raise WeChatDatError(
            f"truncated DAT file: expected at least {min_size} bytes, got {len(data)}"
        )
    raw_size = len(data) - min_size
    payload_size = aes_size + raw_size + xor_size
    version = "v1" if signature == DAT_V1_SIGNATURE else "v2"
    return WeChatDatInfo(
        version=version,
        signature=signature,
        aes_size=aes_size,
        xor_size=xor_size,
        payload_size=payload_size,
        file_size=len(data),
        encrypted_aes_size=encrypted_aes_size,
        raw_size=raw_size,
    )


def parse_dat_file(path: str | Path) -> WeChatDatInfo | None:
    file_path = Path(path)
    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        raise WeChatDatError(f"cannot stat DAT file: {exc}") from exc
    if file_size < DAT_HEADER_SIZE:
        return None
    with file_path.open("rb") as file:
        header = file.read(DAT_HEADER_SIZE)
    signature = header[:6]
    if signature not in {DAT_V1_SIGNATURE, DAT_V2_SIGNATURE}:
        return None
    try:
        parsed_signature, aes_size, xor_size = struct.unpack("<6sLLx", header)
    except struct.error as exc:
        raise WeChatDatError(f"invalid DAT header: {exc}") from exc
    if parsed_signature != signature:
        raise WeChatDatError("invalid DAT signature")
    encrypted_aes_size = aligned_aes_block_size(aes_size)
    min_size = DAT_HEADER_SIZE + encrypted_aes_size + xor_size
    if file_size < min_size:
        raise WeChatDatError(
            f"truncated DAT file: expected at least {min_size} bytes, got {file_size}"
        )
    raw_size = file_size - min_size
    payload_size = aes_size + raw_size + xor_size
    version = "v1" if signature == DAT_V1_SIGNATURE else "v2"
    return WeChatDatInfo(
        version=version,
        signature=signature,
        aes_size=aes_size,
        xor_size=xor_size,
        payload_size=payload_size,
        file_size=file_size,
        encrypted_aes_size=encrypted_aes_size,
        raw_size=raw_size,
    )


def infer_xor_key(data: bytes, info: WeChatDatInfo, media_type: str | None = None) -> int | None:
    """Infer the single-byte XOR key from the decoded image tail when possible."""
    if info.file_size < info.expected_file_size or info.xor_size <= 0:
        return None
    tail_size = min(DAT_XOR_PREFIX_SIZE, info.xor_size)
    tail = data[info.file_size - tail_size : info.file_size]
    if len(tail) != tail_size:
        return None

    # PNG IEND, JPEG EOI and GIF trailer give stable anchors for local inference.
    anchors: tuple[bytes, ...]
    if media_type == "image/png":
        anchors = (b"\x00\x00\x00\x00IEND\xaeB`\x82",)
    elif media_type == "image/jpeg":
        anchors = (b"\xff\xd9",)
    elif media_type == "image/gif":
        anchors = (b";",)
    elif media_type:
        return None
    else:
        anchors = (
            b"\x00\x00\x00\x00IEND\xaeB`\x82",
            b"\xff\xd9",
            b";",
        )
    for anchor in anchors:
        if len(anchor) > len(tail):
            continue
        offset = len(tail) - len(anchor)
        key = tail[offset] ^ anchor[0]
        if all((tail[offset + i] ^ key) == anchor[i] for i in range(len(anchor))):
            return key
    return None


def decrypt_dat_bytes(
    data: bytes,
    *,
    aes_key: bytes | str | None = None,
    xor_key: int | None = None,
) -> WeChatDatDecryptResult:
    """Decrypt a Linux WeChat image DAT file.

    Linux WeChat 4.x image DAT files use a 15-byte header, a PKCS7-padded
    AES-ECB prefix segment, an optional raw middle segment, then an XOR tail.
    The XOR key is often inferable from the image trailer, while the AES key
    still has to be supplied or discovered from WeChat runtime state.
    """
    info = parse_dat_info(data)
    if info is None:
        media_type, extension = guess_image_type(data)
        if not media_type:
            raise WeChatDatError("not a WeChat DAT image and not a supported image")
        return WeChatDatDecryptResult(data=data, media_type=media_type, extension=extension, info=_plain_info(len(data)))

    if info.version != "v2":
        raise WeChatDatError(f"unsupported DAT version: {info.version}")

    aes_key_bytes = normalize_aes_key(aes_key)
    if not aes_key_bytes:
        raise WeChatDatError("DAT AES key is unavailable")

    aes_start = DAT_HEADER_SIZE
    aes_end = aes_start + info.encrypted_aes_size
    raw_start = aes_end
    xor_start = info.file_size - info.xor_size
    raw_end = xor_start
    xor_end = info.file_size
    aes_data = data[aes_start:aes_end]
    raw_data = data[raw_start:raw_end]
    xor_data = data[xor_start:xor_end]
    if (
        len(aes_data) != info.encrypted_aes_size
        or len(raw_data) != info.raw_size
        or len(xor_data) != info.xor_size
    ):
        raise WeChatDatError("DAT segment is incomplete")
    if len(aes_data) % 16 != 0:
        raise WeChatDatError("DAT AES segment size is not block-aligned")

    prefix = _pkcs7_unpad(_aes_ecb_decrypt(aes_data, aes_key_bytes))
    if len(prefix) != info.aes_size:
        raise WeChatDatError("DAT AES plaintext size is invalid")
    decoded_without_suffix = prefix + raw_data
    media_type, extension = guess_image_type(decoded_without_suffix)
    if info.xor_size <= 0:
        xor_key = 0
    elif (xor_key is None or xor_key < 0) and media_type:
        xor_key = infer_xor_key(data, info, media_type=media_type)
    if xor_key is None:
        raise WeChatDatError("DAT XOR key is unavailable")
    if not 0 <= int(xor_key) <= 255:
        raise WeChatDatError("DAT XOR key must be a byte value")
    suffix = bytes(byte ^ int(xor_key) for byte in xor_data)
    decoded = decoded_without_suffix + suffix
    media_type, extension = guess_image_type(decoded)
    if not media_type:
        raise WeChatDatError("decrypted DAT payload is not a supported image")
    if not _is_complete_image_payload(decoded, media_type):
        raise WeChatDatError("decrypted DAT payload failed image integrity checks")
    return WeChatDatDecryptResult(
        data=decoded,
        media_type=media_type,
        extension=extension,
        info=info,
    )


def decrypt_dat_file(
    path: str | Path,
    *,
    aes_key: bytes | str | None = None,
    xor_key: int | None = None,
) -> WeChatDatDecryptResult:
    return decrypt_dat_bytes(Path(path).read_bytes(), aes_key=aes_key, xor_key=xor_key)


def normalize_aes_key(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        key = value
    else:
        text = str(value or "").strip()
        if not text:
            return b""
        if text.startswith(("hex:", "HEX:")):
            text = text.split(":", 1)[1]
            try:
                key = bytes.fromhex(text)
            except ValueError as exc:
                raise WeChatDatError("DAT AES key hex value is invalid") from exc
        else:
            key = text.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise WeChatDatError("DAT AES key must be 16, 24, or 32 bytes")
    return key


def aligned_aes_block_size(aes_size: int) -> int:
    if aes_size < 0:
        raise WeChatDatError("DAT AES size is invalid")
    remainder = aes_size % 16
    if remainder:
        return aes_size + (16 - remainder)
    return aes_size + 16


def guess_image_type(data: bytes) -> tuple[str, str]:
    for magic, media_type, extension in IMAGE_MAGIC_TYPES:
        if not data.startswith(magic):
            continue
        if media_type == "image/webp" and (len(data) < 12 or data[8:12] != b"WEBP"):
            continue
        return media_type, extension
    return "", ""


def _is_complete_image_payload(data: bytes, media_type: str) -> bool:
    stripped = data.rstrip(b"\0")
    if media_type == "image/png":
        return stripped.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    if media_type == "image/jpeg":
        return stripped.endswith(b"\xff\xd9")
    if media_type == "image/gif":
        return stripped.endswith(b";")
    if media_type == "image/webp":
        return len(stripped) >= 12 and stripped[8:12] == b"WEBP"
    return True


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16 != 0:
        raise WeChatDatError("DAT AES plaintext is not block-aligned")
    pad = data[-1]
    if pad < 1 or pad > 16:
        raise WeChatDatError("DAT AES padding is invalid")
    if data[-pad:] != bytes([pad]) * pad:
        raise WeChatDatError("DAT AES padding is invalid")
    return data[:-pad]


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _plain_info(file_size: int) -> WeChatDatInfo:
    return WeChatDatInfo(
        version="plain",
        signature=b"",
        aes_size=0,
        xor_size=file_size,
        payload_size=file_size,
        file_size=file_size,
        xor_prefix_size=0,
    )
