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

        self.assertEqual(result.media_type, "application/x-wechat-wxgf")
        self.assertEqual(result.extension, ".wxgf")
        self.assertEqual(result.data, plain)

    def test_decrypt_wxgf_requires_explicit_xor_key(self):
        key = b"0123456789abcdef"
        plain = b"wxgf" + b"\0" * 2048
        dat = make_dat(plain=plain, key=key, xor_key=60, aes_size=32, xor_size=16)

        with self.assertRaisesRegex(WeChatDatError, "XOR key"):
            decrypt_dat_bytes(dat, aes_key=key)


class WeChatDatTrailerTest(unittest.TestCase):
    @staticmethod
    def image_bytes(kind, *, progressive=False, animated=False):
        import io
        from PIL import Image
        output = io.BytesIO()
        image = Image.new('RGB', (48, 40), 'blue')
        options = {}
        if progressive:
            options['progressive'] = True
        if animated:
            options.update(save_all=True, append_images=[Image.new('RGB', image.size, 'red')],
                           duration=100, loop=0)
        image.save(output, format=kind, **options)
        return output.getvalue()

    @staticmethod
    def decode(payload, *, xor_size=None):
        dat = make_dat(plain=payload, aes_size=32, xor_size=xor_size)
        return decrypt_dat_bytes(dat, aes_key=b'0123456789abcdef', xor_key=60)

    def test_short_metadata_is_removed_without_reencoding(self):
        trailer = b'TAG!' + b'\0' * 4 + bytes(range(16))
        for kind in ('JPEG', 'PNG', 'GIF'):
            for xor_size in (0, 16, None):
                with self.subTest(kind=kind, xor_size=xor_size):
                    image = self.image_bytes(kind)
                    result = self.decode(image + trailer, xor_size=xor_size)
                    self.assertEqual(result.data, image)
                    self.assertEqual(result.info.payload_size, len(image) + len(trailer))

    def test_unmodified_images_and_plain_files_stay_byte_identical(self):
        for kind in ('JPEG', 'PNG', 'GIF', 'WEBP'):
            with self.subTest(kind=kind):
                image = self.image_bytes(kind)
                self.assertEqual(self.decode(image).data, image)
                # Plain files keep their existing handling, including padding.
                self.assertEqual(decrypt_dat_bytes(image + b'\0' * 2048).data, image + b'\0' * 2048)

    def test_trailer_length_is_bounded_even_if_it_ends_with_a_marker(self):
        for kind, marker in (('JPEG', b'\xff\xd9'), ('PNG', PNG_BYTES[-12:]), ('GIF', b';')):
            image = self.image_bytes(kind)
            for count in (1, 24, 1024, 1025):
                with self.subTest(kind=kind, trailer_length=count):
                    trailer = b'x' * count
                    if count >= len(marker):
                        trailer = trailer[:-len(marker)] + marker
                    if count <= 1024:
                        self.assertEqual(self.decode(image + trailer).data, image)
                    else:
                        with self.assertRaisesRegex(WeChatDatError, 'integrity'):
                            self.decode(image + trailer)

    def test_progressive_jpeg_and_animated_gif_keep_all_image_data(self):
        import io
        from PIL import Image
        jpeg = self.image_bytes('JPEG', progressive=True)
        gif = self.image_bytes('GIF', animated=True)
        self.assertEqual(self.decode(jpeg + b'metadata').data, jpeg)
        decoded = self.decode(gif + b';metadata;hash').data
        self.assertEqual(decoded, gif)
        with Image.open(io.BytesIO(decoded)) as image:
            self.assertEqual(image.n_frames, 2)
            image.seek(1)
            image.load()

    def test_mpo_secondary_picture_is_not_stripped_as_metadata(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new('RGB', (48, 40), 'blue').save(
            output, format='MPO', save_all=True,
            append_images=[Image.new('RGB', (48, 40), 'red')],
        )
        image = output.getvalue()
        decoded = self.decode(image).data
        self.assertEqual(decoded, image)
        with Image.open(io.BytesIO(decoded)) as parsed:
            self.assertEqual(parsed.n_frames, 2)
            parsed.seek(1)
            parsed.load()

    def test_embedded_jpeg_thumbnail_does_not_end_the_main_image(self):
        image = self.image_bytes('JPEG')
        embedded = b'Exif\0\0' + image
        app1 = b'\xff\xe1' + struct.pack('>H', len(embedded) + 2) + embedded
        image = image[:2] + app1 + image[2:]
        self.assertEqual(self.decode(image + b'tag\0hash').data, image)
        with self.assertRaisesRegex(WeChatDatError, 'integrity'):
            self.decode(image[:-2] + b'tag\0hash')

    def test_iend_bytes_inside_png_chunk_do_not_end_the_image(self):
        import zlib
        image = self.image_bytes('PNG')
        payload = b'note\0' + PNG_BYTES[-12:]
        chunk = b'tEXt' + payload
        chunk = struct.pack('>I', len(payload)) + chunk + struct.pack('>I', zlib.crc32(chunk))
        image = image[:33] + chunk + image[33:]
        self.assertEqual(self.decode(image + b'metadata').data, image)
        with self.assertRaisesRegex(WeChatDatError, 'integrity'):
            self.decode(image[:-12] + b'metadata')

    def test_semicolon_in_gif_comment_does_not_end_the_animation(self):
        image = self.image_bytes('GIF', animated=True)
        comment = b'\x21\xfe\x03a;b\0'
        image = image[:-1] + comment + image[-1:]
        self.assertEqual(self.decode(image + b'metadata;hash').data, image)
        with self.assertRaisesRegex(WeChatDatError, 'integrity'):
            self.decode(image[:-1] + b'metadata')

    def test_corrupt_png_before_trailer_is_rejected(self):
        image = bytearray(self.image_bytes('PNG'))
        image[image.index(b'IDAT') + 5] ^= 1  # Corrupt data without fixing CRC.
        with self.assertRaisesRegex(WeChatDatError, 'integrity'):
            self.decode(bytes(image) + b'metadata')

    def test_wrong_xor_key_and_missing_terminators_still_fail(self):
        for kind, marker_size in (('JPEG', 2), ('PNG', 12), ('GIF', 1)):
            with self.subTest(kind=kind):
                image = self.image_bytes(kind)
                dat = make_dat(plain=image + b'metadata', aes_size=32, xor_key=48)
                with self.assertRaises(WeChatDatError):
                    decrypt_dat_bytes(dat, aes_key=b'0123456789abcdef', xor_key=60)
                with self.assertRaisesRegex(WeChatDatError, 'integrity'):
                    self.decode(image[:-marker_size] + b'metadata')


if __name__ == "__main__":
    unittest.main()
