import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_mcp_send_image import upload as image_upload
from wechat_ai_bot.mcp.file_upload import MAX_FILE_BASE64_CHARS, MAX_FILE_BYTES
from wechat_ai_bot.rpa.action_handlers import SendFileAction
from wechat_ai_bot.services.core.rpa_service import RPAService

CONTENT = b"report\x00\xff\n"
ENCODED = base64.b64encode(CONTENT).decode()


@pytest.fixture
def upload(image_upload):
    image_upload.tool = image_upload.app._tool_manager._tools['send_file_msg']

    def call(**kwargs):
        arguments = {'recipient_name': 'Alice', 'file_base64': ENCODED, 'file_name': '报告.bin'}
        arguments.update(kwargs)
        return json.loads(image_upload.tool.fn(image_upload.ctx, **arguments))

    image_upload.call = call
    return image_upload


@pytest.mark.parametrize('value', ['', '  ', '@@@', 'a', '你好', 'YWJj$', 'YW\nJj', '_w==',
    'data:text/plain;base64,', 'data:text/plain,abcd', 'data:bad;base64,YQ=='])
def test_invalid_upload_never_stages_or_queues(upload, value):
    assert upload.call(file_base64=value, dry_run=True)['status'] == 'invalid_request'
    assert upload.bot.rpa_task_queue.empty()
    assert list(upload.directory.iterdir()) == []


def test_schema_and_exclusive_inputs(upload):
    assert set(upload.tool.parameters['required']) == {'recipient_name'}
    assert {'file_path', 'file_base64', 'file_name'} <= upload.tool.parameters['properties'].keys()
    assert upload.call(file_base64=None, dry_run=True)['status'] == 'invalid_request'
    assert upload.call(file_path='/tmp/report.pdf', dry_run=True)['status'] == 'invalid_request'
    assert upload.call(recipient_name='', dry_run=True)['status'] == 'invalid_request'
    # Exercise MCP argument validation with file_path omitted.
    result = asyncio.run(upload.app.call_tool('send_file_msg', {
        'recipient_name': 'Alice', 'file_base64': ENCODED, 'dry_run': True,
    }))
    assert 'dry_run' in str(result)


@pytest.mark.parametrize('prefix', ['', 'data:application/octet-stream;base64,',
    'data:text/plain;charset=utf-8;base64,', 'data:;base64,'])
@pytest.mark.parametrize('name, expected', [(None, 'file.bin'), ('../../报告.pdf', '报告.pdf'),
    ('C:\\temp\\report.zip', 'report.zip'), ('../..', 'file.bin'), ('a\x00\n.txt', 'a.txt')])
def test_data_uri_and_safe_filename_without_io(upload, prefix, name, expected):
    result = upload.call(file_base64=prefix + ENCODED, file_name=name, dry_run=True)
    assert result['status'] == 'dry_run'
    assert result['file'] == {'file_name': expected, 'bytes': len(CONTENT)}
    assert 'file_path' not in result['action_data']
    assert not result['queued'] and not result['completed']
    assert list(upload.directory.iterdir()) == []
    assert upload.bot.rpa_task_queue.empty()


def test_filename_byte_limit(upload):
    assert upload.call(file_name='报' * 86, dry_run=True)['status'] == 'invalid_request'


@pytest.mark.parametrize('size', [MAX_FILE_BYTES, MAX_FILE_BYTES + 1])
def test_decoded_size_limit(upload, size):
    result = upload.call(file_base64=base64.b64encode(b'x' * size).decode(), dry_run=True)
    assert result['status'] == ('dry_run' if size == MAX_FILE_BYTES else 'invalid_request')
    assert list(upload.directory.iterdir()) == []


def test_encoded_size_checked_before_decode(upload, monkeypatch):
    decode = Mock(side_effect=AssertionError('must reject before decoding'))
    monkeypatch.setattr('wechat_ai_bot.mcp.file_upload.base64.b64decode', decode)
    assert upload.call(file_base64='A' * (MAX_FILE_BASE64_CHARS + 129), dry_run=True)['status'] == 'invalid_request'
    decode.assert_not_called()


