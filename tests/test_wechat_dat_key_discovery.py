import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_ai_bot.services.core.wechat_dat_key_discovery import (
    _AesScanResult,
    WeChatDatKeyDiscovery,
    _DatAesKeyScanner,
    _generate_probe_png,
)
from wechat_ai_bot.services.core.wechat_dat import DAT_V2_SIGNATURE, parse_dat_file


def make_dat_from_plain(plain: bytes, key: bytes = b"0123456789abcdef", xor_key: int = 60) -> bytes:
    prefix = plain[:1024]
    suffix = plain[1024:]
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(prefix) + encryptor.finalize()
    header = struct.pack("<6sLLx", DAT_V2_SIGNATURE, len(encrypted), len(suffix))
    xor_prefix = bytes(byte ^ xor_key for byte in b"PADDING-PREFIX!!")
    return header + encrypted + xor_prefix + bytes(byte ^ xor_key for byte in suffix)


class WeChatDatKeyDiscoveryTest(unittest.TestCase):
    def test_generate_probe_png_is_large_enough_for_dat_aes_segment(self):
        png = _generate_probe_png(12345)

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1024)
        self.assertIn(b"IEND", png[-12:])

    def test_find_probe_dat_matches_plain_payload_size(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            img_dir = root / "wxid_test_abcd" / "msg" / "attach" / "filehelper" / "2026-06" / "Img"
            img_dir.mkdir(parents=True)
            runtime = Path(tmp) / "runtime"
            discovery = WeChatDatKeyDiscovery(
                bot=object(),
                xwechat_root=root,
                runtime_dir=runtime,
            )
            probe_path, probe = discovery.generate_probe_image()
            dat_path = img_dir / "probe_h.dat"
            dat_path.write_bytes(make_dat_from_plain(probe))

            matched = discovery._find_probe_dat(probe_path, since=0)

            self.assertEqual(matched, dat_path)
            self.assertEqual(parse_dat_file(matched).xor_size, len(probe) - 1024)

    def test_candidate_keys_preserve_text_key_config_value(self):
        scanner = _DatAesKeyScanner(
            process_names=(),
            max_region_size=1,
            chunk_size=1,
        )

        candidates = list(scanner._candidate_keys(b"0123456789abcdef"))

        self.assertEqual(candidates[0].key, b"0123456789abcdef")
        self.assertEqual(candidates[0].config_value, "0123456789abcdef")
        self.assertEqual(candidates[0].encoding, "text")

    def test_discover_updates_runtime_keys_when_probe_dat_exists(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            img_dir = root / "wxid_test_abcd" / "msg" / "attach" / "filehelper" / "2026-06" / "Img"
            img_dir.mkdir(parents=True)
            bot = SimpleNamespace(
                user_info=SimpleNamespace(dat_key="", dat_xor_key=-1),
                config={},
            )

            class FakeDiscovery(WeChatDatKeyDiscovery):
                def generate_probe_image(self):
                    path, png = super().generate_probe_image()
                    (img_dir / "probe_h.dat").write_bytes(make_dat_from_plain(png))
                    return path, png

                def _scan_aes_key(self, **kwargs):
                    return _AesScanResult(
                        key=b"0123456789abcdef",
                        key_value="0123456789abcdef",
                        key_encoding="text",
                        candidate_count=1,
                    )

            result = FakeDiscovery(
                bot=bot,
                xwechat_root=root,
                runtime_dir=Path(tmp) / "runtime",
            ).discover(send_probe=False, scan_timeout_seconds=5)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.xor_key, 60)
            self.assertEqual(result.aes_key, "0123456789abcdef")
            self.assertEqual(result.to_dict()["aes_key_preview"], "0123***cdef")
            self.assertEqual(bot.user_info.dat_key, "0123456789abcdef")
            self.assertEqual(bot.user_info.dat_xor_key, 60)
            self.assertEqual(bot.config["aes_xor_key"], "0123456789abcdef,60")


if __name__ == "__main__":
    unittest.main()
