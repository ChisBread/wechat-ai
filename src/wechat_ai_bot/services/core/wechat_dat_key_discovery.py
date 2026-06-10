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
    WeChatDatInfo,
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
    text_candidate_count: int = 0
    raw_candidate_count: int = 0
    matched_method: str = ""
    scan_methods: list[str] = field(default_factory=list)
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
            "text_candidate_count": self.text_candidate_count,
            "raw_candidate_count": self.raw_candidate_count,
            "matched_method": self.matched_method,
            "scan_methods": self.scan_methods,
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
        raw_aligned_scan: bool = True,
    ):
        self.bot = bot
        self.xwechat_root = Path(xwechat_root)
        self.runtime_dir = Path(runtime_dir)
        self.process_names = set(process_names)
        self.max_region_size = max_region_size
        self.chunk_size = chunk_size
        self.raw_aligned_scan = raw_aligned_scan

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
            if send_probe:
                probe_path, probe_bytes = self.generate_probe_image()
            else:
                probe_path, probe_bytes = self._latest_existing_probe_image()
                if not probe_path:
                    probe_path, probe_bytes = self.generate_probe_image()
            result.probe_path = str(probe_path)
            result.probe_sha256 = hashlib.sha256(probe_bytes).hexdigest()

            before = 0.0 if not send_probe else time.time()
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

            aes_result = self._derive_aes_key(
                dat_path=dat_path,
                dat_bytes=dat_bytes,
                plain_bytes=probe_bytes,
                aes_size=info.aes_size,
                xor_key=result.xor_key,
            )
            if aes_result.xor_key is not None and result.xor_key is None:
                result.xor_key = aes_result.xor_key
            if not aes_result.key_value:
                scan_result = self._scan_aes_key(
                    dat_bytes=dat_bytes,
                    plain_bytes=probe_bytes,
                    aes_size=info.aes_size,
                    timeout_seconds=scan_timeout_seconds,
                )
                aes_result = _merge_aes_results(aes_result, scan_result)
            result.candidate_count = aes_result.candidate_count
            result.text_candidate_count = aes_result.text_candidate_count
            result.raw_candidate_count = aes_result.raw_candidate_count
            result.matched_method = aes_result.matched_method
            result.scan_methods = aes_result.scan_methods
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

    def _latest_existing_probe_image(self) -> tuple[Path | None, bytes]:
        candidates: list[tuple[float, Path]] = []
        for path in self.runtime_dir.glob("wechat_ai_dat_probe*.png"):
            try:
                if path.stat().st_size > 1024:
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        if not candidates:
            return None, b""
        path = sorted(candidates, reverse=True)[0][1]
        try:
            return path, path.read_bytes()
        except OSError:
            return None, b""

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

    def _derive_aes_key(
        self,
        *,
        dat_path: Path,
        dat_bytes: bytes,
        plain_bytes: bytes,
        aes_size: int,
        xor_key: int | None = None,
    ) -> "_AesScanResult":
        encrypted = dat_bytes[DAT_HEADER_SIZE : DAT_HEADER_SIZE + aes_size]
        expected_plain = plain_bytes[:aes_size]
        result = _AesScanResult(scan_methods=["kvcomm_derive"])
        account_name = _account_name_from_path(self.xwechat_root, dat_path)
        if not account_name:
            result.errors.append("cannot infer xwechat account directory from DAT path")
            return result
        for candidate in _iter_kvcomm_derived_keys(self.xwechat_root, account_name, xor_key=xor_key):
            result.candidate_count += 1
            result.text_candidate_count += 1
            if _aes_ecb_matches(candidate.key, encrypted, expected_plain):
                result.key = candidate.key
                result.key_value = candidate.config_value
                result.key_encoding = candidate.encoding
                result.matched_method = "kvcomm_derive"
                result.xor_key = candidate.xor_key
                return result
        return result

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
            raw_aligned_scan=self.raw_aligned_scan,
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
    text_candidate_count: int = 0
    raw_candidate_count: int = 0
    matched_method: str = ""
    scan_methods: list[str] = field(default_factory=list)
    scanned_pids: list[int] = field(default_factory=list)
    xor_key: int | None = None
    errors: list[str] = field(default_factory=list)


