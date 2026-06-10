"""Automatic discovery workflow for Linux WeChat image DAT keys."""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_ai_bot.services.core.wechat_dat import (
    DAT_HEADER_SIZE,
    infer_xor_key,
    parse_dat_file,
)


DEFAULT_FILEHELPER_NAME = "文件传输助手"
DEFAULT_PROCESS_NAMES = ("wechat", "WeChatAppEx", "wxutility", "wxocr", "wxplayer")


@dataclass
class DatKeyDiscoveryResult:
    status: str
    message: str = ""
    probe_path: str = ""
    probe_sha256: str = ""
    sent: bool = False
    send_result: dict[str, Any] | None = None
    dat_path: str = ""
    dat_info: dict[str, Any] = field(default_factory=dict)
    xor_key: int | None = None
    aes_key: str = ""
    aes_key_encoding: str = "text"
    persisted: bool = False
    candidate_count: int = 0
    scanned_pids: list[int] = field(default_factory=list)
    duration_seconds: float = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "probe_path": self.probe_path,
            "probe_sha256": self.probe_sha256,
            "sent": self.sent,
            "send_result": self.send_result,
            "dat_path": self.dat_path,
            "dat_info": self.dat_info,
            "xor_key": self.xor_key,
            "aes_key_found": bool(self.aes_key),
            "aes_key_preview": _mask_key(self.aes_key),
            "aes_key_encoding": self.aes_key_encoding,
            "persisted": self.persisted,
            "candidate_count": self.candidate_count,
            "scanned_pids": self.scanned_pids,
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": self.errors[-10:],
        }


