import hashlib
import json
from pathlib import Path

from starlette.testclient import TestClient

from wechat_ai_bot.models import ChatRoom, Contact, UserInfo
from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService
from wechat_ai_bot.services.core.message_factory_service import MessageFactoryService
from wechat_ai_bot.weixin.parser.util.protocbuf.roomdata_pb2 import ChatRoomData
from wechat_ai_bot.mcp.app import create_app
from tests.test_debug_routes import DummyConfig, DummyMcpBot, dashboard_auth_env, dashboard_auth_headers


def contact(username, nickname):
    return Contact.from_db_row((1, username, 0, '', '', 0, 0, 0, '', '', '', nickname, '', '', '', '', '', 0, 0, '', b'', 0))


def fixture():
    db = LinuxDatabaseService(UserInfo(account='me'), scan_keys=False)
    sender = contact('alice', 'Alice')
    room = contact('room@chatroom', '测试🎃')
    other = contact('other@chatroom', '另一个群')
    db._contact_by_username = {c.username: c for c in (sender, room, other)}
    for chat, nickname in ((room, '群昵称🎃'), (other, '另一个昵称')):
        data = ChatRoomData()
        data.members.add(wxID='alice', displayName=nickname)
        db._chat_rooms[chat.username] = ChatRoom.from_db_row((1, chat.username, '', data.SerializeToString()))
        db._room_by_md5[hashlib.md5(chat.username.encode()).hexdigest()] = chat
    db._sender_username_by_id = lambda path, sender_id: 'alice' if sender_id == 3 else ''
    row = (1, 100, 1, 1, 3, 1780998000, 0, 0, 0, 0, 0, '', 'alice:\n  hello alice🎃\n', None, None, 0, 0, '/tmp/message_0.db')
    db.get_messages_by_username = lambda *args, **kwargs: [row]
    db.query_text_messages = lambda *args, **kwargs: [(row[12], 'alice', row[17], row[5], row[1], row[15])]
    db._message_username_map = {room.username: {Path(row[17])}}
    db.is_available = True
    return db, sender, room, row


def test_room_names_are_scoped_and_personal_remarks_keep_precedence():
    db, sender, room, _ = fixture()
    assert db.get_contact_in_room(room.username, 'alice').display_name == '群昵称🎃'
    assert db.get_contact_in_room('other@chatroom', 'alice').display_name == '另一个昵称'
    assert db.get_room_member_list(room.username)[0].room_remark == '群昵称🎃'
    assert sender.display_name == 'Alice'
    assert sender.room_remark == ''
    sender.remark = '我的备注'
    assert db.get_contact_in_room(room.username, 'alice').display_name == '我的备注'


def test_sender_id_never_falls_back_to_unrelated_contact_id():
    db, sender, _, row = fixture()
    db._contact_by_id[99] = sender
    assert db.get_contact_by_sender_id(99, row[17]) is None
    assert db.get_contact_by_sender_id(99) is sender


def test_factory_uses_room_nickname_and_exact_body():
    db, sender, room, row = fixture()
    factory = MessageFactoryService(db.user_info, db)
    msg = factory.create_message(('Msg_' + hashlib.md5(room.username.encode()).hexdigest(), row))
    assert msg.real_sender_name == '群昵称🎃'
    assert msg.to_text() == '  hello alice🎃\n'
    assert sender.room_remark == ''


def test_dashboard_and_mcp_agree_on_group_sender_and_body():
    db, sender, room, row = fixture()
    bot = DummyMcpBot()
    bot.database_service = db
    bot.message_factory_service = MessageFactoryService(db.user_info, db)
    with dashboard_auth_env():
        app = create_app(db.user_info, DummyConfig({'debug': {'enabled': True}}), bot=bot)
        client = TestClient(app.streamable_http_app())
        headers = dashboard_auth_headers()
        payloads = []
        for path in ('/dashboard/api/messages', '/dashboard/api/messages/recent'):
            response = client.get(path, params={'username': room.username}, headers=headers)
            assert response.status_code == 200
            payloads.extend(response.json()['messages'])
        chats = client.get('/dashboard/api/chats', headers=headers).json()
        payloads.append(chats['chats'][0]['last_message'])
        tools = app._tool_manager._tools
        for media in (False, True):
            payloads.extend(json.loads(tools['get_recent_messages'].fn(None, room.username, 10, True, media))['messages'])
        payloads.extend(json.loads(tools['search_text_messages'].fn(None, room.username))['messages'])
        payloads.append(json.loads(tools['get_recent_chats'].fn(None))['chats'][0]['last_message'])
        for payload in payloads:
            assert payload['sender_display'] == '群昵称🎃'
            assert payload['text'] == '  hello alice🎃\n'
        legacy = json.loads(tools['query_wechat_msg'].fn(None, room.username))
        assert legacy[0]['from'] == '群昵称🎃'
        assert legacy[0]['text'] == '  hello alice🎃\n'
        summary = json.loads(tools['get_chat_summary_context'].fn(None, room.username))
        assert '群昵称🎃:' in summary['context']
        assert 'alice:' not in summary['context']
        assert sender.room_remark == ''
