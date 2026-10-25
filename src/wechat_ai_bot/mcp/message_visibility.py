"""Read message lists without exposing incoming voice before its text is ready."""

from wechat_ai_bot.weixin.message_content import message_content_text
from wechat_ai_bot.weixin.parser.audio_parser import audio_transcript


def row_transcript(row):
    return audio_transcript(message_content_text(row[12], row[15]), row[14])


def get_display_messages(db, username, *, count, bot=None, order="desc"):
    service = getattr(bot, "voice_transcription_service", None)
    enabled = bool(getattr(service, "enabled", False))
    # Look behind hidden voices so a pending latest message does not blank the
    # chat preview. Keep the database read bounded, even for old voice-only chats.
    rows = db.get_messages_by_username(
        username, count=count + 64 if enabled else count, order=order,
    )
    if not enabled:
        return rows
    account = getattr(getattr(db, "user_info", None), "account", "")
    account = account or getattr(getattr(bot, "user_info", None), "account", "")

    def visible(row):
        if row[2] != 34 or row_transcript(row).strip():
            return True
        if int(row[7] or 0) > 0:
            return True  # Outgoing audio does not enter automatic transcription.
        try:
            sender = db.get_contact_by_sender_id(row[4], row[17])
        except Exception:
            sender = None
        return bool(account and getattr(sender, "username", "") == account)

    # Check the DB content itself, not the pending queue: queries can race ahead
    # of the message listener, and a failed/timed-out job still has no usable text.
    return [row for row in rows if visible(row)][:count]