class WeChatDatKeyDiscovery:
    """Send a known image to filehelper and try to discover DAT AES/XOR keys."""

    def __init__(
        self,
        bot: Any,
        *,
        xwechat_root: str | Path = "/config/xwechat_files",
        runtime_dir: str | Path = "/config/runtime_images",
        process_names: Iterable[str] = DEFAULT_PROCESS_NAMES,
        max_region_size: int = 256 * 1024 * 1024,
        chunk_size: int = 1024 * 1024,
    ):
        self.bot = bot
        self.xwechat_root = Path(xwechat_root)
        self.runtime_dir = Path(runtime_dir)
        self.process_names = set(process_names)
        self.max_region_size = max_region_size
        self.chunk_size = chunk_size

    def discover(
        self,
        *,
        send_probe: bool = True,
        target: str = DEFAULT_FILEHELPER_NAME,
        wait_seconds: float = 20,
        scan_timeout_seconds: float = 120,
    ) -> DatKeyDiscoveryResult:
        started = time.time()
        result = DatKeyDiscoveryResult(status="running")
        try:
            probe_path, probe_bytes = self.generate_probe_image()
            result.probe_path = str(probe_path)
            result.probe_sha256 = hashlib.sha256(probe_bytes).hexdigest()

            before = time.time()
            if send_probe:
                send_status = self._send_probe(probe_path, target=target, wait_seconds=wait_seconds)
                result.sent = True
                result.send_result = send_status
                if not bool(send_status.get("ok")):
                    result.status = "send_failed"
                    result.message = "probe image could not be sent"
                    return self._finish(result, started)
                before = float(send_status.get("finished_at") or before)

            dat_path = self._find_probe_dat(probe_path, since=before - 5)
            if dat_path is None:
                result.status = "dat_not_found"
                result.message = "probe DAT was not found under xwechat_files"
                return self._finish(result, started)

            result.dat_path = str(dat_path)
            info = parse_dat_file(dat_path)
            if not info:
                result.status = "dat_not_found"
                result.message = "matched probe path is not a WeChat DAT file"
                return self._finish(result, started)
            result.dat_info = info.to_dict()
            dat_bytes = dat_path.read_bytes()
            result.xor_key = infer_xor_key(dat_bytes, info)

            aes_result = self._scan_aes_key(
                dat_bytes=dat_bytes,
                plain_bytes=probe_bytes,
                aes_size=info.aes_size,
                timeout_seconds=scan_timeout_seconds,
            )
            result.candidate_count = aes_result.candidate_count
            result.scanned_pids = aes_result.scanned_pids
            result.errors.extend(aes_result.errors)
            if aes_result.key_value:
                result.aes_key = aes_result.key_value
                result.aes_key_encoding = aes_result.key_encoding
                if result.xor_key is not None:
                    self._apply_user_info_keys(result.aes_key, result.xor_key)
                    result.persisted = self._persist_config_keys(result.aes_key, result.xor_key)
                    result.status = "ok"
                    result.message = "DAT AES and XOR keys discovered"
                else:
                    result.status = "partial"
                    result.message = "DAT AES key discovered but XOR key is unavailable"
            else:
                if result.xor_key is not None:
                    self._apply_user_info_keys("", result.xor_key)
                result.status = "partial"
                result.message = "DAT XOR key discovered; AES key was not found"
            return self._finish(result, started)
        except Exception as exc:
            result.status = "error"
            result.message = f"{type(exc).__name__}: {exc}"
            result.errors.append(result.message)
            return self._finish(result, started)

    def generate_probe_image(self) -> tuple[Path, bytes]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        path = self.runtime_dir / f"wechat_ai_dat_probe_{timestamp}.png"
        png = _generate_probe_png(timestamp)
        path.write_bytes(png)
        return path, png

    def _send_probe(self, probe_path: Path, *, target: str, wait_seconds: float) -> dict[str, Any]:
        queue = getattr(self.bot, "rpa_task_queue", None)
        if queue is None:
            return {"ok": False, "error": "rpa queue unavailable"}
        from wechat_ai_bot.rpa.action_handlers import SendFileAction

        result_queue = Queue(maxsize=1)
        action = SendFileAction(
            file_path=str(probe_path),
            target=target,
            is_chatroom=False,
            result_queue=result_queue,
        )
        queue.put(action)
        try:
            result = result_queue.get(timeout=max(0.1, float(wait_seconds)))
        except Empty:
            return {"ok": False, "error": "send probe timed out"}
        return result

    def _find_probe_dat(self, probe_path: Path, *, since: float) -> Path | None:
        candidates: list[Path] = []
        cutoff = max(0.0, since)
        for path in self.xwechat_root.glob("*/msg/attach/*/*/Img/*_h.dat"):
            try:
                if path.stat().st_mtime >= cutoff:
                    candidates.append(path)
            except OSError:
                continue
        if not candidates:
            for path in self.xwechat_root.glob("*/msg/attach/*/*/Img/*.dat"):
                try:
                    if path.stat().st_mtime >= cutoff and not path.name.endswith("_t.dat"):
                        candidates.append(path)
                except OSError:
                    continue

        plain_len = probe_path.stat().st_size
        best: list[tuple[float, Path]] = []
        for path in candidates:
            try:
                info = parse_dat_file(path)
                if not info:
                    continue
                decoded_size = info.aes_size + info.xor_size
                if decoded_size != plain_len:
                    continue
                best.append((path.stat().st_mtime, path))
            except Exception:
                continue
        if not best:
            return None
        return sorted(best, reverse=True)[0][1]

    def _apply_user_info_keys(self, aes_key: str, xor_key: int) -> None:
        user_info = getattr(self.bot, "user_info", None)
        if not user_info:
            return
        if aes_key:
            user_info.dat_key = aes_key
        user_info.dat_xor_key = int(xor_key)

    def _persist_config_keys(self, aes_key: str, xor_key: int) -> bool:
        config = getattr(self.bot, "config", None)
        value = f"{aes_key},{int(xor_key)}"
        if hasattr(config, "set"):
            try:
                config.set("aes_xor_key", value)
                return True
            except Exception:
                return False
        raw_config = getattr(config, "config", None)
        if isinstance(raw_config, dict):
            raw_config["aes_xor_key"] = value
            return True
        if isinstance(config, dict):
            config["aes_xor_key"] = value
            return True
        return False

    def _scan_aes_key(
        self,
        *,
        dat_bytes: bytes,
        plain_bytes: bytes,
        aes_size: int,
        timeout_seconds: float,
    ) -> "_AesScanResult":
        scanner = _DatAesKeyScanner(
            process_names=self.process_names,
            max_region_size=self.max_region_size,
            chunk_size=self.chunk_size,
        )
        return scanner.scan(
            encrypted=dat_bytes[DAT_HEADER_SIZE : DAT_HEADER_SIZE + aes_size],
            expected_plain=plain_bytes[:aes_size],
            timeout_seconds=timeout_seconds,
        )

    def _finish(self, result: DatKeyDiscoveryResult, started: float) -> DatKeyDiscoveryResult:
        result.duration_seconds = time.time() - started
        return result


