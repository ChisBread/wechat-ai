from unittest.mock import patch

import pytest

from wechat_ai_bot.common.config import Config


def test_atomic_config_updates_preserve_other_settings_and_permissions(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('aes_xor_key: ""\nother:\n  enabled: true\n')
    config = Config(str(path))
    config.set('aes_xor_key', 'synthetic,60')
    config.set('other.enabled', False)
    restored = Config(str(path))
    assert restored.get('aes_xor_key') == 'synthetic,60'
    assert restored.get('other.enabled') is False
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_config_write_does_not_truncate_file_or_publish_unsaved_key(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('aes_xor_key: ""\n')
    config = Config(str(path))
    original = path.read_bytes()
    with patch('wechat_ai_bot.common.config.os.replace', side_effect=OSError('read only')):
        with pytest.raises(OSError):
            config.set('aes_xor_key', 'synthetic,60')
    assert path.read_bytes() == original
    assert config.get('aes_xor_key') == ''
    assert list(tmp_path.iterdir()) == [path]
    config.set('aes_xor_key', 'synthetic,60')
    assert Config(str(path)).get('aes_xor_key') == 'synthetic,60'
