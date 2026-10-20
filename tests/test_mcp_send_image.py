import asyncio
import base64
import io
import json
import tempfile
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from tests.test_debug_routes import DummyConfig, DummyMcpBot
from wechat_ai_bot.mcp.app import AppContext, create_app
from wechat_ai_bot.mcp.dispatchers import QueueCommandDispatcher
from wechat_ai_bot.mcp.image_upload import MAX_IMAGE_BYTES, MAX_IMAGE_BASE64_CHARS
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.rpa.action_handlers import SendFileAction
from wechat_ai_bot.rpa.rpa_action import RPAActionType
from wechat_ai_bot.services.core.rpa_service import RPAService


def image_bytes(format='PNG'):
    stream = io.BytesIO()
    Image.new('RGB', (8, 6), 'blue').save(stream, format=format)
    return stream.getvalue()


@pytest.fixture
def upload(tmp_path, monkeypatch):
    monkeypatch.setenv('WECHAT_AI_MCP_WRITE_ENABLED', 'true')
    # All staged image files stay in a test-owned directory.
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr('wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory',
                        lambda **kwargs: original(dir=tmp_path, **kwargs))
    bot = DummyMcpBot()
    bot.rpa_task_queue = Queue()
    user = UserInfo(account='synthetic')
    dispatcher = QueueCommandDispatcher(bot, user)
    app = create_app(user, DummyConfig({'debug': {'enabled': False}}), bot=bot)
    context = SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context=AppContext(user, dispatcher, {}, {})))
    tool = app._tool_manager._tools['send_image_msg']

    def call(recipient_name='Alice', image_base64=base64.b64encode(image_bytes()).decode(), **kwargs):
        return json.loads(tool.fn(context, recipient_name, image_base64, **kwargs))

    return SimpleNamespace(app=app, tool=tool, call=call, bot=bot, ctx=context, directory=tmp_path)


@pytest.mark.parametrize('value', [None, '', '  ', '@@@', 'a', '你好', 'data:image/png;base64,',
    'data:image/png,abcd', 'data:text/plain;base64,YQ==', 'YWJj$', base64.b64encode(b'BMfake').decode(),
    base64.b64encode(b'<svg/>').decode()])
def test_invalid_image_returns_json_and_never_stages_or_queues(upload, value):
    result = upload.call(image_base64=value, dry_run=True)
    assert result['status'] == 'invalid_request'
    assert upload.bot.rpa_task_queue.empty()
    assert list(upload.directory.iterdir()) == []


def test_required_mcp_arguments(upload):
    assert set(upload.tool.parameters['required']) == {'recipient_name', 'image_base64'}
    # Missing arguments are rejected by MCP schema validation, before any IO.
    with pytest.raises(Exception, match='image_base64'):
        asyncio.run(upload.app.call_tool('send_image_msg', {'recipient_name': 'Alice', 'dry_run': True}))
    assert upload.call(recipient_name='', dry_run=True)['status'] == 'invalid_request'
    assert upload.bot.rpa_task_queue.empty()
    assert list(upload.directory.iterdir()) == []


@pytest.mark.parametrize('size', [MAX_IMAGE_BYTES, MAX_IMAGE_BYTES + 1])
def test_decoded_size_limit(upload, size):
    data = image_bytes()
    encoded = base64.b64encode(data + b'\0' * (size - len(data))).decode()
    result = upload.call(image_base64=encoded, dry_run=True)
    assert result['status'] == ('dry_run' if size == MAX_IMAGE_BYTES else 'invalid_request')
    assert list(upload.directory.iterdir()) == []


def test_oversized_encoded_input_is_rejected_before_decoding(upload, monkeypatch):
    decode = Mock(side_effect=AssertionError('must reject before decoding'))
    monkeypatch.setattr('wechat_ai_bot.mcp.image_upload.base64.b64decode', decode)
    result = upload.call(image_base64='A' * (MAX_IMAGE_BASE64_CHARS + 129), dry_run=True)
    assert result['status'] == 'invalid_request'
    decode.assert_not_called()


@pytest.mark.parametrize('format, mime, extension', [('PNG','image/png','.png'),
    ('JPEG','image/jpeg','.jpg'), ('WEBP','image/webp','.webp'), ('GIF','image/gif','.gif')])
