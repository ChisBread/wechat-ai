import asyncio
import base64
import json

import pytest

from wechat_ai_bot.mcp.app import create_app
from wechat_ai_bot.models import UserInfo
from tests.test_debug_routes import DummyConfig, DummyMediaBot
from tests.test_image_media import png


@pytest.mark.parametrize('tool, arguments', [
    ('get_recent_messages', {'parse_media': True}),
    ('get_recent_media_messages', {}),
])
@pytest.mark.parametrize('available', [True, False])
def test_native_image_tool_serialization_and_missing_media(tmp_path, available, tool, arguments):
    image = png((320, 240))
    if available:
        folder = tmp_path / 'msg/attach/alice/2026-06/Img'
        folder.mkdir(parents=True)
        (folder / 'image.dat').write_bytes(image)
    app = create_app(UserInfo(account='me'), DummyConfig({'debug': {'enabled': False}}),
                     bot=DummyMediaBot(tmp_path))
    # Exercise SDK conversion as used by tools/call, not only the Python function.
    assert 'get_recent_images' not in app._tool_manager._tools
    result = asyncio.run(app.call_tool(tool, {'contact_name': 'media', **arguments}))
    assert result[0].type == 'text'
    data = json.loads(result[0].text)
    assert data['count'] == 1
    assert 'base64' not in data['messages'][0]
    if available:
        assert data['image_count'] == 1
        assert data['messages'][0]['image_content_index'] == 1
        assert result[1].type == 'image'
        assert result[1].mimeType == 'image/png'
        assert base64.b64decode(result[1].data) == image
    else:
        assert data['image_count'] == 0
        assert data['messages'][0]['media_status'] == 'unavailable'
        assert len(result) == 1


@pytest.mark.parametrize('arguments, count', [({}, 1), ({'parse_media': True, 'include_non_text': False}, 0)])
def test_plain_history_and_text_filter_do_not_emit_images(tmp_path, arguments, count):
    app = create_app(UserInfo(account='me'), DummyConfig({'debug': {'enabled': False}}),
                     bot=DummyMediaBot(tmp_path))
    result = asyncio.run(app.call_tool('get_recent_messages', {'contact_name': 'media', **arguments}))
    assert len(result) == 1 and result[0].type == 'text'
    data = json.loads(result[0].text)
    assert data['count'] == count
    assert all('image_content_index' not in m for m in data['messages'])


@pytest.mark.parametrize('tool', ['get_recent_messages', 'get_recent_media_messages'])
def test_multiple_image_messages_map_to_correct_content_blocks(tmp_path, tool):
    folder = tmp_path / 'msg/attach/alice/2026-06/Img'
    folder.mkdir(parents=True)
    image = png((320, 240))
    (folder / 'image.dat').write_bytes(image)
    bot = DummyMediaBot(tmp_path)
    row = bot.database_service._image_row()
    second_row = (row[0] + 1, row[1] + 1, *row[2:])
    bot.database_service.get_messages_by_username = lambda *args, **kwargs: [second_row, row]
    app = create_app(UserInfo(account='me'), DummyConfig({'debug': {'enabled': False}}), bot=bot)
    arguments = {'contact_name': 'media'}
    if tool == 'get_recent_messages':
        arguments['parse_media'] = True
    result = asyncio.run(app.call_tool(tool, arguments))
    data = json.loads(result[0].text)
    assert data['image_count'] == data['count'] == 2
    assert [m['local_id'] for m in data['messages']] == [row[0], second_row[0]]
    for index, message in enumerate(data['messages'], 1):
        assert message['image_content_index'] == index
        assert base64.b64decode(result[index].data) == image
        assert 'base64' not in message
