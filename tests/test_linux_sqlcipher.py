import hashlib
import hmac
import io
import struct
from pathlib import Path
from unittest.mock import patch

from wechat_ai_bot.services.core.linux_sqlcipher import (
    SqlCipherKeyScanner,
    SqlCipherKeyScanResult,
    decode_obfuscated_sqlcipher_key,
)


def _fixture():
    key = bytes(range(32))
    salt = bytes(range(16, 32))
    page = bytearray(4096)
    page[:16] = salt
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in salt), 2, 32)
    page[-64:] = hmac.new(mac_key, page[16:4032] + struct.pack('<I', 1), hashlib.sha512).digest()
    literal = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
    pad = bytes(range(32, 64))
    blob = bytes(x ^ pad[i % 32] for i, x in enumerate(literal))
    return key, salt, bytes(page), blob


def test_obfuscated_key_uses_database_salt_without_version_specific_pad():
    key, salt, _, blob = _fixture()
    assert decode_obfuscated_sqlcipher_key(blob, salt) == key
    assert decode_obfuscated_sqlcipher_key(blob[:-1], salt) is None
    assert decode_obfuscated_sqlcipher_key(blob, salt[:-1]) is None
    assert decode_obfuscated_sqlcipher_key(b'!' * 99, salt) is None


def _scan_buffer(page, blob):
    _, salt, _, _ = _fixture()
    memory = bytearray(1024)
    # Pointer/length pair crosses the scanner chunk boundary.
    memory[56:72] = struct.pack('<QQ', 512, 99)
    memory[512:611] = blob
    scanner = SqlCipherKeyScanner(chunk_size=64)
    found = {}
    result = SqlCipherKeyScanResult()
    with patch.object(scanner, '_readable_regions', return_value=[(0, 1024)]):
        scanner._scan_obfuscated_keys(io.BytesIO(memory), 123, {salt: [(Path('test.db'), page)]}, found, result)
    return found, result


def test_obfuscated_key_scan_handles_chunk_boundaries_and_validates_hmac():
    key, salt, page, blob = _fixture()
    found, result = _scan_buffer(page, blob)
    assert found == {salt: key.hex() + salt.hex()}
    assert result.candidate_count >= 1
    assert key.hex() not in str(result.redacted())


def test_obfuscated_key_scan_rejects_candidate_with_bad_page_hmac():
    _, _, page, blob = _fixture()
    damaged_page = page[:-1] + bytes([page[-1] ^ 1])
    found, _ = _scan_buffer(damaged_page, blob)
    assert found == {}


def test_scan_targets_wechat_executables_not_bot_or_diagnostic_commands(tmp_path):
    for pid, comm, argv in (
        (1, 'wechat', b'/usr/bin/wechat\0'),
        (2, 'python', b'/opt/venv-bot/bin/python\0-m\0wechat_ai_bot.bot\0'),
        (3, 'python', b'python\0/tmp/wechat-probe.py\0'),
        (4, 'WeChatAppEx', b'/opt/wechat/WeChatAppEx\0'),
    ):
        folder = tmp_path / str(pid)
        folder.mkdir()
        (folder / 'comm').write_text(comm)
        (folder / 'cmdline').write_bytes(argv)
        (folder / 'statm').write_text('100 50')
    with patch('wechat_ai_bot.services.core.linux_sqlcipher.Path', side_effect=lambda p: tmp_path if p == '/proc' else Path(p)):
        assert set(SqlCipherKeyScanner()._wechat_pids()) == {1, 4}
