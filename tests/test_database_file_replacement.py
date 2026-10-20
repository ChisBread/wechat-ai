"""Exercise real SQLCipher/SQLite connections, not mocked query results."""
import hashlib
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wechat_ai_bot.models import UserInfo
from wechat_ai_bot.services.core.linux_database_service import LinuxDatabaseService, MESSAGE_COLUMNS

KEY = '01' * 32  # Synthetic key, never a real account key.


def table_for(username):
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


def create_database(driver, path, users, *, key=KEY, wal=False):
    conn = driver.connect(str(path))
    conn.execute(f'PRAGMA key="x\'{key}\'"')
    if wal:
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE Name2Id(user_name TEXT)')
    for username, count in users.items():
        conn.execute('INSERT INTO Name2Id VALUES (?)', (username,))
        table = table_for(username)
        conn.execute(f'CREATE TABLE {table} (' + ','.join(f'"{c}"' for c in MESSAGE_COLUMNS) + ')')
        for i in range(1, count + 1):
            insert_message(conn, username, i)
    conn.commit()
    return conn


def insert_message(conn, username, local_id):
    row = [0] * len(MESSAGE_COLUMNS)
    row[0] = row[1] = row[3] = row[5] = local_id
    row[2] = 1
    row[12] = 'synthetic message'
    conn.execute(f'INSERT INTO {table_for(username)} VALUES (' + ','.join('?' for _ in row) + ')', row)


@pytest.fixture(params=['sqlite3', 'sqlcipher3'])
def database(request, tmp_path):
    driver = pytest.importorskip(request.param)
    folder = tmp_path / 'message'
    folder.mkdir()
    path = folder / 'message_0.db'
    create_database(driver, path, {'alice': 1}).close()
    service = LinuxDatabaseService(UserInfo(account='synthetic'), scan_keys=False,
                                   message_map_refresh_interval=0)
    service._driver = driver
    service._keys[path] = KEY
    service._primary_account = SimpleNamespace(db_storage_dir=tmp_path)
    service.load_message_username_map()
    service._prime_message_sequences()
    service.is_available = True
    yield service, driver, path
    service.stop()


def replace_database(driver, path, users=None, key=KEY):
    replacement = path.with_name('replacement.db')
    create_database(driver, replacement, users or {'alice': 2}, key=key).close()
    os.replace(replacement, path)


def test_send_verification_discovers_new_conversation_without_periodic_refresh(database):
    service, driver, path = database
    assert service.get_messages_by_username("first-chat", refresh_if_missing=True) == []
    writer = driver.connect(str(path))
    try:
        writer.execute(f'PRAGMA key="x\'{KEY}\'"')
        writer.execute("INSERT INTO Name2Id VALUES (?)", ("first-chat",))
        writer.execute(f"CREATE TABLE {table_for('first-chat')} (" +
                       ",".join(f'"{c}"' for c in MESSAGE_COLUMNS) + ")")
        insert_message(writer, "first-chat", 1)
        writer.commit()
    finally:
        writer.close()
    assert service.get_messages_by_username("first-chat") == []
    assert len(service.get_messages_by_username("first-chat", refresh_if_missing=True)) == 1
    # Confirming a send does not consume rows from existing chats.
    assert service._table_sequences[(path, table_for("alice"))] == 1


@pytest.mark.parametrize('reader', ['poll', 'mcp'])
def test_replacement_recovers_without_errors_or_rescan_and_preserves_offsets(database, reader):
    service, driver, path = database
    old = service._connections[path]
    replace_database(driver, path, {'alice': 2, 'new-chat': 1})
    if reader == 'mcp':
        # Public query entry used by MCP; also discovers a new chat immediately.
        assert len(service.get_messages_by_username('new-chat')) == 1
        assert len(service.get_messages_by_username('alice')) == 2
    messages = service.check_new_messages()
    assert {(table, row[0]) for table, row in messages} == {
        (table_for('alice'), 2), (table_for('new-chat'), 1),
    }
    assert service.check_new_messages() == []
    assert service._consecutive_poll_errors == 0
    assert service._connection_replacements == 1
    assert service._connections[path] is not old
    with pytest.raises(Exception):
        old.execute('select 1')


