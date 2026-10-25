import hashlib
import logging
import time
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.action_handlers import SendFileAction, SendTextMessageAction
from wechat_ai_bot.rpa.action_handlers.send_text_message_handler import SendTextMessageHandler
from wechat_ai_bot.rpa.controller import RPAController
from wechat_ai_bot.rpa.send_verifier import SendVerifier, SendVerificationError
from wechat_ai_bot.services.core.rpa_service import RPAService
from wechat_ai_bot.weixin.message_classes import MessageType


def row(local_id=2, server_id=123, sender_id=1, text="hello", kind=MessageType.Text,
        timestamp=None, path="/message.db"):
    return (
        local_id, server_id, kind, local_id, sender_id,
        int(time.time()) if timestamp is None else timestamp,
        0, 0, 0, 0, "", "", text, None, None, 0, 0, path,
    )


@pytest.fixture
def db():
    contact = SimpleNamespace(username="alice", display_name="Alice")
    return SimpleNamespace(
        is_available=True, user_info=UserInfo(account="me"),
        get_contact_by_username=Mock(side_effect=lambda name: contact if name == "alice" else None),
        get_contact_by_display_name=Mock(return_value=[contact]),
        get_contact_by_sender_id=Mock(side_effect=lambda id, path: SimpleNamespace(username="me" if id == 1 else "other")),
        get_messages_by_username=Mock(return_value=[]),
    )


def prepared(db, action=None, baseline=()):
    action = action or SendTextMessageAction(content="hello", target="Alice")
    db.get_messages_by_username.return_value = list(baseline)
    verifier = SendVerifier(db, timeout=0)
    verifier.prepare(action)
    return verifier, action


def test_new_self_message_is_verified(db):
    verifier, action = prepared(db, baseline=[row(local_id=1)])
    db.get_messages_by_username.return_value = [row(), row(local_id=1)]
    result = verifier.verify(action)
    assert result == {
        "verified": True, "verification": "database", "recipient_username": "alice",
        "local_id": 2, "server_id": "123",
    }
    db.get_messages_by_username.assert_called_with(
        "alice", count=100, order="desc", refresh_if_missing=True,
    )


@pytest.mark.parametrize("candidate", [
    row(local_id=1),                      # old identical message
    row(sender_id=2),                     # incoming echo
    row(text="hello again"),              # substring is insufficient
    row(server_id=0),                     # local pending or failed row
    row(timestamp=1),                     # newly discovered historical row
    row(text="hello", kind=MessageType.Image),
])
def test_unrelated_or_unconfirmed_rows_never_count_as_success(db, candidate):
    verifier, action = prepared(db, baseline=[row(local_id=1)])
    db.get_messages_by_username.return_value = [candidate]
    with pytest.raises(SendVerificationError, match="timeout"):
        verifier.verify(action)


def test_existing_pending_row_becoming_sent_does_not_confirm_another_send(db):
    verifier, action = prepared(db, baseline=[row(server_id=0)])
    db.get_messages_by_username.return_value = [row()]
    with pytest.raises(SendVerificationError):
        verifier.verify(action)


def test_read_polling_waits_for_server_ack_without_sending(db):
    verifier, action = prepared(db)
    verifier.timeout, verifier.poll_interval = 0.2, 0.001
    db.get_messages_by_username.side_effect = [[], [row(server_id=0)], [row()]]
    assert verifier.verify(action)["verified"]
    assert db.get_messages_by_username.call_count == 4


def test_database_read_failure_is_not_success(db):
    verifier, action = prepared(db)
    db.get_messages_by_username.side_effect = OSError("database unavailable")
    with pytest.raises(OSError):
        verifier.verify(action)


@pytest.mark.parametrize("available, contacts", [
    (False, []), (True, []),
    (True, [SimpleNamespace(username="a", display_name="Alice"),
            SimpleNamespace(username="b", display_name="Alice")]),
    (True, [SimpleNamespace(username="a", display_name="Alice2")]),
])
def test_missing_database_and_ambiguous_or_fuzzy_target_fail_before_send(db, available, contacts):
    db.is_available = available
    db.get_contact_by_display_name.return_value = contacts
    with pytest.raises(SendVerificationError, match="not attempted"):
        prepared(db)


