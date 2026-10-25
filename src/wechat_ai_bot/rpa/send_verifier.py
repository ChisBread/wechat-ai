"""Read-only confirmation of outgoing messages; never repeat a send."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from xml.etree import ElementTree

from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.weixin.message_classes import MessageType
from wechat_ai_bot.weixin.message_content import chat_message_text


SEND_ACTIONS = {RPAActionType.SEND_TEXT_MESSAGE, RPAActionType.SEND_FILE, RPAActionType.SEND_VOICE}
NO_RETRY = (
    "Do not automatically resend. Check get_rpa_result and recent messages first; "
    "an unconfirmed send may still appear later and resending can duplicate it."
)


class SendVerificationError(RuntimeError):
    pass


class SendVerifier:
    """Match a new, server-acknowledged row in the intended conversation.

    A local pending row or an old identical message is not confirmation. Media
    must match the original content hash; a new unrelated image is insufficient.
    If WeChat transforms the media and no matching hash is available, fail closed.
    """

    def __init__(self, db, *, timeout=10.0, poll_interval=0.5):
        self.db = db
        self.timeout = timeout
        self.poll_interval = poll_interval

    def prepare(self, action):
        if self.db is None or not self.db.is_available:
            raise SendVerificationError("Database unavailable; send was not attempted")
        self.sender = str(self.db.user_info.account or "")
        if not self.sender:
            raise SendVerificationError("Cannot identify the sending account; send was not attempted")
        self.username = self._resolve_target(action)
        self.file_md5 = ""
        if action.action_type == RPAActionType.SEND_FILE:
            with Path(action.file_path).expanduser().open("rb") as stream:
                self.file_md5 = hashlib.file_digest(stream, "md5").hexdigest()
        rows = self._rows()
        self.watermarks = {}
        for row in rows:
            path, local_id = self._identity(row)
            self.watermarks[path] = max(self.watermarks.get(path, 0), local_id)
        self.started_at = time.time()

    def _resolve_target(self, action):
        target = action.target_username or action.target
        direct = self.db.get_contact_by_username(target)
        if direct:
            return direct.username
        if action.target_username:
            raise SendVerificationError("Recipient username unavailable; send was not attempted")
        # The database search also returns fuzzy matches. Never use them as
        # evidence for a send to a different or ambiguously named conversation.
        contacts = self.db.get_contact_by_display_name(target)
        exact = {
            contact.username
            for contact in contacts
            if target in {
                str(getattr(contact, field, "") or "")
                for field in ("username", "display_name", "remark", "nick_name", "alias")
            }
        }
        if len(exact) != 1:
            raise SendVerificationError("Recipient is missing or ambiguous; send was not attempted")
        return exact.pop()

    def _rows(self):
        if not self.db.is_available:
            raise SendVerificationError("Database unavailable during send verification")
        return self.db.get_messages_by_username(
            self.username, count=100, order="desc", refresh_if_missing=True,
        )

    @staticmethod
    def _identity(row):
        return str(row[17]), int(row[0])

    def verify(self, action):
        deadline = time.monotonic() + self.timeout
        while True:
            matches = []
            for row in self._rows():
                path, local_id = self._identity(row)
                if local_id <= self.watermarks.get(path, 0):
                    continue
                if float(row[5] or 0) < int(self.started_at):
                    continue
                # Pending/failed sends may already have a local row. Require a
                # server ID as well as content before reporting success.
                if int(row[1] or 0) <= 0:
                    continue
                sender = self.db.get_contact_by_sender_id(row[4], row[17])
                if not sender or sender.username != self.sender:
                    continue
                if self._matches(action, row):
                    matches.append({
                        "verified": True,
                        "verification": "database",
                        "recipient_username": self.username,
                        "local_id": local_id,
                        "server_id": str(row[1]),
                    })
            if matches:
                if action.action_type == RPAActionType.SEND_VOICE:
                    if len(matches) != 1:
                        raise SendVerificationError("Multiple matching voice messages; delivery is ambiguous")
                    matches[0].update(verification="database_voice_metadata", audio_content_verified=False)
                return matches[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SendVerificationError("No matching new sent message found before verification timeout")
            # Only the read is polled. The UI send is never retried.
            time.sleep(min(self.poll_interval, remaining))

    def _matches(self, action, row):
        text = chat_message_text(
            row[12], row[15], chat_username=self.username, sender_username=self.sender,
        )
        if action.action_type == RPAActionType.SEND_VOICE:
            if row[2] != MessageType.Audio or not action.recording_seconds:
                return False
            try:
                root = ElementTree.fromstring(text)
                voice = root if root.tag == "voicemsg" else root.find(".//voicemsg")
                duration = int(voice.get("voicelength", "0")) / 1000 if voice is not None else 0
            except (ElementTree.ParseError, ValueError):
                return False
            # WeChat re-encodes microphone input, so the source file hash cannot
            # match. This is explicitly metadata verification, not audio identity.
            return duration > 0 and abs(duration - action.recording_seconds) <= 2
        if action.action_type == RPAActionType.SEND_TEXT_MESSAGE:
            if row[2] not in (MessageType.Text, MessageType.Text2):
                return False
            if action.at_user_name:
                name = str(action.at_user_name).strip().lstrip("@").strip()
                parts = name.split()
                name = max(parts, key=len) if parts else ""
                # A mention must be present; a suffix/substring match is unsafe.
                match = re.match(r"^@" + re.escape(name) + r"[\u2005 \t]", text)
                if not match:
                    return False
                text = text[match.end():]
            return text.replace("\r\n", "\n") == action.content.replace("\r\n", "\n")
        if row[2] not in (MessageType.Image, MessageType.File, MessageType.FileWait):
            return False
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return False
        if row[2] == MessageType.Image:
            hashes = [node.get("md5", "") for node in root.iter("img")]
        else:
            hashes = [
                node.text or ""
                for attach in root.iter("appattach")
                for tag in ("md5", "filemd5")
                for node in attach.iter(tag)
            ]
            if not any(value.strip() for value in hashes):
                # WeChat 4.1 also stores the file hash directly under appmsg.
                # Do not scan arbitrary descendants: quoted/embedded messages
                # can contain hashes for a different attachment.
                appmsgs = [root] if root.tag == "appmsg" else root.findall("appmsg")
                hashes = [
                    node.text or ""
                    for appmsg in appmsgs
                    for tag in ("md5", "filemd5")
                    for node in appmsg.findall(tag)
                ]
        return self.file_md5 in {value.strip().lower() for value in hashes}
