import asyncio
import base64
import json
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.weixin.message_classes import (
    EmojiMessage,
    ImageMessage,
    MessageType,
    QuoteMessage,
    TextMessage,
)


class QuoteImagePayloadTests(unittest.TestCase):
    def setUp(self):
        self.sender = SimpleNamespace(username="quoted-sender", display_name="Quoted Sender")
        self.chat = SimpleNamespace(username="test-chat", display_name="Test Chat")
        self.current_encoded = base64.b64encode(b"synthetic current image").decode("ascii")
        self.quote_encoded = base64.b64encode(b"synthetic quoted image").decode("ascii")
        self.db = SimpleNamespace(
            is_available=True,
            ensure_contact=Mock(return_value=self.chat),
            get_contact_by_sender_id=Mock(return_value=self.sender),
            get_messages_by_username=Mock(),
        )
        self.factory = SimpleNamespace(create_message=Mock())
        self.app = create_app(
            UserInfo(account="test-bot"),
            {"debug": {"enabled": False}},
            bot=SimpleNamespace(database_service=self.db, message_factory_service=self.factory),
        )

    def message(self, cls, local_type, **values):
        attributes = {field.name: None for field in fields(cls)}
        attributes.update(local_id=10, server_id=1010, local_type=local_type, contact=self.sender)
        attributes.update(values)
        return cls(**attributes)

    def image_quote(self, cls=ImageMessage, local_type=MessageType.Image):
        return self.message(
            cls, local_type, path="quoted.dat", thumb_path=Path("quoted-thumb.dat"),
            md5="0" * 32,
        )

    def call_recent(self, quote, decoder, *, count=1, parse_media=True):
        reply = self.message(QuoteMessage, MessageType.Quote, content="Please describe", quote_message=quote)
        self.factory.create_message.return_value = reply
        # Database rows arrive newest first; the tool returns chronological order.
        rows = [(index, 1000 + index, MessageType.Quote, index, 1, 0)
                for index in range(count, 0, -1)]
        self.db.get_messages_by_username.side_effect = lambda *args, **kwargs: list(rows)
        with patch("wechat_ai_bot.mcp.app.add_media_base64", side_effect=decoder) as media:
            result = asyncio.run(self.app.call_tool("get_recent_messages", {
                "contact_name": "test-chat", "parse_media": parse_media,
            }))
        return result, json.loads(result[0].text), media

    def decode_quote(self, db, payload):
        self.assertIs(db, self.db)
        if payload["type"] in (MessageType.Image, MessageType.Emoji):
            self.assertEqual(payload, {
                "type": payload["type"], "path": "quoted.dat",
                "thumb_path": "quoted-thumb.dat", "md5": "0" * 32,
            })
            payload.update(base64=self.quote_encoded, base64_mime_type="image/png")

    def assert_no_encoded_json(self, result, metadata):
        self.assertNotIn(self.current_encoded, result[0].text)
        self.assertNotIn(self.quote_encoded, result[0].text)
        for message in metadata["messages"]:
            self.assertNotIn("base64", message)
            self.assertNotIn("quote_base64", message)

    def test_image_and_emoji_quote_metadata_and_image_block(self):
        for cls, local_type in ((ImageMessage, MessageType.Image), (EmojiMessage, MessageType.Emoji)):
            with self.subTest(local_type=local_type):
                quote = self.image_quote(cls, local_type)
                result, data, media = self.call_recent(quote, self.decode_quote)
                message = data["messages"][0]
                self.assertTrue(message["factory_ok"])
                self.assertEqual(message["type"], MessageType.Quote)
                self.assertEqual(message["quote"], {
                    "local_id": 10, "server_id": "1010", "type": local_type,
                    "type_name": MessageType.name(local_type),
                    "sender_username": "quoted-sender", "sender_display": "Quoted Sender",
                    "text": quote.to_text(),
                })
                self.assertEqual(message["quote_base64_mime_type"], "image/png")
                self.assertEqual(message["quote_image_content_index"], 1)
                self.assertNotIn("image_content_index", message)
                self.assertEqual(data["image_count"], 1)
                self.assertEqual(len(result), 2)
                self.assertEqual(result[1].type, "image")
                self.assertEqual(result[1].data, self.quote_encoded)
                self.assertEqual(result[1].mimeType, "image/png")
                self.assertEqual(media.call_count, 2)
                self.assert_no_encoded_json(result, data)

    def test_current_then_quote_images_follow_message_order(self):
        def decode(db, payload):
            if payload["type"] == MessageType.Quote:
                payload.update(base64=self.current_encoded, base64_mime_type="image/jpeg")
            else:
                self.decode_quote(db, payload)

        result, data, _ = self.call_recent(self.image_quote(), decode, count=2)
        self.assertEqual(data["image_count"], 4)
        self.assertEqual(len(result), 5)
        self.assertEqual([message["local_id"] for message in data["messages"]], [1, 2])
        for offset, message in enumerate(data["messages"]):
            current_index = 2 * offset + 1
            self.assertEqual(message["image_content_index"], current_index)
            self.assertEqual(message["quote_image_content_index"], current_index + 1)
            self.assertEqual(result[current_index].data, self.current_encoded)
            self.assertEqual(result[current_index].mimeType, "image/jpeg")
            self.assertEqual(result[current_index + 1].data, self.quote_encoded)
            self.assertEqual(result[current_index + 1].mimeType, "image/png")
        self.assert_no_encoded_json(result, data)

    def test_text_quote_is_truncated_without_decoding_quote_media(self):
        quote = self.message(TextMessage, MessageType.Text, content="文" * 2100)
        result, data, media = self.call_recent(quote, self.decode_quote)
        self.assertEqual(data["messages"][0]["quote"]["text"], "文" * 1997 + "...")
        self.assertEqual(media.call_count, 1)
        self.assertEqual(data["image_count"], 0)
        self.assertEqual(len(result), 1)
        self.assertNotIn("quote_image_content_index", data["messages"][0])

    def test_quote_media_failure_or_non_image_keeps_metadata(self):
        for outcome in ("error", "missing", "video"):
            with self.subTest(outcome=outcome):
                def decode(db, payload):
                    if payload["type"] == MessageType.Image:
                        if outcome == "error":
                            raise ValueError("synthetic decode failure")
                        if outcome == "video":
                            payload.update(base64=self.quote_encoded, base64_mime_type="video/mp4")

                result, data, _ = self.call_recent(self.image_quote(), decode)
                message = data["messages"][0]
                self.assertTrue(message["factory_ok"])
                self.assertEqual(message["quote"]["text"], "【图片】")
                self.assertNotIn("quote_image_content_index", message)
                self.assertNotIn("quote_base64_mime_type", message)
                self.assertEqual(data["image_count"], 0)
                self.assertEqual(len(result), 1)
                self.assert_no_encoded_json(result, data)

    def test_quote_text_failure_and_missing_contact_keep_image(self):
        quote = self.image_quote()
        quote.contact = None
        with patch.object(quote, "to_text", side_effect=ValueError("synthetic text failure")):
            result, data, _ = self.call_recent(quote, self.decode_quote)
        self.assertEqual(data["messages"][0]["quote"]["text"], "")
        self.assertEqual(data["messages"][0]["quote"]["sender_username"], "")
        self.assertEqual(data["messages"][0]["quote"]["sender_display"], "")
        self.assertEqual(data["image_count"], 1)
        self.assertEqual(result[1].data, self.quote_encoded)

    def test_plain_history_does_not_parse_quote(self):
        result, data, media = self.call_recent(self.image_quote(), self.decode_quote, parse_media=False)
        media.assert_not_called()
        self.factory.create_message.assert_not_called()
        self.assertNotIn("quote", data["messages"][0])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
