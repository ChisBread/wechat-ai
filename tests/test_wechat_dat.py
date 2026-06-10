import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from wechat_ai_bot.services.core.wechat_dat import (
    DAT_HEADER_SIZE,
    DAT_V2_SIGNATURE,
    DAT_XOR_PREFIX_SIZE,
    WeChatDatError,
    decrypt_dat_bytes,
    infer_xor_key,
    parse_dat_info,
)


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\xe2%}"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
LONG_PNG_BYTES = PNG_BYTES[:-12] + (b"x" * 2048) + PNG_BYTES[-12:]


def make_dat(
    plain: bytes = PNG_BYTES,
    key: bytes = b"0123456789abcdef",
    xor_key: int = 60,
    aes_size: int = 1024,
    xor_size: int | None = None,
) -> bytes:
    if xor_size is None:
        xor_size = max(0, len(plain) - aes_size)
    if aes_size + xor_size > len(plain):
        raise ValueError("aes_size + xor_size exceeds plain size")
    prefix = plain[:aes_size]
    raw = plain[aes_size : len(plain) - xor_size]
    suffix = plain[len(plain) - xor_size :]
    padder = padding.PKCS7(128).padder()
    padded_prefix = padder.update(prefix) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    aes_data = encryptor.update(padded_prefix) + encryptor.finalize()
    header = struct.pack("<6sLLx", DAT_V2_SIGNATURE, aes_size, xor_size)
    return header + aes_data + raw + bytes(byte ^ xor_key for byte in suffix)


class WeChatDatTest(unittest.TestCase):
    def test_parse_dat_info(self):
        dat = make_dat(plain=LONG_PNG_BYTES)

        info = parse_dat_info(dat)

        self.assertIsNotNone(info)
        self.assertEqual(info.version, "v2")
        self.assertEqual(info.aes_size, 1024)
        self.assertEqual(info.encrypted_aes_size, 1040)
        self.assertEqual(info.xor_size, len(LONG_PNG_BYTES) - 1024)
        self.assertEqual(info.payload_size, len(LONG_PNG_BYTES))
        self.assertEqual(info.expected_file_size, len(dat))
        self.assertEqual(DAT_HEADER_SIZE, 15)
        self.assertEqual(DAT_XOR_PREFIX_SIZE, 16)

    def test_decrypt_dat_bytes_with_keys(self):
        key = b"0123456789abcdef"
        plain = LONG_PNG_BYTES
        dat = make_dat(plain=plain, key=key, xor_key=60)

        result = decrypt_dat_bytes(dat, aes_key=key, xor_key=60)

        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.extension, ".png")
        self.assertEqual(result.data, plain)

    def test_decrypt_rejects_wrong_xor_key_even_when_magic_matches(self):
        key = b"0123456789abcdef"
        plain = LONG_PNG_BYTES
        dat = make_dat(plain=plain, key=key, xor_key=48)

        with self.assertRaisesRegex(WeChatDatError, "integrity"):
            decrypt_dat_bytes(dat, aes_key=key, xor_key=60)

    def test_decrypt_requires_aes_key(self):
        dat = make_dat(plain=LONG_PNG_BYTES, xor_key=60)

        with self.assertRaisesRegex(WeChatDatError, "AES key"):
            decrypt_dat_bytes(dat, xor_key=60)

    def test_infer_xor_key_from_png_tail(self):
        dat = make_dat(plain=LONG_PNG_BYTES, xor_key=60)
        info = parse_dat_info(dat)

        self.assertEqual(infer_xor_key(dat, info), 60)

    def test_decrypt_dat_bytes_with_raw_middle_segment(self):
        key = b"0123456789abcdef"
        plain = LONG_PNG_BYTES
        dat = make_dat(plain=plain, key=key, xor_key=48, aes_size=32, xor_size=16)

        result = decrypt_dat_bytes(dat, aes_key=key, xor_key=48)

        self.assertEqual(result.data, plain)
        self.assertGreater(result.info.raw_size, 0)

    def test_decrypt_wxgf_payload(self):
        key = b"0123456789abcdef"
        plain = b"wxgf" + b"\0" * 2048
        dat = make_dat(plain=plain, key=key, xor_key=60, aes_size=32, xor_size=16)

        result = decrypt_dat_bytes(dat, aes_key=key, xor_key=60)

        self.assertEqual(result.media_type, "video/hevc")
        self.assertEqual(result.extension, ".hevc")
        self.assertEqual(result.data, plain)

    def test_decrypt_wxgf_requires_explicit_xor_key(self):
        key = b"0123456789abcdef"
        plain = b"wxgf" + b"\0" * 2048
        dat = make_dat(plain=plain, key=key, xor_key=60, aes_size=32, xor_size=16)

        with self.assertRaisesRegex(WeChatDatError, "XOR key"):
            decrypt_dat_bytes(dat, aes_key=key)


if __name__ == "__main__":
    unittest.main()
