import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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


def make_dat(plain: bytes = PNG_BYTES, key: bytes = b"0123456789abcdef", xor_key: int = 60) -> bytes:
    prefix = plain[:1024].ljust(1024, b"\0")
    suffix = plain[1024:]
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    aes_data = encryptor.update(prefix) + encryptor.finalize()
    header = struct.pack("<6sLLx", DAT_V2_SIGNATURE, len(aes_data), len(suffix))
    xor_prefix = bytes(byte ^ xor_key for byte in b"PADDING-PREFIX!!")
    return header + aes_data + xor_prefix + bytes(byte ^ xor_key for byte in suffix)


class WeChatDatTest(unittest.TestCase):
    def test_parse_dat_info(self):
        dat = make_dat()

        info = parse_dat_info(dat)

        self.assertIsNotNone(info)
        self.assertEqual(info.version, "v2")
        self.assertEqual(info.aes_size, 1024)
        self.assertEqual(info.xor_size, 0)
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

    def test_decrypt_requires_aes_key(self):
        dat = make_dat(plain=LONG_PNG_BYTES, xor_key=60)

        with self.assertRaisesRegex(WeChatDatError, "AES key"):
            decrypt_dat_bytes(dat, xor_key=60)

    def test_infer_xor_key_from_png_tail(self):
        dat = make_dat(plain=LONG_PNG_BYTES, xor_key=60)
        info = parse_dat_info(dat)

        self.assertEqual(infer_xor_key(dat, info), 60)


if __name__ == "__main__":
    unittest.main()