class _DatAesKeyScanner:
    def __init__(
        self,
        *,
        process_names: Iterable[str],
        max_region_size: int,
        chunk_size: int,
        raw_aligned_scan: bool,
    ):
        self.process_names = {name.lower() for name in process_names}
        self.max_region_size = max_region_size
        self.chunk_size = chunk_size
        self.raw_aligned_scan = raw_aligned_scan
        self.patterns = (
            re.compile(rb"(?<![a-z0-9])[a-z0-9]{32}(?![a-z0-9])"),
            re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16,64}(?![A-Za-z0-9])"),
            re.compile(rb"[A-Za-z0-9+/=_-]{22,88}"),
        )
        self.hex_run_pattern = re.compile(rb"[0-9a-fA-F]{16,}")

    def scan(self, *, encrypted: bytes, expected_plain: bytes, timeout_seconds: float) -> _AesScanResult:
        result = _AesScanResult()
        if not encrypted or not expected_plain or len(encrypted) % 16 != 0:
            result.errors.append("invalid AES scan sample")
            return result
        deadline = time.time() + max(1.0, float(timeout_seconds))
        seen_keys: set[bytes] = set()
        pids = self._wechat_pids()
        if not pids:
            result.errors.append("no WeChat process found")
            return result

        result = self._scan_method(
            method="text",
            pids=pids,
            encrypted=encrypted,
            expected_plain=expected_plain,
            deadline=deadline,
            result=result,
            seen_keys=seen_keys,
        )
        if result.key or not self.raw_aligned_scan or time.time() >= deadline:
            return result

        return self._scan_method(
            method="raw_aligned",
            pids=pids,
            encrypted=encrypted,
            expected_plain=expected_plain,
            deadline=deadline,
            result=result,
            seen_keys=seen_keys,
        )

    def _scan_method(
        self,
        *,
        method: str,
        pids: list[int],
        encrypted: bytes,
        expected_plain: bytes,
        deadline: float,
        result: _AesScanResult,
        seen_keys: set[bytes],
    ) -> _AesScanResult:
        result.scan_methods.append(method)
        seen_tokens: set[bytes] = set()
        for pid in pids:
            if time.time() >= deadline:
                break
            if pid not in result.scanned_pids:
                result.scanned_pids.append(pid)
            try:
                mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
            except OSError as exc:
                result.errors.append(f"pid {pid}: {type(exc).__name__}: {exc}")
                continue
            with mem:
                for region in self._readable_regions(pid, method=method):
                    if time.time() >= deadline:
                        break
                    start, end = region.start, region.end
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
                        data_start = pos - len(carry)
                        if method == "text":
                            candidates = self._iter_text_candidate_keys(data, seen_tokens)
                        else:
                            candidates = self._iter_raw_aligned_candidate_keys(data, data_start)
                        for candidate in candidates:
                            if candidate.key in seen_keys:
                                continue
                            seen_keys.add(candidate.key)
                            result.candidate_count += 1
                            if method == "text":
                                result.text_candidate_count += 1
                            else:
                                result.raw_candidate_count += 1
                            if self._matches(candidate.key, encrypted, expected_plain):
                                result.key = candidate.key
                                result.key_value = candidate.config_value
                                result.key_encoding = candidate.encoding
                                result.matched_method = method
                                return result
                        carry = data[-256:]
                        pos += len(chunk)
        return result

    def _candidate_keys(self, token: bytes) -> Iterable["_CandidateKey"]:
        token = token.strip(b"\x00\r\n\t '\",;:()[]{}<>")
        if len(token) >= 32 and re.fullmatch(rb"[A-Za-z0-9]+", token):
            prefix = token[:16]
            yield _CandidateKey(
                key=prefix,
                config_value=prefix.decode("ascii", errors="replace"),
                encoding="text-prefix16",
            )
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

    def _iter_text_candidate_keys(
        self,
        data: bytes,
        seen_tokens: set[bytes],
    ) -> Iterable["_CandidateKey"]:
        for match in self.hex_run_pattern.finditer(data):
            run = match.group(0)
            for offset in range(0, len(run) - 15):
                token = run[offset : offset + 16]
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                yield _CandidateKey(
                    key=token,
                    config_value=token.decode("ascii", errors="replace"),
                    encoding="hex-run-ascii16",
                )

        for pattern in self.patterns:
            for match in pattern.finditer(data):
                token = match.group(0)
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                yield from self._candidate_keys(token)

    def _iter_raw_aligned_candidate_keys(
        self,
        data: bytes,
        data_start: int,
    ) -> Iterable["_CandidateKey"]:
        offset = (16 - (data_start % 16)) % 16
        while offset + 16 <= len(data):
            key = data[offset : offset + 16]
            offset += 16
            if self._is_unlikely_raw_key(key):
                continue
            yield _CandidateKey(
                key=key,
                config_value=f"hex:{key.hex()}",
                encoding="raw-aligned16",
            )

    def _is_unlikely_raw_key(self, key: bytes) -> bool:
        if len(key) != 16:
            return True
        if key == b"\x00" * 16 or key == b"\xff" * 16:
            return True
        if len(set(key)) <= 2:
            return True
        return False

    def _matches(self, key: bytes, encrypted: bytes, expected_plain: bytes) -> bool:
        return _aes_ecb_matches(key, encrypted, expected_plain)

    def _wechat_pids(self) -> list[int]:
        pids: list[tuple[int, int]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(errors="ignore").strip()
                comm_lower = comm.lower()
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
                exe = ""
                try:
                    exe = (entry / "exe").resolve().as_posix()
                except OSError:
                    pass
                rss_pages = int((entry / "statm").read_text().split()[1])
            except (OSError, IndexError, ValueError):
                continue
            if self._is_wechat_process(comm_lower, cmdline, exe):
                pids.append((rss_pages, int(entry.name)))
        return [pid for _rss, pid in sorted(pids, reverse=True)]

    def _is_wechat_process(self, comm_lower: str, cmdline: bytes, exe: str) -> bool:
        if comm_lower in self.process_names:
            return True
        exe_name = Path(exe).name.lower() if exe else ""
        if exe_name.startswith(("python", "bash", "sh", "zsh", "node")):
            return False
        haystack = (exe + " ").encode(errors="ignore").lower() + cmdline.lower()
        return b"/opt/wechat" in haystack or b"wechatappex" in haystack

    def _readable_regions(self, pid: int, *, method: str) -> list["_MemoryRegion"]:
        try:
            lines = Path(f"/proc/{pid}/maps").read_text(errors="ignore").splitlines()
        except OSError:
            return []
        regions: list[_MemoryRegion] = []
        for line in lines:
            parts = line.split(maxsplit=5)
            if len(parts) < 2 or "r" not in parts[1]:
                continue
            path = parts[5] if len(parts) > 5 else ""
            if self._skip_mapping(path):
                continue
            if method == "raw_aligned" and not self._raw_scan_mapping(parts[1], path):
                continue
            start_hex, end_hex = parts[0].split("-", 1)
            start = int(start_hex, 16)
            end = int(end_hex, 16)
            size = end - start
            if size <= 0 or size > self.max_region_size:
                continue
            regions.append(_MemoryRegion(start=start, end=end, perms=parts[1], path=path))
        return regions

    def _raw_scan_mapping(self, perms: str, path: str) -> bool:
        if "w" in perms:
            return True
        if not path:
            return True
        path_lower = path.lower()
        return "wechat" in path_lower or "wcdb" in path_lower or "weixin" in path_lower

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


@dataclass(frozen=True)
class _MemoryRegion:
    start: int
    end: int
    perms: str
    path: str


@dataclass(frozen=True)
class DerivedDatKey:
    key: bytes
    config_value: str
    encoding: str
    xor_key: int
    account_name: str
    code: int


def derive_dat_key_for_file(
    file_path: str | Path,
    *,
    xwechat_root: str | Path = "/config/xwechat_files",
    info: WeChatDatInfo | None = None,
    xor_key: int | None = None,
) -> DerivedDatKey | None:
    dat_path = Path(file_path)
    root = Path(xwechat_root)
    if info is None:
        info = parse_dat_file(dat_path)
    if info is None:
        return None
    data = dat_path.read_bytes()
    inferred_xor = xor_key
    if inferred_xor is None or int(inferred_xor) < 0:
        inferred_xor = infer_xor_key(data, info)
    encrypted = data[DAT_HEADER_SIZE : DAT_HEADER_SIZE + info.aes_size]
    account_name = _account_name_from_path(root, dat_path)
    if not account_name:
        return None
    for candidate in _iter_kvcomm_derived_keys(root, account_name, xor_key=inferred_xor):
        if _dat_key_matches_image_magic(candidate.key, encrypted):
            return candidate
    return None


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


def _account_name_from_path(xwechat_root: Path, dat_path: Path) -> str:
    try:
        return dat_path.relative_to(xwechat_root).parts[0]
    except (ValueError, IndexError):
        parts = dat_path.parts
        try:
            idx = parts.index(xwechat_root.name)
        except ValueError:
            return ""
        if idx + 1 >= len(parts):
            return ""
        return parts[idx + 1]


def _account_candidates(account_name: str) -> list[str]:
    candidates = [account_name]
    match = re.match(r"^(.+)_([0-9a-fA-F]{4})$", account_name)
    if match:
        candidates.append(match.group(1))
    if account_name.startswith("RM"):
        candidates.append(account_name[2:])
    for value in list(candidates):
        if value.startswith("RM"):
            candidates.append(value[2:])
        lower = value.lower()
        if lower != value:
            candidates.append(lower)
    return list(dict.fromkeys(value for value in candidates if value))


def _collect_kvcomm_codes(xwechat_root: Path) -> list[int]:
    codes: set[int] = set()
    for kvcomm_dir in _kvcomm_dir_candidates(xwechat_root):
        if not kvcomm_dir.is_dir():
            continue
        for path in kvcomm_dir.glob("key_*_*.statistic"):
            match = re.match(r"^key_(\d+)_", path.name)
            if not match:
                continue
            try:
                code = int(match.group(1))
            except ValueError:
                continue
            if 0 < code <= 0xFFFFFFFF:
                codes.add(code)
    return sorted(codes)


def _iter_kvcomm_derived_keys(
    xwechat_root: Path,
    account_name: str,
    *,
    xor_key: int | None = None,
) -> Iterable[DerivedDatKey]:
    for code in _collect_kvcomm_codes(xwechat_root):
        derived_xor_key = int(code) & 0xFF
        if xor_key is not None and 0 <= int(xor_key) <= 255 and derived_xor_key != int(xor_key):
            continue
        for account in _account_candidates(account_name):
            key_text = hashlib.md5(f"{code}{account}".encode("utf-8")).hexdigest()[:16]
            yield DerivedDatKey(
                key=key_text.encode("ascii"),
                config_value=key_text,
                encoding="text",
                xor_key=derived_xor_key,
                account_name=account,
                code=int(code),
            )


def _kvcomm_dir_candidates(xwechat_root: Path) -> list[Path]:
    root = xwechat_root.resolve()
    config_root = root.parent
    candidates = [
        config_root / ".xwechat" / "net" / "kvcomm",
        config_root / "app_data" / "net" / "kvcomm",
        config_root / "xwechat" / "net" / "kvcomm",
    ]
    return list(dict.fromkeys(candidates))


def _merge_aes_results(first: _AesScanResult, second: _AesScanResult) -> _AesScanResult:
    if second.key_value:
        merged = second
    else:
        merged = first
    merged.candidate_count = first.candidate_count + second.candidate_count
    merged.text_candidate_count = first.text_candidate_count + second.text_candidate_count
    merged.raw_candidate_count = first.raw_candidate_count + second.raw_candidate_count
    merged.scan_methods = list(dict.fromkeys(first.scan_methods + second.scan_methods))
    merged.scanned_pids = list(dict.fromkeys(first.scanned_pids + second.scanned_pids))
    merged.errors = first.errors + second.errors
    if merged.xor_key is None:
        merged.xor_key = first.xor_key if first.xor_key is not None else second.xor_key
    return merged


def _aes_ecb_matches(key: bytes, encrypted: bytes, expected_plain: bytes) -> bool:
    if len(key) not in (16, 24, 32):
        return False
    if not encrypted or len(encrypted) % 16 != 0:
        return False
    if len(expected_plain) < len(encrypted):
        return False
    if len(encrypted) >= 16:
        try:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            first = decryptor.update(encrypted[:16]) + decryptor.finalize()
        except Exception:
            return False
        if first != expected_plain[:16]:
            return False
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(encrypted) + decryptor.finalize()
    except Exception:
        return False
    return plain == expected_plain[: len(encrypted)]


def _dat_key_matches_image_magic(key: bytes, encrypted: bytes) -> bool:
    if len(encrypted) < 16:
        return False
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(encrypted[:16]) + decryptor.finalize()
    except Exception:
        return False
    return (
        plain.startswith(b"\xff\xd8\xff")
        or plain.startswith(b"\x89PNG\r\n\x1a\n")
        or plain.startswith(b"GIF8")
        or plain.startswith(b"RIFF")
        or plain.startswith(b"wxgf")
    )


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[:1] + "***"
    return f"{value[:4]}***{value[-4:]}"