@dataclass
class _AesScanResult:
    key: bytes = b""
    key_value: str = ""
    key_encoding: str = ""
    candidate_count: int = 0
    scanned_pids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _DatAesKeyScanner:
    def __init__(
        self,
        *,
        process_names: Iterable[str],
        max_region_size: int,
        chunk_size: int,
    ):
        self.process_names = set(process_names)
        self.max_region_size = max_region_size
        self.chunk_size = chunk_size
        self.patterns = (
            re.compile(rb"(?<![a-z0-9])[a-z0-9]{32}(?![a-z0-9])"),
            re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16,64}(?![A-Za-z0-9])"),
            re.compile(rb"[A-Za-z0-9+/=_-]{22,88}"),
        )

    def scan(self, *, encrypted: bytes, expected_plain: bytes, timeout_seconds: float) -> _AesScanResult:
        result = _AesScanResult()
        if not encrypted or not expected_plain or len(encrypted) % 16 != 0:
            result.errors.append("invalid AES scan sample")
            return result
        deadline = time.time() + max(1.0, float(timeout_seconds))
        seen_tokens: set[bytes] = set()
        seen_keys: set[bytes] = set()
        for pid in self._wechat_pids():
            if time.time() >= deadline:
                break
            result.scanned_pids.append(pid)
            try:
                mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
            except OSError as exc:
                result.errors.append(f"pid {pid}: {type(exc).__name__}: {exc}")
                continue
            with mem:
                for start, end in self._readable_regions(pid):
                    if time.time() >= deadline:
                        break
                    carry = b""
                    pos = start
                    while pos < end and time.time() < deadline:
                        try:
                            mem.seek(pos)
                            chunk = mem.read(min(self.chunk_size, end - pos))
                        except OSError:
                            break
                        if not chunk:
                            break
                        data = carry + chunk
                        for pattern in self.patterns:
                            for match in pattern.finditer(data):
                                token = match.group(0)
                                if token in seen_tokens:
                                    continue
                                seen_tokens.add(token)
                                for candidate in self._candidate_keys(token):
                                    if candidate.key in seen_keys:
                                        continue
                                    seen_keys.add(candidate.key)
                                    result.candidate_count += 1
                                    if self._matches(candidate.key, encrypted, expected_plain):
                                        result.key = candidate.key
                                        result.key_value = candidate.config_value
                                        result.key_encoding = candidate.encoding
                                        return result
                        carry = data[-256:]
                        pos += len(chunk)
        return result

    def _candidate_keys(self, token: bytes) -> Iterable["_CandidateKey"]:
        token = token.strip(b"\x00\r\n\t '\",;:()[]{}<>")
        if len(token) in (16, 24, 32):
            yield _CandidateKey(
                key=token,
                config_value=token.decode("utf-8", errors="replace"),
                encoding="text",
            )
        if len(token) in (32, 48, 64) and re.fullmatch(rb"[0-9a-fA-F]+", token):
            try:
                raw = bytes.fromhex(token.decode("ascii"))
            except ValueError:
                raw = b""
            if len(raw) in (16, 24, 32):
                yield _CandidateKey(
                    key=raw,
                    config_value=f"hex:{raw.hex()}",
                    encoding="hex",
                )
        if 22 <= len(token) <= 88 and re.fullmatch(rb"[A-Za-z0-9+/=_-]+", token):
            for alt in (token, token.replace(b"-", b"+").replace(b"_", b"/")):
                padding = b"=" * ((4 - len(alt) % 4) % 4)
                try:
                    raw = base64.b64decode(alt + padding, validate=False)
                except Exception:
                    continue
                if len(raw) in (16, 24, 32):
                    yield _CandidateKey(
                        key=raw,
                        config_value=f"hex:{raw.hex()}",
                        encoding="base64",
                    )

    def _matches(self, key: bytes, encrypted: bytes, expected_plain: bytes) -> bool:
        try:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            plain = decryptor.update(encrypted) + decryptor.finalize()
        except Exception:
            return False
        return plain == expected_plain

    def _wechat_pids(self) -> list[int]:
        pids: list[tuple[int, int]] = []
        for entry in Path("/proc").iterdir():
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


@dataclass(frozen=True)
class _CandidateKey:
    key: bytes
    config_value: str
    encoding: str


def _generate_probe_png(seed: int) -> bytes:
    width, height = 384, 216
    seed_bytes = str(seed).encode("ascii")
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(
                (
                    (x * 3 + y + seed_bytes[0]) % 256,
                    (x + y * 2 + seed_bytes[-1]) % 256,
                    (x * y + len(seed_bytes) * 17) % 256,
                )
            )
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"tEXt", b"wechat-ai-dat-probe\x00" + seed_bytes)
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[:1] + "***"
    return f"{value[:4]}***{value[-4:]}"
