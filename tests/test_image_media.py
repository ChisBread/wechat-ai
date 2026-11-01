import io
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from wechat_ai_bot.mcp.debug import add_media_base64, _dashboard_dat_media_response
from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService
from wechat_ai_bot.services.core.wechat_dat import decrypt_dat_bytes, WeChatDatError
from wechat_ai_bot.services.core.wechat_dat_key_discovery import DerivedDatKey
from wechat_ai_bot.services.core.wechat_image import image_variants, present_dat_image
from tests.test_wechat_dat import make_dat


def png(size=(32, 24)):
    out = io.BytesIO()
    Image.new('RGB', size, 'blue').save(out, format='PNG')
    return out.getvalue()


def db_at(root, user_info=None):
    return SimpleNamespace(root=root, _primary_account=SimpleNamespace(account_dir=root),
                           user_info=user_info or UserInfo())


def test_original_preferred_and_thumbnail_fallback_is_reported(tmp_path):
    (tmp_path / 'a_h.dat').write_bytes(png((320, 240)))
    (tmp_path / 'a.dat').write_bytes(png((160, 120)))
    (tmp_path / 'a_t.dat').write_bytes(png((16, 12)))
    payload = {'path': 'a.dat', 'thumb_path': 'a_t.dat'}
    add_media_base64(db_at(tmp_path), payload)
    assert payload['media_tier'] == 'original'
    assert payload['media_width'] == 320
    assert payload['is_thumbnail'] is False
    (tmp_path / 'a_h.dat').unlink()
    (tmp_path / 'a.dat').write_bytes(b'broken')
    payload = {'path': 'a.dat', 'thumb_path': 'a_t.dat'}
    add_media_base64(db_at(tmp_path), payload)
    assert payload['is_thumbnail'] is True
    assert payload['media_width'] == 16
    assert payload['media_fallback_reason'] == 'higher_resolution_unavailable'
    assert payload['media_fallback_errors']


def test_large_image_keeps_original_url_instead_of_thumbnail(tmp_path):
    (tmp_path / 'a_h.dat').write_bytes(png((320, 240)) + b'\0' * (5 * 1024 * 1024))
    (tmp_path / 'a_t.dat').write_bytes(png((16, 12)))
    payload = {'thumb_path': 'a_t.dat'}
    add_media_base64(db_at(tmp_path), payload)
    assert payload['media_tier'] == 'original'
    assert payload['media_width'] == 320
    assert payload['media_inline_reason'] == 'exceeds_5_mib'
    assert 'base64' not in payload
    assert 'a_h.dat' in payload['media_url']


@pytest.mark.parametrize('configured_key', ['', 'wrongwrongwrong12'])
def test_mcp_and_dashboard_recover_key_and_return_same_image(tmp_path, configured_key):
    image = png()
    path = tmp_path / 'a.dat'
    path.write_bytes(make_dat(plain=image, aes_size=32))
    derived = DerivedDatKey(key=b'0123456789abcdef', config_value='0123456789abcdef',
                            encoding='text', xor_key=60, account_name='test', code=60)
    user = UserInfo(dat_key=configured_key)
    payload = {'path': 'a.dat'}
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', return_value=derived):
        add_media_base64(db_at(tmp_path, user), payload)
        response = _dashboard_dat_media_response(path, UserInfo(dat_key=configured_key),
                    bot=SimpleNamespace(database_service=db_at(tmp_path)), headers={}, as_download=False)
    import base64
    assert base64.b64decode(payload['base64']) == response.body == image
    assert payload['media_key_refreshed'] is True
    assert user.dat_key == derived.config_value
    assert response.headers['content-type'] == 'image/png'


def test_failed_key_is_not_cached(tmp_path):
    path = tmp_path / 'a.dat'
    path.write_bytes(make_dat(plain=png(), aes_size=32))
    user = UserInfo()
    bad = SimpleNamespace(config_value='wrongwrongwrong12', xor_key=60)
    payload = {'path': 'a.dat'}
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', return_value=bad):
        add_media_base64(db_at(tmp_path, user), payload)
    assert payload['media_status'] == 'unavailable'
    assert payload['media_errors']
    assert not user.dat_key


def test_database_selects_original_only_and_thumbnail_only_files(tmp_path):
    db = LinuxDatabaseService(UserInfo(), scan_keys=False)
    db._primary_account = SimpleNamespace(account_dir=tmp_path)
    db._hardlink_dir_name = lambda rowid: {1: 'session', 2: '2026-10'}[rowid]
    folder = tmp_path / 'msg/attach/session/2026-10/Img'
    folder.mkdir(parents=True)
    (folder / 'a_h.dat').write_bytes(png())
    row = (0, '', 0, 'a.dat', 0, 0, 1, 2)
    assert db._hardlink_relative_path(row, 'Img').name == 'a_h.dat'
    assert db._hardlink_relative_path(row, 'Img', thumb=True).name == 'a_t.dat'
    msg = SimpleNamespace(str_time='2026-10-20', local_id=12, create_time=100)
    assert db._image_by_filename('a', msg, '', thumb=False).name == 'a_h.dat'
    (folder / '12_100_t.dat').write_bytes(png())
    assert db.get_image_thumb(msg, '').name == '12_100_t.dat'
    assert db.get_image_by_time(msg, '') is None


def test_wxgf_conversion_reports_missing_decoder():
    result = decrypt_dat_bytes(b'wxgf' + b'\0' * 20)
    with patch('wechat_ai_bot.services.core.wechat_image.shutil.which', return_value=None):
        with pytest.raises(WeChatDatError, match='requires ffmpeg'):
            present_dat_image(result)