def test_missing_path_never_serves_old_data_or_creates_empty_database(database):
    service, driver, path = database
    path.unlink()
    with pytest.raises(FileNotFoundError):
        service.execute_query(path, 'select count(*) from Name2Id')
    assert not path.exists()
    assert path not in service._connections
    assert service._table_sequences[(path, table_for('alice'))] == 1
    create_database(driver, path, {'alice': 2}).close()
    assert [row[0] for _, row in service.check_new_messages()] == [2]
    assert service.check_new_messages() == []


def test_replacement_during_query_retries_before_returning_rows(database):
    service, driver, path = database
    original = service._connections[path]

    class ReplacingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            replace_database(driver, path)
            return rows

    class Connection:
        def execute(self, query, params=()):
            return ReplacingCursor(original.execute(query, params))

        def close(self):
            original.close()

    service._connections[path] = Connection()
    rows = service.execute_query(path, f'SELECT count(*) FROM {table_for("alice")}')
    assert rows == [(2,)]
    assert service._connection_replacements == 1


def test_unlink_between_stat_and_open_does_not_create_database(database):
    service, driver, path = database
    service._drop_connection(path)
    connect = driver.connect

    def remove_then_connect(*args, **kwargs):
        path.unlink()
        return connect(*args, **kwargs)

    with patch.object(driver, 'connect', side_effect=remove_then_connect):
        with pytest.raises(driver.OperationalError):
            service._connection(path)
    assert not path.exists()
    assert path not in service._connections


def test_key_change_keeps_failed_tables_available_for_existing_error_recovery(database):
    service, driver, path = database
    if driver.__name__ == 'sqlite3':
        pytest.skip('requires encrypted database')
    next_key = '02' * 32
    replace_database(driver, path, {'alice': 2, 'new-chat': 1}, key=next_key)
    assert service.check_new_messages() == []
    assert service._consecutive_poll_errors == 1
    assert (path, table_for('alice')) in service._table_username_map
    assert service._table_sequences[(path, table_for('alice'))] == 1
    # Exercise the existing three-failure refresh with a synthetic key scanner.
    service.scan_keys_enabled = True
    with (patch.object(service, '_discover_accounts'),
          patch.object(service, '_select_primary_account'),
          patch.object(service, 'load_contacts'),
          patch.object(service, 'load_chat_rooms'),
          patch.object(service, 'get_status', return_value={'available': True}),
          patch.object(service, 'rescan_keys', side_effect=lambda: service._keys.update({path: next_key}))):
        service._primary_account.account_dir = path.parent.parent
        service._primary_account.account_id = 'synthetic'
        service.check_new_messages()
        service.check_new_messages()
    assert {(table, row[0]) for table, row in service.check_new_messages()} == {
        (table_for('alice'), 2), (table_for('new-chat'), 1),
    }
    assert service.check_new_messages() == []


@pytest.mark.parametrize('driver_name', ['sqlite3', 'sqlcipher3'])
def test_normal_wal_writes_reuse_connection(tmp_path, driver_name):
    driver = pytest.importorskip(driver_name)
    path = tmp_path / 'live.db'
    writer = create_database(driver, path, {'alice': 1}, wal=True)
    service = LinuxDatabaseService(UserInfo(), scan_keys=False)
    service._driver = driver
    service._keys[path] = KEY
    try:
        assert service.execute_query(path, f'SELECT count(*) FROM {table_for("alice")}') == [(1,)]
        connection = service._connections[path]
        insert_message(writer, 'alice', 2)
        writer.commit()
        assert service.execute_query(path, f'SELECT count(*) FROM {table_for("alice")}') == [(2,)]
        assert service._connections[path] is connection
        assert service._connection_replacements == 0
    finally:
        service.stop()
        writer.close()