@pytest.mark.parametrize('data_uri', [False, True])
def test_formats_data_uri_and_corrected_mime_filename(upload, format, mime, extension, data_uri):
    data = image_bytes(format)
    encoded = base64.b64encode(data).decode()
    if data_uri:
        encoded = 'data:image/png;base64,' + encoded
    result = upload.call(image_base64=encoded, mime_type='image/png',
                         file_name='../../nested\\photo.wrong', dry_run=True)
    assert result['status'] == 'dry_run'
    assert result['image'] == {'mime_type': mime, 'bytes': len(data), 'file_name': 'photo' + extension}
    assert result['queued'] is False and result['completed'] is False
    assert result['result'] is None and result['wait_seconds'] == 12
    assert 'file_path' not in result['action_data']
    assert list(upload.directory.iterdir()) == []


def test_blocked_and_dry_run_do_not_write_or_queue(upload, monkeypatch):
    monkeypatch.setenv('WECHAT_AI_MCP_WRITE_ENABLED', 'false')
    stage = Mock(side_effect=AssertionError('no staging allowed'))
    monkeypatch.setattr('wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory', stage)
    assert upload.call()['status'] == 'blocked'
    assert upload.call(dry_run=True)['status'] == 'dry_run'
    stage.assert_not_called()
    assert upload.bot.rpa_task_queue.empty()


@pytest.mark.parametrize('recipient, target, is_room', [('alice','Alice',False), ('room@chatroom','Room',True)])
@pytest.mark.parametrize('outcome', [True, False, 'exception'])
def test_send_file_action_and_cleanup_after_worker_completion(upload, recipient, target, is_room, outcome):
    observed = []

    def execute(action):
        assert isinstance(action, SendFileAction)
        assert action.action_type == RPAActionType.SEND_FILE
        path = Path(action.file_path)
        observed.append((action, path.read_bytes()))
        assert path.suffix == '.png' and len(path.stem) == 32
        assert path.stat().st_mode & 0o444 == 0o444
        assert path.parent.stat().st_mode & 0o111 == 0o111
        if outcome == 'exception':
            raise RuntimeError('synthetic controller failure')
        return outcome

    service = RPAService(upload.bot.rpa_task_queue, SimpleNamespace(execute_action=execute))
    service.start()
    try:
        result = upload.call(recipient_name=recipient, wait_seconds=3)
    finally:
        service.stop()
    assert result['status'] == ('executed' if outcome is True else 'failed')
    assert result['queued'] and result['completed'] and not result['dry_run']
    assert result['result']['ok'] == (outcome is True)
    action, content = observed[0]
    assert content == image_bytes()
    assert action.target == target and action.is_chatroom == is_room
    assert result['resolved']['is_chatroom'] == is_room
    assert result['action_data']['file_path'] == action.file_path
    assert list(upload.directory.iterdir()) == []
    assert upload.bot.rpa_task_queue.unfinished_tasks == 0


@pytest.mark.parametrize('wait, status', [(0,'queued'), (0.01,'timeout')])
def test_async_or_timed_out_send_keeps_file_until_execution(upload, wait, status):
    result = upload.call(wait_seconds=wait)
    assert result['status'] == status and result['queued'] and not result['completed']
    path = Path(result['action_data']['file_path'])
    assert path.read_bytes() == image_bytes()

    def execute(action):
        assert Path(action.file_path).read_bytes() == image_bytes()
        service.is_running = False
        return True

    service = RPAService(upload.bot.rpa_task_queue, SimpleNamespace(execute_action=execute))
    service.is_running = True
    service._execute_loop()
    assert not path.exists() and list(upload.directory.iterdir()) == []


def test_queue_failure_cleans_file(upload, monkeypatch):
    monkeypatch.setattr(upload.bot.rpa_task_queue, 'put', Mock(side_effect=RuntimeError('queue failed')))
    result = upload.call()
    assert result['status'] == 'error' and not result['queued']
    assert list(upload.directory.iterdir()) == []


def test_staging_failure_returns_json(upload, monkeypatch):
    monkeypatch.setattr('wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory',
                        Mock(side_effect=OSError('disk full')))
    assert upload.call()['status'] == 'error'
    assert upload.bot.rpa_task_queue.empty()


def test_remote_dispatcher_does_not_receive_a_temporary_path(upload):
    remote = SimpleNamespace(dispatch_rpa_wait=Mock())
    upload.ctx.request_context.lifespan_context.command_dispatcher = remote
    assert upload.call()['status'] == 'unavailable'
    remote.dispatch_rpa_wait.assert_not_called()
    assert list(upload.directory.iterdir()) == []
