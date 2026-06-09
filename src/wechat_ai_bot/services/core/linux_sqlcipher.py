"""Python SQLCipher helpers for Linux WeChat databases."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SQLCIPHER_PAGE_SIZE = 4096
SQLCIPHER_RESERVED_SIZE = 80
SQLCIPHER_HMAC_SIZE = 64
SQLCIPHER_KDF_ITER = 2
SQLITE_HEADER = b"SQLite format 3\x00"
HEX_KEY_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


def sqlcipher_driver_name() -> str:
    """Return the DB-API compatible Python SQLCipher module name."""
    for name in ("sqlcipher3", "pysqlcipher3"):
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, AttributeError, ValueError):
            continue
    return ""


def import_sqlcipher_driver() -> Any:
    """Import the configured Python SQLCipher driver."""
    driver = sqlcipher_driver_name()
    if not driver:
        raise RuntimeError(
            "Python SQLCipher driver is missing; install sqlcipher3-binary"
        )
    return importlib.import_module(driver)


def verify_sqlcipher_page_hmac(
    page1: bytes,
    encrypted_key: bytes,
    *,
    page_no: int = 1,
) -> bool:
    """Validate a SQLCipher v4 page HMAC for a candidate raw encryption key."""
    if len(page1) < SQLCIPHER_PAGE_SIZE or len(encrypted_key) != 32:
        return False

    salt = page1[:16]
    mac_salt = bytes(byte ^ 0x3A for byte in salt)
    mac_key = hashlib.pbkdf2_hmac(
        "sha512",
        encrypted_key,
        mac_salt,
        SQLCIPHER_KDF_ITER,
        dklen=32,
    )
    hmac_data = page1[16 : SQLCIPHER_PAGE_SIZE - SQLCIPHER_RESERVED_SIZE + 16]
    stored_hmac = page1[SQLCIPHER_PAGE_SIZE - SQLCIPHER_HMAC_SIZE : SQLCIPHER_PAGE_SIZE]

    for pack_format in ("<I", ">I"):
        digest = hmac.new(mac_key, hmac_data, hashlib.sha512)
        digest.update(struct.pack(pack_format, page_no))
        if hmac.compare_digest(digest.digest(), stored_hmac):
            return True
    return False


def database_salt(db_path: Path) -> bytes:
    with db_path.open("rb") as file:
        return file.read(16)


def database_page1(db_path: Path) -> bytes:
    with db_path.open("rb") as file:
        return file.read(SQLCIPHER_PAGE_SIZE)


@dataclass
class SqlCipherKeyScanResult:
    """Redacted key scan result; raw keys stay in found_keys only."""

    found_keys: dict[Path, str] = field(default_factory=dict)
    scanned_pids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    candidate_count: int = 0
    duration_seconds: float = 0

    def redacted(self) -> dict[str, Any]:
        return {
            "found_key_count": len(self.found_keys),
            "scanned_pids": self.scanned_pids[:20],
            "candidate_count": self.candidate_count,
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": self.errors[-5:],
        }


class SqlCipherKeyScanner:
    """Scan WeChat process memory for SQLCipher raw key strings."""

    def __init__(
        self,
        *,
        process_names: Iterable[str] | None = None,
        chunk_size: int = 1024 * 1024,
        max_region_size: int = 256 * 1024 * 1024,
    ):
        self.logger = logging.getLogger(__name__)
        self.process_names = set(
            process_names
            or ("wechat", "WeChatAppEx", "wxutility", "wxocr", "wxplayer")
        )
        self.chunk_size = chunk_size
        self.max_region_size = max_region_size

    def scan(self, database_paths: Iterable[Path]) -> SqlCipherKeyScanResult:
        """Return PRAGMA-ready raw SQLCipher keys for the requested DB paths."""
        started = time.time()
        result = SqlCipherKeyScanResult()
        pages_by_salt: dict[bytes, list[tuple[Path, bytes]]] = {}
        target_count = 0
        for path in sorted({Path(path) for path in database_paths}):
            try:
                page = database_page1(path)
            except OSError as exc:
                result.errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if page.startswith(SQLITE_HEADER) or len(page) < SQLCIPHER_PAGE_SIZE:
                continue
            pages_by_salt.setdefault(page[:16], []).append((path, page))
            target_count += 1

        if not pages_by_salt:
            result.duration_seconds = time.time() - started
            return result

        found_by_salt: dict[bytes, str] = {}
        seen_candidates: set[bytes] = set()

        for pid in self._wechat_pids():
            result.scanned_pids.append(pid)
            try:
                mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
            except OSError as exc:
                result.errors.append(f"pid {pid}: {type(exc).__name__}: {exc}")
                continue
            with mem:
                for start, end in self._readable_regions(pid):
                    carry = b""
                    pos = start
                    while pos < end:
                        try:
                            mem.seek(pos)
                            chunk = mem.read(min(self.chunk_size, end - pos))
                        except OSError:
                            break
                        if not chunk:
                            break
                        data = carry + chunk
                        for match in HEX_KEY_RE.finditer(data):
                            candidate_hex = match.group(1).lower()
                            if candidate_hex in seen_candidates:
                                continue
                            seen_candidates.add(candidate_hex)
                            result.candidate_count += 1
                            if len(candidate_hex) < 64:
                                continue
                            try:
                                encrypted_key = bytes.fromhex(
                                    candidate_hex[:64].decode("ascii")
                                )
                            except ValueError:
                                continue
                            self._match_candidate(
                                encrypted_key,
                                pages_by_salt,
                                found_by_salt,
                            )
                            if len(result.found_keys) >= target_count:
                                result.duration_seconds = time.time() - started
                                return result
                        carry = data[-256:]
                        for salt, raw_key in found_by_salt.items():
                            for path, _page in pages_by_salt.get(salt, []):
                                result.found_keys[path] = raw_key
                        if len(result.found_keys) >= target_count:
                            result.duration_seconds = time.time() - started
                            return result
                        pos += len(chunk)

        result.duration_seconds = time.time() - started
        return result

    def _match_candidate(
        self,
        encrypted_key: bytes,
        pages_by_salt: dict[bytes, list[tuple[Path, bytes]]],
        found_by_salt: dict[bytes, str],
    ) -> None:
        for salt, db_pages in pages_by_salt.items():
            if salt in found_by_salt:
                continue
            if any(verify_sqlcipher_page_hmac(page, encrypted_key) for _path, page in db_pages):
                found_by_salt[salt] = encrypted_key.hex() + salt.hex()

    def _wechat_pids(self) -> list[int]:
        pids: list[tuple[int, int]] = []
        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(errors="ignore").strip()
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
                rss_pages = int((entry / "statm").read_text().split()[1])
            except (OSError, IndexError, ValueError):
                continue
            if comm in self.process_names or b"wechat" in cmdline.lower():
                pids.append((rss_pages, int(entry.name)))
        return [pid for _rss, pid in sorted(pids, reverse=True)]

    def _readable_regions(self, pid: int) -> list[tuple[int, int]]:
        try:
            lines = Path(f"/proc/{pid}/maps").read_text(errors="ignore").splitlines()
        except OSError:
            return []
        regions: list[tuple[int, int]] = []
        for line in lines:
            parts = line.split(maxsplit=5)
            if len(parts) < 2 or "r" not in parts[1]:
                continue
            path = parts[5] if len(parts) > 5 else ""
            if self._skip_mapping(path):
                continue
            start_hex, end_hex = parts[0].split("-", 1)
            start = int(start_hex, 16)
            end = int(end_hex, 16)
            size = end - start
            if size <= 0 or size > self.max_region_size:
                continue
            regions.append((start, end))
        return regions

    def _skip_mapping(self, path: str) -> bool:
        if not path:
            return False
        return path.startswith(
            (
                "/usr/lib",
                "/lib",
                "/opt/venv-bot",
                "/app",
                "/usr/share",
                "/usr/local",
            )
        )