def test_real_hevc_conversion_and_shared_media_paths(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('ffmpeg not installed')
    encoded = subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=64x48', '-frames:v', '1', '-c:v', 'libx265',
        '-x265-params', 'pools=1:frame-threads=1:log-level=error', '-f', 'hevc', 'pipe:1'],
        capture_output=True, timeout=20)
    assert encoded.returncode == 0, encoded.stderr.decode()
    raw = b'wxgf' + b'\0' * 20 + encoded.stdout
    path = tmp_path / 'a.dat'
    path.write_bytes(make_dat(plain=raw, aes_size=32))
    user = UserInfo(dat_key='0123456789abcdef', dat_xor_key=61)
    payload = {'path': 'a.dat'}
    derived = DerivedDatKey(key=b'0123456789abcdef', config_value='0123456789abcdef',
                            encoding='text', xor_key=60, account_name='test', code=60)
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', return_value=derived):
        add_media_base64(db_at(tmp_path, user), payload)
    assert user.dat_xor_key == 60
    response = _dashboard_dat_media_response(path, user, headers={}, as_download=False)
    import base64
    assert base64.b64decode(payload['base64']) == response.body
    assert payload['base64_mime_type'] == 'image/png'
    assert (payload['media_width'], payload['media_height']) == (64, 48)
    assert payload['media_conversion'] == 'wxgf_first_frame_png'


def test_download_placeholder_cannot_report_success_or_click_ui():
    from wechat_ai_bot.rpa.action_handlers.download_image_handler import DownloadImageAction, DownloadImageHandler
    controller = SimpleNamespace(window_manager=Mock(), image_processor=Mock(), ocr_processor=Mock(),
                                 input_handler=Mock(), ui_helper=Mock(), logger=logging.getLogger('test'))
    handler = DownloadImageHandler(controller)
    assert handler.execute(DownloadImageAction(target='test')) is False
    assert not controller.window_manager.mock_calls


@pytest.mark.parametrize('entry', ['mcp', 'startup'])
def test_discovered_media_keys_survive_restart_without_rescan(tmp_path, entry):
    from wechat_ai_bot.common.config import Config
    from wechat_ai_bot.mcp.debug import restore_dat_media_keys
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('aes_xor_key: ""\nother: keep\n')
    config = Config(str(config_path))
    folder = tmp_path / 'msg/attach/session/2026-10/Img'
    folder.mkdir(parents=True)
    path = folder / 'a.dat'
    path.write_bytes(make_dat(plain=png(), aes_size=32))
    derived = DerivedDatKey(key=b'0123456789abcdef', config_value='0123456789abcdef',
                            encoding='text', xor_key=60, account_name='test', code=60)
    db = db_at(tmp_path)
    db.config = config
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', return_value=derived):
        if entry == 'startup':
            assert restore_dat_media_keys(db, config)
        else:
            payload = {'path': str(path)}
            add_media_base64(db, payload)
            assert payload['media_status'] == 'available'
    reloaded = Config(str(config_path))
    key, xor = reloaded.get('aes_xor_key').split(',')
    assert reloaded.get('other') == 'keep'
    restarted = db_at(tmp_path, UserInfo(dat_key=key, dat_xor_key=int(xor)))
    restarted.config = reloaded
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', side_effect=AssertionError('unexpected discovery')):
        payload = {'path': str(path)}
        add_media_base64(restarted, payload)
        assert payload['media_status'] == 'available'
        assert payload['media_key_refreshed'] is False


def test_valid_runtime_key_is_persisted_and_invalid_key_is_not(tmp_path):
    from wechat_ai_bot.common.config import Config
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('aes_xor_key: ""\n')
    config = Config(str(config_path))
    (tmp_path / 'a.dat').write_bytes(make_dat(plain=png(), aes_size=32))
    db = db_at(tmp_path, UserInfo(dat_key='wrongwrongwrong12', dat_xor_key=60))
    db.config = config
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file', return_value=None):
        add_media_base64(db, {'path': 'a.dat'})
    assert Config(str(config_path)).get('aes_xor_key') == ''
    db.user_info.dat_key = '0123456789abcdef'
    add_media_base64(db, {'path': 'a.dat'})
    assert Config(str(config_path)).get('aes_xor_key') == '0123456789abcdef,60'


@pytest.mark.parametrize('tier', ['standard', 'original'])
def test_dat_trailer_does_not_force_thumbnail_fallback(tmp_path, tier):
    import base64
    output = io.BytesIO()
    Image.new('RGB', (796, 776), 'blue').save(output, format='JPEG')
    image = output.getvalue()
    trailer = b'TAG!' + b'\0' * 4 + bytes(range(16))
    path = tmp_path / ('a_h.dat' if tier == 'original' else 'a.dat')
    path.write_bytes(make_dat(plain=image + trailer, aes_size=32))
    (tmp_path / 'a_t.dat').write_bytes(png((150, 146)))
    user = UserInfo(dat_key='0123456789abcdef', dat_xor_key=60)
    payload = {'path': 'a.dat', 'thumb_path': 'a_t.dat'}
    with patch('wechat_ai_bot.mcp.debug.derive_dat_key_for_file') as discover:
        add_media_base64(db_at(tmp_path, user), payload)
        response = _dashboard_dat_media_response(path, user, headers={}, as_download=False)
        discover.assert_not_called()
    assert payload['media_tier'] == tier
    assert payload['is_thumbnail'] is False
    assert (payload['media_width'], payload['media_height']) == (796, 776)
    assert payload['media_size_bytes'] == len(image)
    assert payload.get('media_fallback_reason') != 'higher_resolution_unavailable'
    assert 'media_fallback_errors' not in payload
    assert base64.b64decode(payload['base64']) == response.body == image
