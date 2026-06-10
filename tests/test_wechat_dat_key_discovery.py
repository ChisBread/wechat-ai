import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from wechat_ai_bot.services.core.wechat_dat_key_discovery import (
    _AesScanResult,
    WeChatDatKeyDiscovery,
    _DatAesKeyScanner,
    _account_candidates,
    derive_dat_key_for_file,
    _generate_probe_png,
)
from wechat_ai_bot.services.core.wechat_dat import DAT_V2_SIGNATURE, parse_dat_file


def make_dat_from_plain(
    plain: bytes,
    key: bytes = b"0123456789abcdef",
    xor_key: int = 60,
    aes_size: int = 1024,
    xor_size: int | None = None,
) -> bytes:
    if xor_size is None:
        xor_size = max(0, len(plain) - aes_size)
    prefix = plain[:aes_size]
    raw = plain[aes_size : len(plain) - xor_size]
    suffix = plain[len(plain) - xor_size :]
    padder = padding.PKCS7(128).padder()
    padded_prefix = padder.update(prefix) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded_prefix) + encryptor.finalize()
    header = struct.pack("<6sLLx", DAT_V2_SIGNATURE, aes_size, xor_size)
    return header + encrypted + raw + bytes(byte ^ xor_key for byte in suffix)


LINUX_PROBE_PNG = _generate_probe_png(974999100)


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

    def test_send_probe_false_reuses_latest_existing_probe(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            old_probe = runtime / "wechat_ai_dat_probe_1.png"
            old_probe.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)
            new_probe = runtime / "wechat_ai_dat_probe_2.png"
            new_probe.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 2048)
            old_time = 1000
            new_time = 2000
            import os

            os.utime(old_probe, (old_time, old_time))
            os.utime(new_probe, (new_time, new_time))

            discovery = WeChatDatKeyDiscovery(
                bot=object(),
                xwechat_root=Path(tmp) / "xwechat_files",
                runtime_dir=runtime,
            )

            path, data = discovery._latest_existing_probe_image()

            self.assertEqual(path, new_probe)
            self.assertEqual(data, new_probe.read_bytes())

    def test_candidate_keys_preserve_text_key_config_value(self):
        scanner = _DatAesKeyScanner(
            process_names=(),
            max_region_size=1,
            chunk_size=1,
            raw_aligned_scan=False,
        )

        candidates = list(scanner._candidate_keys(b"0123456789abcdef"))

        self.assertEqual(candidates[0].key, b"0123456789abcdef")
        self.assertEqual(candidates[0].config_value, "0123456789abcdef")
        self.assertEqual(candidates[0].encoding, "text")

    def test_candidate_keys_try_32_char_prefix_as_aes128(self):
        scanner = _DatAesKeyScanner(
            process_names=(),
            max_region_size=1,
            chunk_size=1,
            raw_aligned_scan=False,
        )

        candidates = list(scanner._candidate_keys(b"0123456789abcdef0123456789abcdef"))

        self.assertEqual(candidates[0].key, b"0123456789abcdef")
        self.assertEqual(candidates[0].config_value, "0123456789abcdef")
        self.assertEqual(candidates[0].encoding, "text-prefix16")

    def test_account_candidates_include_linux_rm_base_name(self):
        self.assertIn("RM616319889", _account_candidates("RM616319889_7b2d"))

    def test_derive_dat_key_for_file_uses_linux_kvcomm_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            img_dir = root / "RM616319889_7b2d" / "msg" / "attach" / "filehelper" / "2026-06" / "Img"
            img_dir.mkdir(parents=True)
            kvcomm = Path(tmp) / ".xwechat" / "net" / "kvcomm"
            kvcomm.mkdir(parents=True)
            (kvcomm / "key_974999100_4067692804_1_1_1_3600_input.statistic").write_text("")
            dat_path = img_dir / "image_h.dat"
            dat_path.write_bytes(
                make_dat_from_plain(LINUX_PROBE_PNG, key=b"765a3ffda1280704", xor_key=60)
            )

            derived = derive_dat_key_for_file(dat_path, xwechat_root=root)

            self.assertIsNotNone(derived)
            self.assertEqual(derived.config_value, "765a3ffda1280704")
            self.assertEqual(derived.xor_key, 60)
            self.assertEqual(derived.account_name, "RM616319889")

    def test_derive_dat_key_for_wxgf_does_not_require_inferred_xor(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            img_dir = root / "RM616319889_7b2d" / "msg" / "attach" / "filehelper" / "2026-06" / "Img"
            img_dir.mkdir(parents=True)
            kvcomm = Path(tmp) / ".xwechat" / "net" / "kvcomm"
            kvcomm.mkdir(parents=True)
            (kvcomm / "key_974999100_4067692804_1_1_1_3600_input.statistic").write_text("")
            dat_path = img_dir / "image_h.dat"
            dat_path.write_bytes(
                make_dat_from_plain(
                    b"wxgf" + b"\0" * 2048,
                    key=b"765a3ffda1280704",
                    xor_key=60,
                    aes_size=32,
                    xor_size=16,
                )
            )

            derived = derive_dat_key_for_file(dat_path, xwechat_root=root)

            self.assertIsNotNone(derived)
            self.assertEqual(derived.config_value, "765a3ffda1280704")
            self.assertEqual(derived.xor_key, 60)

    def test_discover_derives_linux_kvcomm_aes_key(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            img_dir = root / "RM616319889_7b2d" / "msg" / "attach" / "filehelper" / "2026-06" / "Img"
            img_dir.mkdir(parents=True)
            kvcomm = Path(tmp) / ".xwechat" / "net" / "kvcomm"
            kvcomm.mkdir(parents=True)
            (kvcomm / "key_974999100_4067692804_1_1_1_3600_input.statistic").write_text("")
            aes_key = b"765a3ffda1280704"
            bot = SimpleNamespace(
                user_info=SimpleNamespace(dat_key="", dat_xor_key=-1),
                config={},
            )

            class FakeDiscovery(WeChatDatKeyDiscovery):
                def generate_probe_image(self):
                    path, png = super().generate_probe_image()
                    (img_dir / "probe_h.dat").write_bytes(
                        make_dat_from_plain(png, key=aes_key, xor_key=60)
                    )
                    return path, png

                def _scan_aes_key(self, **kwargs):
                    raise AssertionError("kvcomm-derived key should skip memory scan")

            result = FakeDiscovery(
                bot=bot,
                xwechat_root=root,
                runtime_dir=Path(tmp) / "runtime",
            ).discover(send_probe=False, scan_timeout_seconds=5)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.aes_key, "765a3ffda1280704")
            self.assertEqual(result.aes_key_encoding, "text")
            self.assertEqual(result.matched_method, "kvcomm_derive")
            self.assertEqual(result.xor_key, 60)
            self.assertEqual(bot.user_info.dat_key, "765a3ffda1280704")
            self.assertEqual(bot.user_info.dat_xor_key, 60)

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
