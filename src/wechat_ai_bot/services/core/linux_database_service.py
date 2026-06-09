"""Read-only Linux WeChat database service backed by Python SQLCipher."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from wechat_ai_bot.models import ChatRoom, Contact, FMessage, UserInfo
from wechat_ai_bot.services.core.linux_database_discovery import (
    LinuxDatabaseDiscovery,
    LinuxDatabaseDiscoveryReport,
)
from wechat_ai_bot.services.core.linux_sqlcipher import (
    SqlCipherKeyScanResult,
    SqlCipherKeyScanner,
    import_sqlcipher_driver,
    sqlcipher_driver_name,
)
from wechat_ai_bot.weixin.parser.util.common import get_md5_from_xml


MESSAGE_COLUMNS = (
    "local_id",
    "server_id",
    "local_type",
    "sort_seq",
    "real_sender_id",
    "create_time",
    "status",
    "upload_status",
    "download_status",
    "server_seq",
    "origin_source",
    "source",
    "message_content",
    "compress_content",
    "packed_info_data",
    "WCDB_CT_message_content",
    "WCDB_CT_source",
)


@dataclass
class LinuxDatabaseAccount:
    account_dir: Path
    account_id: str
    suffix: str
    db_storage_dir: Path
    databases: dict[str, Path] = field(default_factory=dict)


class LinuxDatabaseService:
    """Linux implementation of the DatabaseService protocol."""

    def __init__(
        self,
        user_info: UserInfo,
        *,
        xwechat_files_root: str | Path = "/config/xwechat_files",
        scan_keys: bool = True,
        active_account: str = "",
        key_retry_interval: float = 10.0,
        message_map_refresh_interval: float = 30.0,
    ):
        self.logger = logging.getLogger(__name__)
        self.user_info = user_info
        self.root = Path(xwechat_files_root)
        self.scan_keys_enabled = scan_keys
        self.active_account = active_account
        self.key_retry_interval = key_retry_interval
        self.message_map_refresh_interval = message_map_refresh_interval

        self.driver_name = sqlcipher_driver_name()
        self._driver = None
        self._scan_result = SqlCipherKeyScanResult()
        self._discovery_report: LinuxDatabaseDiscoveryReport | None = None
        self._accounts: list[LinuxDatabaseAccount] = []
        self._primary_account: LinuxDatabaseAccount | None = None
        self._connections: dict[Path, Any] = {}
        self._keys: dict[Path, str] = {}
        self._table_sequences: dict[tuple[Path, str], int] = {}
        self._message_username_map: dict[str, set[Path]] = {}
        self._table_username_map: dict[tuple[Path, str], str] = {}
        self._contact_by_username: dict[str, Contact] = {}
        self._contact_by_id: dict[int, Contact] = {}
        self._room_by_md5: dict[str, Contact] = {}
        self._chat_rooms: dict[str, ChatRoom] = {}
        self._lock = threading.RLock()

        self.is_available = False
        self.last_error = ""
        self.last_scan_at = 0.0
        self._last_message_map_refresh = 0.0

    def setup(self) -> None:
        """Discover DBs, load keys, open primary caches, and prime sequence state."""
        with self._lock:
            self._driver = import_sqlcipher_driver()
            self.driver_name = self.driver_name or sqlcipher_driver_name()
            self.refresh()
            self.logger.info(
                "LinuxDatabaseService setup: available=%s driver=%s accounts=%s keys=%s",
                self.is_available,
                self.driver_name,
                len(self._accounts),
                len(self._keys),
            )

    def refresh(self) -> dict[str, Any]:
        """Refresh discovery, keyring, caches, and polling baselines."""
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
            self.last_error = ""
            self._discover_accounts()
            if self.scan_keys_enabled:
                self.rescan_keys()
            else:
                self.last_error = "database.scan_keys is disabled"
            self._select_primary_account()
            if self._primary_account:
                self.user_info.data_dir = str(self._primary_account.account_dir)
                self.user_info.account = self.user_info.account or self._primary_account.account_id
            self.load_contacts()
            self.load_chat_rooms()
            self.load_message_username_map()
            self._prime_message_sequences()
            self.is_available = bool(self._message_tables())
            if not self.is_available and not self.last_error:
                self.last_error = "no open message tables"
            return self.get_status()

    def stop(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()

    def rescan_keys(self) -> dict[str, Any]:
        with self._lock:
            self._discover_accounts()
            encrypted_paths = [
                path
                for account in self._accounts
                for path in account.databases.values()
                if path.name.endswith(".db")
            ]
            scanner = SqlCipherKeyScanner()
            self._scan_result = scanner.scan(encrypted_paths)
            self._keys.update(self._scan_result.found_keys)
            self.last_scan_at = time.time()
            if self._scan_result.errors and not self._keys:
                self.last_error = "; ".join(self._scan_result.errors[-3:])
            return self._scan_result.redacted()

    def execute_query(
        self,
        db_path: Path,
        query: str,
        params: tuple = (),
    ) -> list[tuple]:
        with self._lock:
            conn = self._connection(Path(db_path))
            return list(conn.execute(query, params).fetchall())

    def get_db_path_by_username(self, username: str) -> list[Path]:
        return sorted(self._message_username_map.get(username, set()))

    def get_all_db_files(self) -> list[Path]:
        return sorted(
            path
            for account in self._accounts
            for path in account.databases.values()
            if path.suffix == ".db"
        )

    def get_db_tables(self, db_path: Path) -> list[str]:
        return [
            row[0]
            for row in self.execute_query(
                db_path,
                "select name from sqlite_master where type='table' order by name",
            )
        ]

    def check_new_messages(self) -> list[tuple[str, tuple]]:
        with self._lock:
            if not self.is_available:
                return []
            self._refresh_message_maps_if_needed()
            messages: list[tuple[str, tuple]] = []
            for db_path, table_name, _username in self._message_tables():
                try:
                    rows = self._fetch_new_rows(db_path, table_name)
                except Exception as exc:
                    self.last_error = f"{table_name}: {type(exc).__name__}: {exc}"
                    self.logger.debug("check_new_messages failed", exc_info=True)
                    continue
                messages.extend((table_name, row) for row in rows)
            messages.sort(key=lambda item: (item[1][3] or 0, item[1][5] or 0))
            return messages

    def get_message_by_server_id(
        self,
        server_id: str,
        message_db_path: Path,
        username: str,
    ) -> Optional[tuple]:
        table = self._table_for_username(Path(message_db_path), username)
        if not table:
            return None
        sql = f"select {self._message_column_sql()} from {self._q(table)} where server_id = ? limit 1"
        rows = self.execute_query(Path(message_db_path), sql, (server_id,))
        if not rows:
            return None
        return self._normalize_message_row(rows[0], Path(message_db_path))

    def get_messages_by_username(
        self,
        username: str,
        count: int = 10,
        order: str = "desc",
    ) -> list[tuple]:
        result: list[tuple] = []
        for db_path in self.get_db_path_by_username(username):
            table = self._table_for_username(db_path, username)
            if not table:
                continue
            direction = "asc" if str(order).lower() == "asc" else "desc"
            sql = (
                f"select {self._message_column_sql()} from {self._q(table)} "
                f"order by sort_seq {direction} limit ?"
            )
            result.extend(
                self._normalize_message_row(row, db_path)
                for row in self.execute_query(db_path, sql, (int(count),))
            )
        reverse = str(order).lower() != "asc"
        result.sort(key=lambda row: (row[3] or 0, row[5] or 0), reverse=reverse)
        return result[: int(count)]

    def query_text_messages(
        self,
        username: str,
        limit: int = 10,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        order: str = "desc",
        query: Optional[str] = None,
    ) -> list[tuple]:
        result: list[tuple] = []
        direction = "asc" if str(order).lower() == "asc" else "desc"
        start_ts = self._normalize_timestamp(start_timestamp)
        end_ts = self._normalize_timestamp(end_timestamp)
        for db_path in self.get_db_path_by_username(username):
            table = self._table_for_username(db_path, username)
            if not table:
                continue
            clauses = ["local_type in (1, 2)"]
            params: list[Any] = []
            if start_ts is not None:
                clauses.append("create_time >= ?")
                params.append(start_ts)
            if end_ts is not None:
                clauses.append("create_time <= ?")
                params.append(end_ts)
            if query:
                clauses.append("message_content like ?")
                params.append(f"%{query}%")
            params.append(int(limit))
            sql = (
                f"select message_content, real_sender_id, create_time, server_id "
                f"from {self._q(table)} where {' and '.join(clauses)} "
                f"order by sort_seq {direction} limit ?"
            )
            for content, sender_id, create_time, server_id in self.execute_query(
                db_path, sql, tuple(params)
            ):
                sender = self.get_contact_by_sender_id(sender_id, db_path)
                result.append(
                    (
                        content,
                        sender.username if sender else "",
                        str(db_path),
                        create_time,
                        server_id,
                    )
                )
        reverse = direction == "desc"
        result.sort(key=lambda row: row[3] or 0, reverse=reverse)
        return result[: int(limit)]

    def get_contact_by_username(
        self,
        username: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> Optional[Contact]:
        if not username:
            return None
        return self._contact_by_username.get(username)

    def get_contact_by_sender_id(
        self,
        sender_id: int,
        message_db_path: Optional[Path] = None,
    ) -> Optional[Contact]:
        if sender_id is None:
            return None
        db_path = Path(message_db_path) if message_db_path else None
        if db_path:
            username = self._sender_username_by_id(db_path, int(sender_id))
            if username:
                return self.get_contact_by_username(username)
        return self._contact_by_id.get(int(sender_id))

    def get_contact_by_display_name(self, display_name: str) -> list[Contact]:
        if not display_name:
            return []
        needle = display_name.lower()
        exact: list[Contact] = []
        fuzzy: list[Contact] = []
        for contact in self._contact_by_username.values():
            names = [
                contact.username,
                contact.display_name,
                contact.remark,
                contact.nick_name,
                contact.alias,
            ]
            lowered = [str(name).lower() for name in names if name]
            if needle in lowered:
                exact.append(contact)
            elif any(needle in value for value in lowered):
                fuzzy.append(contact)
        return exact + fuzzy

    def get_room_by_md5(self, username_md5: str) -> Optional[Contact]:
        return self._room_by_md5.get(username_md5)

    def get_room_member_list(self, room_user_name: str) -> list[Contact]:
        room = self._chat_rooms.get(room_user_name)
        if not room:
            return []
        members: list[Contact] = []
        try:
            usernames = room.parsed_member_list
        except Exception:
            return []
        for username in usernames:
            contact = self.get_contact_by_username(username)
            if contact:
                members.append(contact)
        return members

    def get_room_member_count_by_name(self, room_name: str) -> int:
        rooms = [contact for contact in self.get_contact_by_display_name(room_name) if contact.is_chatroom]
        if not rooms:
            return -1
        return len(self.get_room_member_list(rooms[0].username))

    def check_member_in_room(self, room_id: int, member_id: int) -> bool:
        contact_db = self._db("contact/contact.db")
        if not contact_db:
            return False
        rows = self.execute_query(
            contact_db,
            "select 1 from chatroom_member where room_id = ? and member_id = ? limit 1",
            (room_id, member_id),
        )
        return bool(rows)

    def get_fmessage_list(self) -> list[FMessage]:
        return []

    def load_contacts(self) -> None:
        contact_db = self._db("contact/contact.db")
        if not contact_db:
            return
        try:
            rows = self.execute_query(contact_db, "select * from contact")
        except Exception as exc:
            self.last_error = f"load_contacts: {type(exc).__name__}: {exc}"
            return
        self._contact_by_username.clear()
        self._contact_by_id.clear()
        for row in rows:
            try:
                contact = Contact.from_db_row(row)
            except Exception:
                continue
            self._contact_by_username[contact.username] = contact
            self._contact_by_id[contact.id] = contact
        self.logger.info("Loaded %s contacts from Linux WeChat DB", len(self._contact_by_username))

    def load_chat_rooms(self) -> None:
        contact_db = self._db("contact/contact.db")
        if not contact_db:
            return
        self._room_by_md5.clear()
        self._chat_rooms.clear()
        try:
            rows = self.execute_query(contact_db, "select * from chat_room")
        except Exception:
            return
        for row in rows:
            try:
                room = ChatRoom.from_db_row(row)
            except Exception:
                continue
            self._chat_rooms[room.username] = room
            contact = self.get_contact_by_username(room.username)
            if contact:
                self._room_by_md5[room.username_md5] = contact

    def load_message_username_map(self) -> None:
        existing_sequences = set(self._table_sequences)
        self._message_username_map.clear()
        self._table_username_map.clear()
        for account in self._accounts_for_queries():
            for db_path in sorted(account.db_storage_dir.glob("message/message_*.db")):
                if not self._has_key(db_path):
                    continue
                try:
                    rows = self.execute_query(db_path, "select rowid, user_name from Name2Id")
                    tables = set(self.get_db_tables(db_path))
                except Exception:
                    continue
                for _rowid, username in rows:
                    if not username:
                        continue
                    table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
                    if table_name not in tables:
                        continue
                    self._message_username_map.setdefault(username, set()).add(db_path)
                    self._table_username_map[(db_path, table_name)] = username
                    if existing_sequences and (db_path, table_name) not in existing_sequences:
                        self._table_sequences[(db_path, table_name)] = self._max_local_id(
                            db_path, table_name
                        )
        self._last_message_map_refresh = time.time()

    def get_image_by_md5(self, md5: str) -> Optional[tuple]:
        return self._hardlink_by_md5("image_hardlink_info_v4", md5)

    def get_video_by_md5(self, md5: str) -> Optional[tuple]:
        return self._hardlink_by_md5("video_hardlink_info_v4", md5)

    def get_file_by_md5(self, md5: str) -> Optional[tuple]:
        return self._hardlink_by_md5("file_hardlink_info_v4", md5)

    def get_video(self, md5: str, thumb: bool = False) -> Optional[Path]:
        if not md5:
            return None
        row = self.get_video_by_md5(md5)
        if row:
            rel = self._hardlink_relative_path(row, "Video", thumb=thumb)
            if rel:
                return rel
        account = self._primary_account
        if not account:
            return None
        for candidate in account.account_dir.glob(f"msg/video/*/{md5}*"):
            if thumb and "thumb" not in candidate.name:
                continue
            if not thumb and "thumb" in candidate.name:
                continue
            return candidate.relative_to(account.account_dir)
        return None

    def get_image(
        self,
        xml_content: str,
        message: Any,
        up_dir: str = "",
        md5: Optional[str] = None,
        thumb: bool = False,
        sender_wxid: str = "",
    ) -> Optional[Path]:
        if isinstance(message, str) and hasattr(up_dir, "create_time"):
            xml_content, md5, message, up_dir, thumb, sender_wxid = (
                "",
                message,
                up_dir,
                str(md5 or ""),
                bool(thumb),
                str(sender_wxid or ""),
            )
        image_md5 = md5 or get_md5_from_xml(xml_content, "img")
        if image_md5:
            row = self.get_image_by_md5(image_md5)
            if row:
                rel = self._hardlink_relative_path(row, "Img", thumb=thumb)
                if rel:
                    return rel
        file_name = str(getattr(message, "file_name", "") or "")
        if file_name:
            row = self._hardlink_by_file_name("image_hardlink_info_v4", file_name)
            if row:
                rel = self._hardlink_relative_path(row, "Img", thumb=thumb)
                if rel:
                    return rel
            rel = self._image_by_filename(file_name, message, sender_wxid, thumb=thumb)
            if rel:
                return rel
        if thumb:
            return self.get_image_thumb(message, sender_wxid)
        return self.get_image_by_time(message, sender_wxid)

    def get_image_thumb(self, message: Any, sender_wxid: str) -> Optional[Path]:
        path = self.get_image_by_time(message, sender_wxid)
        if not path:
            return None
        candidates = [
            path.with_name(f"{path.stem}_t{path.suffix}"),
            path.with_name(f"{path.stem}_t.dat"),
            path.with_name(f"{path.name}_t"),
        ]
        return self._first_existing_relative(candidates) or candidates[0]

    def get_image_by_time(self, message: Any, sender_wxid: str) -> Optional[Path]:
        account = self._primary_account
        if not account or not message:
            return None
        create_time = int(getattr(message, "create_time", 0) or 0)
        local_id = str(getattr(message, "local_id", "") or "")
        month = ""
        try:
            month = getattr(message, "str_time")[:7]
        except Exception:
            pass
        session = sender_wxid or getattr(getattr(message, "room", None), "username", "")
        session_md5 = hashlib.md5(session.encode()).hexdigest() if session else ""
        base_parts = [Path("msg/attach")]
        if session_md5:
            base_parts.append(Path(session_md5))
        if month:
            base_parts.append(Path(month))
        base_parts.append(Path("Img"))
        base = Path(*base_parts)
        candidates: list[Path] = []
        if local_id and create_time:
            candidates.append(base / f"{local_id}_{create_time}.dat")
        if create_time:
            attach_root = account.account_dir / "msg" / "attach"
            candidates.extend(
                path.relative_to(account.account_dir)
                for path in sorted(attach_root.glob(f"**/*_{create_time}.dat"))
            )
        return self._first_existing_relative(candidates)

    def get_file(self, md5: str) -> Optional[Path]:
        row = self.get_file_by_md5(md5)
        if row:
            return self._hardlink_relative_path(row, "File")
        return None

    def get_emoji_url(self, md5: str, thumb: bool = False) -> str:
        db_path = self._db("emoticon/emoticon.db")
        if not db_path or not md5:
            return ""
        column = "thumb_url" if thumb else "cdn_url"
        try:
            rows = self.execute_query(
                db_path,
                f"select {column}, thumb_url, cdn_url from kNonStoreEmoticonTable where md5 = ? limit 1",
                (md5,),
            )
        except Exception:
            return ""
        if not rows:
            return ""
        primary, thumb_url, cdn_url = rows[0]
        return primary or thumb_url or cdn_url or ""

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "available": self.is_available,
            "driver": self.driver_name,
            "root": str(self.root),
            "account_count": len(self._accounts),
            "primary_account": self._mask(self._primary_account.account_id) if self._primary_account else "",
            "data_dir": str(self._primary_account.account_dir) if self._primary_account else "",
            "db_count": len(self.get_all_db_files()),
            "key_count": len(self._keys),
            "open_connections": len(self._connections),
            "message_tables": len(self._message_tables()),
            "contacts": len(self._contact_by_username),
            "rooms": len(self._room_by_md5),
            "last_scan_at": int(self.last_scan_at) if self.last_scan_at else 0,
            "key_scan": self._scan_result.redacted(),
            "last_error": self.last_error,
        }

    def _discover_accounts(self) -> None:
        report = LinuxDatabaseDiscovery(self.root).scan()
        self._discovery_report = report
        accounts: list[LinuxDatabaseAccount] = []
        for storage in report.accounts:
            databases: dict[str, Path] = {}
            for path in storage.encrypted_databases + storage.plaintext_databases:
                databases[str(path.relative_to(storage.db_storage_dir))] = path
            accounts.append(
                LinuxDatabaseAccount(
                    account_dir=storage.account_dir,
                    account_id=storage.account_id,
                    suffix=storage.storage_suffix,
                    db_storage_dir=storage.db_storage_dir,
                    databases=databases,
                )
            )
        self._accounts = accounts

    def _select_primary_account(self) -> None:
        if not self._accounts:
            self._primary_account = None
            return
        configured = self.active_account or self.user_info.account
        if configured:
            for account in self._accounts:
                if account.account_id == configured or account.account_dir.name.startswith(f"{configured}_"):
                    self._primary_account = account
                    return
        scored = []
        for account in self._accounts:
            count = 0
            for db_path in sorted(account.db_storage_dir.glob("message/message_*.db")):
                if not self._has_key(db_path):
                    continue
                try:
                    count += sum(
                        row[0]
                        for row in self.execute_query(
                            db_path,
                            "select count(*) from sqlite_master where type='table' and name like 'Msg_%'",
                        )
                    )
                except Exception:
                    pass
            scored.append((count, account.account_dir.stat().st_mtime, account))
        self._primary_account = sorted(scored, reverse=True, key=lambda item: (item[0], item[1]))[0][2]

    def _accounts_for_queries(self) -> list[LinuxDatabaseAccount]:
        return [self._primary_account] if self._primary_account else []

    def _db(self, relative_path: str) -> Optional[Path]:
        for account in self._accounts_for_queries():
            path = account.databases.get(relative_path)
            if path and self._has_key(path):
                return path
        return None

    def _has_key(self, path: Path) -> bool:
        return Path(path) in self._keys

    def _connection(self, db_path: Path) -> Any:
        db_path = Path(db_path)
        if db_path in self._connections:
            return self._connections[db_path]
        key = self._keys.get(db_path)
        if not key:
            raise RuntimeError(f"No SQLCipher key for {db_path}")
        try:
            conn = self._driver.connect(str(db_path), check_same_thread=False)
        except TypeError:
            conn = self._driver.connect(str(db_path))
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.execute("PRAGMA busy_timeout = 1000")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("select count(*) from sqlite_master").fetchone()
        self._connections[db_path] = conn
        return conn

    def _prime_message_sequences(self) -> None:
        self._table_sequences.clear()
        for db_path, table_name, _username in self._message_tables():
            self._table_sequences[(db_path, table_name)] = self._max_local_id(
                db_path, table_name
            )

    def _max_local_id(self, db_path: Path, table_name: str) -> int:
        try:
            return int(
                self.execute_query(
                    db_path,
                    f"select coalesce(max(local_id), 0) from {self._q(table_name)}",
                )[0][0]
                or 0
            )
        except Exception:
            return 0

    def _refresh_message_maps_if_needed(self) -> None:
        if self.message_map_refresh_interval <= 0:
            return
        now = time.time()
        if now - self._last_message_map_refresh < self.message_map_refresh_interval:
            return
        self.load_message_username_map()

    def _fetch_new_rows(self, db_path: Path, table_name: str) -> list[tuple]:
        last_id = self._table_sequences.get((db_path, table_name), 0)
        sql = (
            f"select {self._message_column_sql()} from {self._q(table_name)} "
            "where local_id > ? order by local_id asc limit 100"
        )
        rows = [
            self._normalize_message_row(row, db_path)
            for row in self.execute_query(db_path, sql, (last_id,))
        ]
        if rows:
            self._table_sequences[(db_path, table_name)] = max(int(row[0]) for row in rows)
        return rows

    def _message_tables(self) -> list[tuple[Path, str, str]]:
        tables: list[tuple[Path, str, str]] = []
        for (db_path, table_name), username in self._table_username_map.items():
            tables.append((db_path, table_name, username))
        return sorted(tables, key=lambda item: (str(item[0]), item[1]))

    def _table_for_username(self, db_path: Path, username: str) -> str:
        table = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        return table if (Path(db_path), table) in self._table_username_map else ""

    def _sender_username_by_id(self, db_path: Path, sender_id: int) -> str:
        try:
            rows = self.execute_query(db_path, "select user_name from Name2Id where rowid = ?", (sender_id,))
        except Exception:
            return ""
        return rows[0][0] if rows else ""

    def _normalize_message_row(self, row: tuple, db_path: Path) -> tuple:
        return tuple(row[:17]) + (str(db_path),)

    def _message_column_sql(self) -> str:
        return ", ".join(self._q(column) for column in MESSAGE_COLUMNS)

    def _hardlink_by_md5(self, table_name: str, md5: str) -> Optional[tuple]:
        db_path = self._db("hardlink/hardlink.db")
        if not db_path or not md5:
            return None
        try:
            rows = self.execute_query(
                db_path,
                f"select md5_hash, md5, type, file_name, file_size, modify_time, dir1, dir2, _rowid_, extra_buffer "
                f"from {self._q(table_name)} where md5 = ? limit 1",
                (md5,),
            )
        except Exception:
            return None
        return rows[0] if rows else None

    def _hardlink_by_file_name(self, table_name: str, file_name: str) -> Optional[tuple]:
        db_path = self._db("hardlink/hardlink.db")
        if not db_path or not file_name:
            return None
        names = [file_name]
        if "." not in Path(file_name).name:
            names.append(f"{file_name}.dat")
        try:
            rows = self.execute_query(
                db_path,
                f"select md5_hash, md5, type, file_name, file_size, modify_time, dir1, dir2, _rowid_, extra_buffer "
                f"from {self._q(table_name)} where file_name in ({','.join('?' for _ in names)}) limit 1",
                tuple(names),
            )
        except Exception:
            return None
        return rows[0] if rows else None

    def _hardlink_relative_path(
        self,
        row: tuple,
        subdir: str,
        *,
        thumb: bool = False,
    ) -> Optional[Path]:
        account = self._primary_account
        if not account:
            return None
        file_name = row[3]
        dir1 = self._hardlink_dir_name(row[6])
        dir2 = self._hardlink_dir_name(row[7])
        if not file_name or not dir1 or not dir2:
            return None
        if thumb and subdir == "Img":
            path = Path("msg/attach") / dir1 / dir2 / subdir / f"{Path(file_name).stem}_t.dat"
            existing = self._first_existing_relative([path])
            if existing:
                return existing
        path = Path("msg/attach") / dir1 / dir2 / subdir / file_name
        existing = self._first_existing_relative([path])
        return existing or path

    def _hardlink_dir_name(self, rowid: int) -> str:
        db_path = self._db("hardlink/hardlink.db")
        if not db_path:
            return ""
        try:
            rows = self.execute_query(db_path, "select username from dir2id where rowid = ?", (rowid,))
        except Exception:
            return ""
        return rows[0][0] if rows else ""

    def _image_by_filename(
        self,
        file_name: str,
        message: Any,
        sender_wxid: str,
        *,
        thumb: bool = False,
    ) -> Optional[Path]:
        account = self._primary_account
        if not account:
            return None
        stem = Path(file_name).stem
        base_names = [file_name]
        if "." not in Path(file_name).name:
            base_names.append(f"{file_name}.dat")
        if thumb:
            names = [f"{stem}_t.dat", f"{file_name}_t"]
        else:
            names = base_names

        month = ""
        try:
            month = getattr(message, "str_time")[:7]
        except Exception:
            pass
        session_md5 = hashlib.md5(sender_wxid.encode()).hexdigest() if sender_wxid else ""
        exact_candidates: list[Path] = []
        if session_md5 and month:
            exact_candidates.extend(
                Path("msg/attach") / session_md5 / month / "Img" / name
                for name in names
            )
        existing = self._first_existing_relative(exact_candidates)
        if existing:
            return existing

        attach_root = account.account_dir / "msg" / "attach"
        for name in names:
            matches = sorted(attach_root.glob(f"**/{name}"))
            if matches:
                return matches[0].relative_to(account.account_dir)
        if not thumb:
            matches = sorted(attach_root.glob(f"**/{stem}*.dat"))
            matches = [path for path in matches if "_t.dat" not in path.name]
            if matches:
                return matches[0].relative_to(account.account_dir)
        return None

    def _first_existing_relative(self, candidates: Iterable[Path]) -> Optional[Path]:
        account = self._primary_account
        if not account:
            return None
        for candidate in candidates:
            if not candidate:
                continue
            path = candidate
            if path.is_absolute():
                try:
                    path.relative_to(account.account_dir)
                    absolute = path
                except ValueError:
                    continue
            else:
                absolute = account.account_dir / path
            if absolute.exists():
                return absolute.relative_to(account.account_dir)
        return None

    def _normalize_timestamp(self, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        value = int(value)
        return value // 1000 if value > 10_000_000_000 else value

    def _q(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _mask(self, value: str) -> str:
        if not value:
            return ""
        if len(value) <= 6:
            return value[:1] + "***"
        return f"{value[:2]}***{value[-4:]}"
