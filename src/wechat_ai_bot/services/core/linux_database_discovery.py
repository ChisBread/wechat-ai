"""
Linux WeChat database discovery.

This module does not decrypt or query message content. It only identifies the
runtime database layout and reports whether encrypted business DBs can be
opened with the current Python environment.
"""

import importlib.util
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class LinuxWeChatAccountStorage:
    account_dir: Path
    account_id: str
    storage_suffix: str
    db_storage_dir: Path
    login_key_db: Optional[Path] = None
    login_key_dat: Optional[Path] = None
    encrypted_databases: list[Path] = field(default_factory=list)
    plaintext_databases: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class LinuxDatabaseDiscoveryReport:
    root: Path
    accounts: list[LinuxWeChatAccountStorage]
    sqlcipher_available: bool

    @property
    def can_read_business_databases(self) -> bool:
        return self.sqlcipher_available


class LinuxDatabaseDiscovery:
    """Discover Linux WeChat DB files under xwechat_files without reading chats."""

    def __init__(self, xwechat_files_root: str | Path = "/config/xwechat_files"):
        self.root = Path(xwechat_files_root)

    def scan(self) -> LinuxDatabaseDiscoveryReport:
        accounts = []
        for db_storage in sorted(self.root.glob("*_*/db_storage")):
            account_dir = db_storage.parent
            account_id, storage_suffix = self._split_account_dir(account_dir.name)
            account = LinuxWeChatAccountStorage(
                account_dir=account_dir,
                account_id=account_id,
                storage_suffix=storage_suffix,
                db_storage_dir=db_storage,
                login_key_db=self._existing_path(
                    self.root / "all_users" / "login" / account_id / "key_info.db"
                ),
                login_key_dat=self._existing_path(
                    Path("/config/.xwechat/login") / account_id / "key_info.dat"
                ),
                encrypted_databases=[
                    db
                    for db in self._database_files(db_storage)
                    if self._database_header(db) != SQLITE_HEADER
                ],
                plaintext_databases=[
                    db
                    for db in self._database_files(db_storage)
                    if self._database_header(db) == SQLITE_HEADER
                ],
            )
            accounts.append(account)

        return LinuxDatabaseDiscoveryReport(
            root=self.root,
            accounts=accounts,
            sqlcipher_available=self._sqlcipher_available(),
        )

    def read_login_key_schema(self, login_key_db: Path) -> dict[str, list[str]]:
        """Return table/column names for the plaintext login key DB."""
        schema: dict[str, list[str]] = {}
        with sqlite3.connect(f"file:{login_key_db}?mode=ro", uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table' order by name"
                )
            ]
            for table in tables:
                schema[table] = [
                    row[1] for row in conn.execute(f"pragma table_info({table})")
                ]
        return schema

    def _database_files(self, root: Path) -> list[Path]:
        return sorted(root.rglob("*.db"))

    def _database_header(self, db_path: Path) -> bytes:
        with db_path.open("rb") as file:
            return file.read(len(SQLITE_HEADER))

    def _existing_path(self, path: Path) -> Optional[Path]:
        return path if path.exists() else None

    def _split_account_dir(self, dirname: str) -> tuple[str, str]:
        account_id, _, suffix = dirname.rpartition("_")
        return account_id or dirname, suffix

    def _sqlcipher_available(self) -> bool:
        if importlib.util.find_spec("pysqlcipher3") is not None:
            return True
        if importlib.util.find_spec("sqlcipher3") is not None:
            return True
        if importlib.util.find_spec("apsw") is not None:
            return True
        return False