@pytest.mark.parametrize("name, content, valid", [
    ("Bob", "me:\n@Bob\u2005hello", True),
    ("Bob", "me:\n@Bob\u2005hello again", False),
    ("Bob", "me:\n@Other\u2005hello", False),
    ("Bob", "me:\nhello", False),
    ("y", "me:\n@y\u2005hello", True),
    ("y", "me:\n\u2005@y\u2005hello", True),
    ("y", "me:\n\u2005@Yang\u2005hello", False),
    ("y", "me:\n@Yang\u2005hello", False),
    ("Bread extra", "me:\n@Bread extra\u2005hello", True),
    ("Bread extra", "me:\n@Bread\u2005hello", False),
])
def test_group_envelope_and_mention_match(db, name, content, valid):
    db.get_contact_by_username.side_effect = None
    db.get_contact_by_username.return_value = SimpleNamespace(username="room@chatroom")
    action = SendTextMessageAction(content="hello", target="Room",
                                   target_username="room@chatroom", at_user_name=name)
    verifier, _ = prepared(db, action)
    db.get_messages_by_username.return_value = [row(text=content)]
    if valid:
        assert verifier.verify(action)["verified"]
    else:
        with pytest.raises(SendVerificationError):
            verifier.verify(action)


@pytest.mark.parametrize("kind", [MessageType.Image, MessageType.File])
@pytest.mark.parametrize("matching", [True, False])
def test_media_requires_matching_content_hash(db, tmp_path, kind, matching):
    path = tmp_path / "image.png"
    path.write_bytes(b"synthetic file content")
    digest = hashlib.md5(path.read_bytes()).hexdigest() if matching else "different-image"
    action = SendFileAction(file_path=str(path), target="Alice")
    verifier, _ = prepared(db, action)
    text = (f'<msg><img md5="{digest}"/></msg>' if kind == MessageType.Image
            else f"<msg><appmsg><appattach><md5>{digest}</md5></appattach></appmsg></msg>")
    db.get_messages_by_username.return_value = [row(kind=kind, text=text)]
    if matching:
        assert verifier.verify(action)["verified"]
    else:
        with pytest.raises(SendVerificationError):
            verifier.verify(action)


@pytest.mark.parametrize("kind", [MessageType.File, MessageType.FileWait])
@pytest.mark.parametrize("tag", ["md5", "filemd5"])
@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("attachment", ["<appattach/>", "<appattach><md5> </md5><filemd5/></appattach>"])
def test_file_hash_directly_under_appmsg(db, tmp_path, kind, tag, wrapped, attachment):
    path = tmp_path / "report.txt"
    path.write_bytes(b"file verification regression")
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    verifier, action = prepared(db, SendFileAction(file_path=str(path), target="Alice"))
    text = f"<appmsg>{attachment}<{tag}> {digest.upper()} </{tag}></appmsg>"
    if wrapped:
        text = f"<msg>{text}</msg>"
    db.get_messages_by_username.return_value = [row(kind=kind, text=text)]
    assert verifier.verify(action)["verified"]


@pytest.mark.parametrize("content", [
    "<appattach/><md5>different-file</md5>",
    "<appattach/><refermsg><md5>{digest}</md5></refermsg>",
    "<appattach/><refermsg><appmsg><md5>{digest}</md5></appmsg></refermsg>",
    "<appattach><md5>different-file</md5></appattach><md5>{digest}</md5>",
    "<appattach/><md5/>",
])
def test_file_hash_fallback_rejects_unrelated_or_conflicting_hashes(db, tmp_path, content):
    path = tmp_path / "report.txt"
    path.write_bytes(b"file verification regression")
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    verifier, action = prepared(db, SendFileAction(file_path=str(path), target="Alice"))
    text = "<msg><appmsg>" + content.format(digest=digest) + "</appmsg></msg>"
    db.get_messages_by_username.return_value = [row(kind=MessageType.File, text=text)]
    with pytest.raises(SendVerificationError, match="timeout"):
        verifier.verify(action)


def controller_for(db, handler, monkeypatch):
    controller = object.__new__(RPAController)
    controller.db = db
    controller.logger = logging.getLogger(__name__)
    controller.window_manager = SimpleNamespace(ensure_action_ready=lambda action: True)
    controller.action_handlers = {action_type: handler for action_type in
                                  (SendTextMessageAction().action_type, SendFileAction().action_type)}
    controller._can_send_message = lambda: True
    monkeypatch.setattr("wechat_ai_bot.rpa.controller.time.sleep", lambda _: None)
    monkeypatch.setattr("wechat_ai_bot.rpa.controller.SendVerifier",
                        lambda db: SendVerifier(db, timeout=0))
    return controller


