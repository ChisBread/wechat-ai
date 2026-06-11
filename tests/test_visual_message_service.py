import unittest
from queue import Queue
from tempfile import TemporaryDirectory
from pathlib import Path
import sqlite3

from PIL import Image

from wechat_ai_bot.services.core.linux_database_discovery import (
    SQLITE_HEADER,
    LinuxDatabaseDiscovery,
)
from wechat_ai_bot.services.core.linux_sqlcipher import verify_sqlcipher_page_hmac
from wechat_ai_bot.services.core.message_factory_service import MessageFactoryService
from wechat_ai_bot.services.core.visual_message_service import VisualMessageService


class DummyImageProcessor:
    def resolve_yolo_imgsz(self, image, imgsz="auto", stride=None):
        stride = max(1, int(stride or 32))

        def ceil(value):
            return max(stride, ((int(value) + stride - 1) // stride) * stride)

        if isinstance(imgsz, str) and imgsz == "auto":
            return [ceil(image.height), ceil(image.width)]
        return ceil(int(imgsz))


class DummyOCRProcessor:
    pass


class DummyWindowManager:
    def get_current_session_name(self):
        return "fallback"


class DummyUserInfo:
    account = "me"


class VisualMessageServiceTest(unittest.TestCase):
    def make_service(self):
        return VisualMessageService(
            window_manager=DummyWindowManager(),
            image_processor=DummyImageProcessor(),
            ocr_processor=DummyOCRProcessor(),
            message_queue=Queue(),
        )

    def test_session_name_normalization_removes_member_count(self):
        svc = self.make_service()

        self.assertEqual(svc._normalize_session_name("大众点屏 (96)"), "大众点屏")
        self.assertEqual(svc._normalize_session_name("大众点屏（96）"), "大众点屏")

    def test_region_bucket_uses_coarse_buckets(self):
        svc = self.make_service()

        self.assertEqual(svc._bucket_region([0, 47, 48, 95]), "0,0,1,1")

    def test_self_message_box_detects_right_aligned_bubble(self):
        svc = self.make_service()

        self.assertTrue(svc._is_self_message_box([420, 10, 635, 60], 682))
        self.assertFalse(svc._is_self_message_box([68, 10, 428, 60], 682))

    def test_crop_hash_handles_invalid_box(self):
        svc = self.make_service()
        image = Image.new("RGB", (20, 20), "white")

        self.assertEqual(svc._hash_detection_image(image, [10, 10, 5, 12]), "")
        self.assertNotEqual(svc._hash_detection_image(image, [1, 1, 10, 10]), "")

    def test_auto_yolo_imgsz_uses_image_shape_aligned_to_stride(self):
        svc = self.make_service()
        image = Image.new("RGB", (682, 1616), "white")

        self.assertEqual(svc._resolve_yolo_imgsz(image), [1632, 704])
        self.assertEqual(svc._last_yolo_imgsz, [1632, 704])

    def test_numeric_yolo_imgsz_is_aligned_to_stride(self):
        svc = self.make_service()
        svc.yolo_imgsz = 961

        self.assertEqual(svc._resolve_yolo_imgsz(Image.new("RGB", (20, 20))), 992)


    def test_visual_message_factory_maps_metadata(self):
        factory = MessageFactoryService(DummyUserInfo())

        message = factory.create_message(
            {
                "type": "text",
                "content": "hello",
                "timestamp": 123,
                "target": "room",
                "session_id": "room",
                "sender": "alice",
                "source": "visual:yolo",
                "visual_type": "text",
                "confidence": 0.93,
                "is_self": True,
            }
        )

        self.assertEqual(message.real_sender_name, "alice")
        self.assertTrue(message.is_self)
        self.assertEqual(message.to_json()["visual_type"], "text")
        self.assertEqual(message.to_json()["confidence"], 0.93)


class LinuxDatabaseDiscoveryTest(unittest.TestCase):
    def test_discovers_encrypted_business_db_and_plaintext_login_key_db(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "xwechat_files"
            db_storage = root / "wxid_test_abcd" / "db_storage" / "message"
            db_storage.mkdir(parents=True)
            encrypted_db = db_storage / "message_0.db"
            encrypted_db.write_bytes(b"encrypted-db-page")

            login_dir = root / "all_users" / "login" / "wxid_test"
            login_dir.mkdir(parents=True)
            login_db = login_dir / "key_info.db"
            with sqlite3.connect(login_db) as conn:
                conn.execute(
                    "create table LoginKeyInfoTable("
                    "user_name_md5 text, key_md5 text, "
                    "key_info_md5 text, key_info_data blob)"
                )

            discovery = LinuxDatabaseDiscovery(root)
            report = discovery.scan()

            self.assertEqual(len(report.accounts), 1)
            self.assertIsInstance(report.sqlcipher_available, bool)
            self.assertIsInstance(report.sqlcipher_driver, str)
            account = report.accounts[0]
            self.assertEqual(account.account_id, "wxid_test")
            self.assertEqual(account.storage_suffix, "abcd")
            self.assertEqual(account.encrypted_databases, [encrypted_db])
            self.assertEqual(account.plaintext_databases, [])
            self.assertEqual(
                discovery.read_login_key_schema(login_db),
                {
                    "LoginKeyInfoTable": [
                        "user_name_md5",
                        "key_md5",
                        "key_info_md5",
                        "key_info_data",
                    ]
                },
            )

    def test_sqlite_header_constant_matches_plain_sqlite(self):
        self.assertEqual(SQLITE_HEADER, b"SQLite format 3\x00")

    def test_sqlcipher_hmac_verifier_rejects_short_or_wrong_key(self):
        self.assertFalse(verify_sqlcipher_page_hmac(b"short", b"0" * 32))
        self.assertFalse(verify_sqlcipher_page_hmac(b"\x00" * 4096, b"1" * 32))


if __name__ == "__main__":
    unittest.main()