def test_write_gate_and_dry_run_do_not_stage(upload, monkeypatch):
    monkeypatch.setenv('WECHAT_AI_MCP_WRITE_ENABLED', 'false')
    stage = Mock(side_effect=AssertionError('no staging allowed'))
    monkeypatch.setattr('wechat_ai_bot.mcp.app.tempfile.TemporaryDirectory', stage)
    assert upload.call()['status'] == 'blocked'
    assert upload.call(dry_run=True)['status'] == 'dry_run'
    stage.assert_not_called()
    assert upload.bot.rpa_task_queue.empty()


@pytest.mark.parametrize('recipient, target, is_room', [('alice', 'Alice', False), ('room@chatroom', 'Room', True)])
@pytest.mark.parametrize('outcome', [True, False, 'exception'])
def test_send_and_cleanup_after_worker(upload, recipient, target, is_room, outcome):
    observed = []

    def execute(action):
        assert isinstance(action, SendFileAction)
        path = Path(action.file_path)
        observed.append(path)
        assert path.name == '报告.bin' and path.read_bytes() == CONTENT
        assert path.stat().st_mode & 0o444 == 0o444
        assert path.parent.stat().st_mode & 0o111 == 0o111
        assert action.target == target and action.is_chatroom == is_room
        if outcome == 'exception':
            raise RuntimeError('synthetic execution failure')
        action.execution_result = {'verified': outcome}
        return outcome

    service = RPAService(upload.bot.rpa_task_queue, SimpleNamespace(execute_action=execute))
    service.start()
    try:
        result = upload.call(recipient_name=recipient, wait_seconds=3)
    finally:
        service.stop()
    assert result['status'] == ('executed' if outcome is True else 'failed')
    assert result['queued'] and result['completed']
    assert observed and not observed[0].exists()
    assert list(upload.directory.iterdir()) == []


@pytest.mark.parametrize('wait, status', [(0, 'queued'), (0.01, 'timeout')])
def test_timeout_retains_file_until_execution(upload, wait, status):
    result = upload.call(wait_seconds=wait)
    assert result['status'] == status and result['queued'] and not result['completed']
    path = Path(result['action_data']['file_path'])
    assert path.read_bytes() == CONTENT

    def execute(action):
        assert Path(action.file_path).read_bytes() == CONTENT
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


def test_write_failure_cleans_directory(upload, monkeypatch):
    monkeypatch.setattr(Path, 'open', Mock(side_effect=OSError('disk full')))
    assert upload.call()['status'] == 'error'
    assert list(upload.directory.iterdir()) == []
    assert upload.bot.rpa_task_queue.empty()


def test_remote_dispatcher_rejected_and_cleaned(upload):
    remote = SimpleNamespace(dispatch_rpa_wait=Mock())
    upload.ctx.request_context.lifespan_context.command_dispatcher = remote
    assert upload.call()['status'] == 'unavailable'
    remote.dispatch_rpa_wait.assert_not_called()
    assert list(upload.directory.iterdir()) == []


def test_local_path_compatibility_and_no_cleanup(upload):
    path = upload.directory / 'existing.txt'
    path.write_bytes(CONTENT)
    # Keep the original positional argument order for existing Python callers.
    result = json.loads(upload.tool.fn(upload.ctx, 'Alice', str(path), True, 0))
    assert result['status'] == 'dry_run' and result['file_path'] == str(path)
    result = upload.call(file_base64=None, file_path=str(path), wait_seconds=0)
    assert result['status'] == 'queued'
    action = upload.bot.rpa_task_queue.get_nowait()
    assert action.file_path == str(path) and action.on_complete is None
    assert path.read_bytes() == CONTENT
    assert upload.call(file_base64=None, file_path='/missing/file', dry_run=True)['status'] == 'invalid_request'