def test_replacement_between_mapping_queries_does_not_mix_generations(database):
    service, driver, path = database
    execute = service.execute_query
    replaced = False

    def replace_after_username_query(db_path, query, params=()):
        nonlocal replaced
        rows = execute(db_path, query, params)
        if query == 'select rowid, user_name from Name2Id' and not replaced:
            replaced = True
            replace_database(driver, path, {'alice': 2, 'new-chat': 1})
        return rows

    service.message_map_refresh_interval = 1
    service._last_message_map_refresh = 0
    with patch.object(service, 'execute_query', side_effect=replace_after_username_query):
        assert len(service.get_messages_by_username('new-chat')) == 1
    assert {(table, row[0]) for table, row in service.check_new_messages()} == {
        (table_for('alice'), 2), (table_for('new-chat'), 1),
    }
    assert service.check_new_messages() == []


@pytest.fixture(params=['sqlite3', 'sqlcipher3'])
def orphan_writer(request, tmp_path):
    """A separate writer is essential: POSIX locks are per process."""
    import subprocess
    import sys
    pytest.importorskip(request.param)
    path = tmp_path / 'message' / 'message_0.db'
    path.parent.mkdir()
    script = '''
import importlib, sys
from pathlib import Path
from tests.test_database_file_replacement import create_database, insert_message
path = Path(sys.argv[2])
conn = create_database(importlib.import_module(sys.argv[1]), path, {'alice': 1}, wal=True)
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print('ready', flush=True)
for command in sys.stdin:
    command = command.strip()
    if command == 'unlink':
        Path(str(path) + '-wal').unlink()
        Path(str(path) + '-shm').unlink()
        Path(str(path) + '-wal').touch()
        Path(str(path) + '-shm').write_bytes(bytes(32768))
    elif command == 'lock':
        conn.execute('BEGIN IMMEDIATE')
    elif command == 'unlock':
        conn.rollback()
    else:
        insert_message(conn, 'alice', int(command))
        conn.commit()
    print('ok', flush=True)
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', script, request.param, str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'ready'

    def command(value):
        process.stdin.write(str(value) + '\n')
        process.stdin.flush()
        assert process.stdout.readline().strip() == 'ok'

    services = []
    def reader():
        service = LinuxDatabaseService(UserInfo(), scan_keys=False, message_map_refresh_interval=0)
        service._driver = importlib.import_module(request.param)
        service._keys[path] = KEY
        service._primary_account = SimpleNamespace(db_storage_dir=path.parent.parent)
        service._database_files._wechat_processes = lambda: [Path('/proc') / str(process.pid)]
        service.load_message_username_map()
        service._prime_message_sequences()
        service.is_available = True
        services.append(service)
        return service

    yield reader, command, path
    for service in services:
        service.stop()
        assert service._database_files.snapshot_count == 0
    process.terminate()
    process.wait(timeout=10)


def test_orphaned_wal_recovers_and_tracks_commits_across_reader_restart(orphan_writer):
    reader, command, path = orphan_writer
    service = reader()
    inode = path.stat().st_ino
    command('unlink')
    command(2)
    service._database_files._last_scan = float('-inf')
    assert path.stat().st_ino == inode
    assert [row[0] for _, row in service.check_new_messages()] == [2]
    assert service._database_files.snapshot_count == 1
    assert service.check_new_messages() == []
    command(3)
    assert [row[0] for _, row in service.check_new_messages()] == [3]
    assert service.check_new_messages() == []
    assert len(service.get_messages_by_username('alice')) == 3
    service.stop()
    restarted = reader()
    assert len(restarted.get_messages_by_username('alice')) == 3
    command(4)
    assert [row[0] for _, row in restarted.check_new_messages()] == [4]
    assert restarted.check_new_messages() == []
    # Recovery never repairs or writes the live replacement WAL.
    assert Path(str(path) + '-wal').read_bytes() == b''
    assert path.stat().st_ino == inode


def test_orphan_snapshot_waits_for_writer_and_does_not_skip_messages(orphan_writer):
    reader, command, path = orphan_writer
    service = reader()
    command('unlink')
    command(2)
    command('lock')
    service._database_files._last_scan = float('-inf')
    assert service.check_new_messages() == []
    assert service._table_sequences[(path, table_for('alice'))] == 1
    command('unlock')
    assert [row[0] for _, row in service.check_new_messages()] == [2]
    assert service.check_new_messages() == []