@pytest.mark.parametrize("kind", ["text", "image", "file"])
@pytest.mark.parametrize("confirmed", [True, False])
def test_bridge_returns_verified_result_or_failed_and_never_retries(db, monkeypatch, tmp_path, kind, confirmed):
    path = tmp_path / "test.png"
    path.write_bytes(b"test image")
    digest = hashlib.md5(path.read_bytes()).hexdigest()

    def send(action):
        if confirmed:
            db.get_messages_by_username.return_value = [
                row() if kind == "text" else (
                    row(kind=MessageType.Image, text=f'<msg><img md5="{digest}"/></msg>') if kind == "image"
                    else row(kind=MessageType.File,
                             text=f"<msg><appmsg><appattach/><md5>{digest}</md5></appmsg></msg>")
                )
            ]
        return True  # Button clicked, even when WeChat is frozen.

    handler = SimpleNamespace(execute=Mock(side_effect=send))
    controller = controller_for(db, handler, monkeypatch)
    queue = Queue()
    service = RPAService(queue, controller)
    dispatcher = QueueCommandDispatcher(SimpleNamespace(rpa_task_queue=queue, rpa_service=service), db.user_info)
    service.start()
    try:
        if kind == "text":
            result = dispatcher.dispatch_wait("unused", {
                "local_type": MessageType.Text, "nickname": "Alice", "message_content": "hello",
            }, timeout=2)
        else:
            result = dispatcher.dispatch_rpa_wait("send_file", {
                "target": "Alice", "file_path": str(path),
            }, timeout=2)
        queue.join()
    finally:
        service.stop()
    assert result["status"] == ("executed" if confirmed else "failed")
    assert result["result"]["verified"] is confirmed
    assert result["result"]["ok"] is confirmed
    assert result["retryable"] is False
    assert "resend" in result["guidance"]
    assert service.get_result(result["action_id"])["status"] == result["status"]
    assert handler.execute.call_count == 1
    assert queue.empty()


def test_unavailable_database_prevents_click(db, monkeypatch):
    db.is_available = False
    handler = SimpleNamespace(execute=Mock(return_value=True))
    controller = controller_for(db, handler, monkeypatch)
    action = SendTextMessageAction(content="hello", target="Alice")
    assert controller.execute_action(action) is False
    assert action.execution_result["send_attempted"] is False
    handler.execute.assert_not_called()


def test_timeout_can_query_original_task_without_requeue(db):
    queue = Queue()
    controller = SimpleNamespace(execute_action=Mock(return_value=True))
    service = RPAService(queue, controller)
    dispatcher = QueueCommandDispatcher(SimpleNamespace(rpa_task_queue=queue, rpa_service=service), db.user_info)
    result = dispatcher.dispatch_wait("unused", {
        "local_type": MessageType.Text, "nickname": "Alice", "message_content": "hello",
    }, timeout=0.001)
    assert result["status"] == "timeout"
    assert service.get_result(result["action_id"])["status"] == "queued"
    service.start()
    try:
        queue.join()
    finally:
        service.stop()
    # A controller that merely returns True cannot bypass worker verification.
    assert service.get_result(result["action_id"])["status"] == "failed"
    assert controller.execute_action.call_count == 1
    assert service.get_result("unknown")["retryable"] is False


@pytest.mark.parametrize("confirmed", [True, False])
def test_text_handler_sends_only_after_confirmed_mention(confirmed, monkeypatch):
    window = SimpleNamespace(
        switch_session=Mock(return_value=True), close_all_windows=Mock(), action_delay=0,
    )
    sender = SimpleNamespace(
        mention_user=Mock(return_value=confirmed), send_message=Mock(return_value=True),
    )
    controller = SimpleNamespace(
        window_manager=window, message_sender=sender, image_processor=None,
        ocr_processor=None, input_handler=None, ui_helper=None,
        logger=logging.getLogger(__name__),
    )
    monkeypatch.setattr("wechat_ai_bot.rpa.action_handlers.send_text_message_handler.time.sleep", lambda _: None)
    handler = SendTextMessageHandler(controller)
    assert handler.execute(SendTextMessageAction(target="Room", content="hello", at_user_name="y")) is confirmed
    sender.mention_user.assert_called_once_with("y")
    if confirmed:
        sender.send_message.assert_called_once_with("hello", clear_input_box=False, activate_input_box=False)
    else:
        sender.send_message.assert_not_called()
    window.close_all_windows.assert_called_once()
