"""Atomic disclosure provenance for stock Goose session databases.

The ledger is project-owned SQLite state. Static triggers capture the last form of a
managed session row while stock Goose still marks it agent-visible. No trigger infers
that an already invisible row was previously disclosed.
"""

from __future__ import annotations

import hashlib
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LEDGER_SCHEMA_VERSION: Final = 1

DEFAULT_MAX_LEDGER_ENTRIES: Final = 65_536
DEFAULT_MAX_LEDGER_BYTES: Final = 256 * 1024 * 1024
DEFAULT_MAX_CONTENT_BYTES: Final = 512 * 1024
DEFAULT_MAX_METADATA_BYTES: Final = 64 * 1024
DEFAULT_MAX_MESSAGE_ID_BYTES: Final = 4 * 1024
DEFAULT_MAX_ROLE_BYTES: Final = 256

HARD_MAX_LEDGER_ENTRIES: Final = DEFAULT_MAX_LEDGER_ENTRIES
HARD_MAX_LEDGER_BYTES: Final = DEFAULT_MAX_LEDGER_BYTES
HARD_MAX_CONTENT_BYTES: Final = DEFAULT_MAX_CONTENT_BYTES
HARD_MAX_METADATA_BYTES: Final = DEFAULT_MAX_METADATA_BYTES
HARD_MAX_MESSAGE_ID_BYTES: Final = DEFAULT_MAX_MESSAGE_ID_BYTES
HARD_MAX_ROLE_BYTES: Final = DEFAULT_MAX_ROLE_BYTES

OMITTED_MESSAGE_ID: Final = 1
OMITTED_ROLE: Final = 2
OMITTED_CONTENT: Final = 4
OMITTED_METADATA: Final = 8

OBJECT_PREFIX: Final = "sandboxed_goose_disclosure_"
META_TABLE: Final = "sandboxed_goose_disclosure_meta"
MANAGED_TABLE: Final = "sandboxed_goose_disclosure_managed_sessions"
ENTRY_TABLE: Final = "sandboxed_goose_disclosure_entries"
ACCOUNTING_TABLE: Final = "sandboxed_goose_disclosure_accounting"


class DisclosureLedgerError(RuntimeError):
    """The disclosure ledger cannot safely establish or verify provenance."""


class DisclosureLedgerUnavailable(DisclosureLedgerError):
    """The database, schema, session, or accounting state is unavailable."""


class DisclosureLedgerOverflow(DisclosureLedgerError):
    """A configured ledger row or byte quota would be exceeded."""


@dataclass(frozen=True, slots=True)
class LedgerLimits:
    """Hard limits installed for one managed Goose session."""

    max_entries: int = DEFAULT_MAX_LEDGER_ENTRIES
    max_stored_bytes: int = DEFAULT_MAX_LEDGER_BYTES
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES
    max_message_id_bytes: int = DEFAULT_MAX_MESSAGE_ID_BYTES
    max_role_bytes: int = DEFAULT_MAX_ROLE_BYTES

    def __post_init__(self) -> None:
        limits = (
            ("max_entries", self.max_entries, HARD_MAX_LEDGER_ENTRIES),
            ("max_stored_bytes", self.max_stored_bytes, HARD_MAX_LEDGER_BYTES),
            ("max_content_bytes", self.max_content_bytes, HARD_MAX_CONTENT_BYTES),
            ("max_metadata_bytes", self.max_metadata_bytes, HARD_MAX_METADATA_BYTES),
            ("max_message_id_bytes", self.max_message_id_bytes, HARD_MAX_MESSAGE_ID_BYTES),
            ("max_role_bytes", self.max_role_bytes, HARD_MAX_ROLE_BYTES),
        )
        for name, value, hard_maximum in limits:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 1 <= value <= hard_maximum:
                raise ValueError(f"{name} must be between 1 and {hard_maximum}")


@dataclass(frozen=True, slots=True)
class LedgerStatus:
    """Non-sensitive status for one verified managed session."""

    database: Path
    session_id: str
    schema_version: int
    schema_fingerprint: str
    coverage_epoch: int
    coverage_complete: bool
    coverage_reason: str
    ambiguous_rows_at_bootstrap: int
    deletion_events: int
    ledger_entries: int
    stored_bytes: int
    omitted_entries: int
    limits: LedgerLimits


def _normalize_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";")


def _byte_length(row: str, column: str) -> str:
    return f"length(CAST(COALESCE({row}.{column}, '') AS BLOB))"


def _visible(row: str) -> str:
    return (
        f"CASE WHEN json_valid({row}.metadata_json) "
        f"THEN COALESCE(json_extract({row}.metadata_json, '$.agentVisible'), 0) "
        "ELSE 0 END = 1"
    )


def _omission_flags(row: str, managed: str) -> str:
    message_id_bytes = _byte_length(row, "message_id")
    role_bytes = _byte_length(row, "role")
    content_bytes = _byte_length(row, "content_json")
    metadata_bytes = _byte_length(row, "metadata_json")
    return (
        f"(CASE WHEN {row}.message_id IS NOT NULL "
        f"AND {message_id_bytes} > {managed}.max_message_id_bytes "
        f"THEN {OMITTED_MESSAGE_ID} ELSE 0 END) + "
        f"(CASE WHEN {role_bytes} > {managed}.max_role_bytes "
        f"THEN {OMITTED_ROLE} ELSE 0 END) + "
        f"(CASE WHEN {content_bytes} > {managed}.max_content_bytes "
        f"THEN {OMITTED_CONTENT} ELSE 0 END) + "
        f"(CASE WHEN {row}.metadata_json IS NOT NULL "
        f"AND {metadata_bytes} > {managed}.max_metadata_bytes "
        f"THEN {OMITTED_METADATA} ELSE 0 END)"
    )


def _stored_bytes(row: str, managed: str) -> str:
    fields = (
        ("message_id", "max_message_id_bytes"),
        ("role", "max_role_bytes"),
        ("content_json", "max_content_bytes"),
        ("metadata_json", "max_metadata_bytes"),
    )
    terms = [
        (
            f"CASE WHEN {row}.{column} IS NOT NULL "
            f"AND {_byte_length(row, column)} <= {managed}.{limit_column} "
            f"THEN {_byte_length(row, column)} ELSE 0 END"
        )
        for column, limit_column in fields
    ]
    return " + ".join(f"({term})" for term in terms)


def _bounded_value(row: str, column: str, managed: str, limit_column: str) -> str:
    return (
        f"CASE WHEN {row}.{column} IS NULL THEN NULL "
        f"WHEN {_byte_length(row, column)} <= {managed}.{limit_column} "
        f"THEN {row}.{column} ELSE NULL END"
    )


def _capture_statement(row: str, reason: str, *, seed: bool = False) -> str:
    message_id_bytes = _byte_length(row, "message_id")
    role_bytes = _byte_length(row, "role")
    content_bytes = _byte_length(row, "content_json")
    metadata_bytes = _byte_length(row, "metadata_json")
    if seed:
        source = f"""
        FROM messages AS {row}
        JOIN {MANAGED_TABLE} AS managed ON managed.session_id = {row}.session_id
        WHERE {row}.session_id = ?
          AND {_visible(row)}
        """
    else:
        source = f"""
        FROM {MANAGED_TABLE} AS managed
        WHERE managed.session_id = {row}.session_id
        """
    return f"""
        INSERT INTO {ENTRY_TABLE} (
            session_id, coverage_epoch, source_row_id, message_id, role,
            created_timestamp, content_json, metadata_json,
            source_message_id_bytes, source_role_bytes, source_content_bytes,
            source_metadata_bytes, stored_bytes, omission_flags, capture_reason,
            ledger_schema_version
        )
        SELECT
            {row}.session_id,
            managed.coverage_epoch,
            {row}.id,
            {_bounded_value(row, 'message_id', 'managed', 'max_message_id_bytes')},
            {_bounded_value(row, 'role', 'managed', 'max_role_bytes')},
            {row}.created_timestamp,
            {_bounded_value(row, 'content_json', 'managed', 'max_content_bytes')},
            {_bounded_value(row, 'metadata_json', 'managed', 'max_metadata_bytes')},
            {message_id_bytes},
            {role_bytes},
            {content_bytes},
            {metadata_bytes},
            {_stored_bytes(row, 'managed')},
            {_omission_flags(row, 'managed')},
            '{reason}',
            {LEDGER_SCHEMA_VERSION}
        {source}
        ON CONFLICT(session_id, coverage_epoch, source_row_id) DO UPDATE SET
            message_id = excluded.message_id,
            role = excluded.role,
            created_timestamp = excluded.created_timestamp,
            content_json = excluded.content_json,
            metadata_json = excluded.metadata_json,
            source_message_id_bytes = excluded.source_message_id_bytes,
            source_role_bytes = excluded.source_role_bytes,
            source_content_bytes = excluded.source_content_bytes,
            source_metadata_bytes = excluded.source_metadata_bytes,
            stored_bytes = excluded.stored_bytes,
            omission_flags = excluded.omission_flags,
            capture_reason = excluded.capture_reason,
            ledger_schema_version = excluded.ledger_schema_version;
    """


_TABLE_DEFINITIONS: Final[dict[str, str]] = {
    META_TABLE: f"""
        CREATE TABLE {META_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL CHECK (schema_version = {LEDGER_SCHEMA_VERSION}),
            schema_fingerprint TEXT NOT NULL CHECK (length(schema_fingerprint) = 64)
        )
    """,
    MANAGED_TABLE: f"""
        CREATE TABLE {MANAGED_TABLE} (
            session_id TEXT PRIMARY KEY,
            ledger_schema_version INTEGER NOT NULL
                CHECK (ledger_schema_version = {LEDGER_SCHEMA_VERSION}),
            coverage_epoch INTEGER NOT NULL DEFAULT 1 CHECK (coverage_epoch >= 1),
            coverage_complete INTEGER NOT NULL CHECK (coverage_complete IN (0, 1)),
            coverage_reason TEXT NOT NULL CHECK (
                coverage_reason IN (
                    'bootstrap-complete', 'preexisting-ambiguous-rows',
                    'message-delete', 'message-session-move', 'session-delete'
                )
            ),
            ambiguous_rows_at_bootstrap INTEGER NOT NULL
                CHECK (ambiguous_rows_at_bootstrap >= 0),
            deletion_events INTEGER NOT NULL DEFAULT 0 CHECK (deletion_events >= 0),
            max_entries INTEGER NOT NULL
                CHECK (max_entries BETWEEN 1 AND {HARD_MAX_LEDGER_ENTRIES}),
            max_stored_bytes INTEGER NOT NULL
                CHECK (max_stored_bytes BETWEEN 1 AND {HARD_MAX_LEDGER_BYTES}),
            max_content_bytes INTEGER NOT NULL
                CHECK (max_content_bytes BETWEEN 1 AND {HARD_MAX_CONTENT_BYTES}),
            max_metadata_bytes INTEGER NOT NULL
                CHECK (max_metadata_bytes BETWEEN 1 AND {HARD_MAX_METADATA_BYTES}),
            max_message_id_bytes INTEGER NOT NULL
                CHECK (max_message_id_bytes BETWEEN 1 AND {HARD_MAX_MESSAGE_ID_BYTES}),
            max_role_bytes INTEGER NOT NULL
                CHECK (max_role_bytes BETWEEN 1 AND {HARD_MAX_ROLE_BYTES})
        )
    """,
    ENTRY_TABLE: f"""
        CREATE TABLE {ENTRY_TABLE} (
            session_id TEXT NOT NULL,
            coverage_epoch INTEGER NOT NULL CHECK (coverage_epoch >= 1),
            source_row_id INTEGER NOT NULL CHECK (source_row_id >= 1),
            message_id TEXT,
            role TEXT,
            created_timestamp INTEGER NOT NULL,
            content_json TEXT,
            metadata_json TEXT,
            source_message_id_bytes INTEGER NOT NULL CHECK (source_message_id_bytes >= 0),
            source_role_bytes INTEGER NOT NULL CHECK (source_role_bytes >= 0),
            source_content_bytes INTEGER NOT NULL CHECK (source_content_bytes >= 0),
            source_metadata_bytes INTEGER NOT NULL CHECK (source_metadata_bytes >= 0),
            stored_bytes INTEGER NOT NULL CHECK (stored_bytes >= 0),
            omission_flags INTEGER NOT NULL CHECK (omission_flags BETWEEN 0 AND 15),
            capture_reason TEXT NOT NULL CHECK (
                capture_reason IN ('bootstrap', 'visible-insert', 'visible-update', 'pre-archive')
            ),
            ledger_schema_version INTEGER NOT NULL
                CHECK (ledger_schema_version = {LEDGER_SCHEMA_VERSION}),
            PRIMARY KEY (session_id, coverage_epoch, source_row_id),
            FOREIGN KEY (session_id) REFERENCES {MANAGED_TABLE}(session_id)
        )
    """,
    ACCOUNTING_TABLE: f"""
        CREATE TABLE {ACCOUNTING_TABLE} (
            session_id TEXT PRIMARY KEY,
            entry_count INTEGER NOT NULL DEFAULT 0 CHECK (entry_count >= 0),
            stored_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
            omitted_entries INTEGER NOT NULL DEFAULT 0 CHECK (omitted_entries >= 0),
            FOREIGN KEY (session_id) REFERENCES {MANAGED_TABLE}(session_id)
        )
    """,
}

_TRIGGER_DEFINITIONS: Final[dict[str, str]] = {
    "sandboxed_goose_disclosure_entry_insert_quota": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_entry_insert_quota
        BEFORE INSERT ON {ENTRY_TABLE}
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM {MANAGED_TABLE} AS managed
                JOIN {ACCOUNTING_TABLE} AS accounting
                    ON accounting.session_id = managed.session_id
                WHERE managed.session_id = NEW.session_id
            ) THEN RAISE(ABORT, 'sandboxed_goose_ledger_unavailable') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM {MANAGED_TABLE} AS managed
                JOIN {ACCOUNTING_TABLE} AS accounting
                    ON accounting.session_id = managed.session_id
                WHERE managed.session_id = NEW.session_id
                  AND (
                      accounting.entry_count + CASE WHEN EXISTS (
                          SELECT 1 FROM {ENTRY_TABLE} AS existing
                          WHERE existing.session_id = NEW.session_id
                            AND existing.coverage_epoch = NEW.coverage_epoch
                            AND existing.source_row_id = NEW.source_row_id
                      ) THEN 0 ELSE 1 END > managed.max_entries
                      OR accounting.stored_bytes + NEW.stored_bytes - COALESCE((
                          SELECT existing.stored_bytes FROM {ENTRY_TABLE} AS existing
                          WHERE existing.session_id = NEW.session_id
                            AND existing.coverage_epoch = NEW.coverage_epoch
                            AND existing.source_row_id = NEW.source_row_id
                      ), 0)
                          > managed.max_stored_bytes
                  )
            ) THEN RAISE(ABORT, 'sandboxed_goose_ledger_overflow') END;
        END
    """,
    "sandboxed_goose_disclosure_entry_insert_account": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_entry_insert_account
        AFTER INSERT ON {ENTRY_TABLE}
        BEGIN
            UPDATE {ACCOUNTING_TABLE}
            SET entry_count = entry_count + 1,
                stored_bytes = stored_bytes + NEW.stored_bytes,
                omitted_entries = omitted_entries + CASE
                    WHEN NEW.omission_flags != 0 THEN 1 ELSE 0 END
            WHERE session_id = NEW.session_id;
        END
    """,
    "sandboxed_goose_disclosure_entry_update_quota": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_entry_update_quota
        BEFORE UPDATE ON {ENTRY_TABLE}
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM {MANAGED_TABLE} AS managed
                JOIN {ACCOUNTING_TABLE} AS accounting
                    ON accounting.session_id = managed.session_id
                WHERE managed.session_id = NEW.session_id
            ) THEN RAISE(ABORT, 'sandboxed_goose_ledger_unavailable') END;
            SELECT CASE WHEN
                NEW.session_id != OLD.session_id
                OR NEW.coverage_epoch != OLD.coverage_epoch
                OR NEW.source_row_id != OLD.source_row_id
            THEN RAISE(ABORT, 'sandboxed_goose_ledger_identity_immutable') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM {MANAGED_TABLE} AS managed
                JOIN {ACCOUNTING_TABLE} AS accounting
                    ON accounting.session_id = managed.session_id
                WHERE managed.session_id = NEW.session_id
                  AND accounting.stored_bytes - OLD.stored_bytes + NEW.stored_bytes
                      > managed.max_stored_bytes
            ) THEN RAISE(ABORT, 'sandboxed_goose_ledger_overflow') END;
        END
    """,
    "sandboxed_goose_disclosure_entry_update_account": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_entry_update_account
        AFTER UPDATE ON {ENTRY_TABLE}
        BEGIN
            UPDATE {ACCOUNTING_TABLE}
            SET stored_bytes = stored_bytes - OLD.stored_bytes + NEW.stored_bytes,
                omitted_entries = omitted_entries
                    - CASE WHEN OLD.omission_flags != 0 THEN 1 ELSE 0 END
                    + CASE WHEN NEW.omission_flags != 0 THEN 1 ELSE 0 END
            WHERE session_id = NEW.session_id;
        END
    """,
    "sandboxed_goose_disclosure_entry_no_delete": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_entry_no_delete
        BEFORE DELETE ON {ENTRY_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'sandboxed_goose_ledger_append_only');
        END
    """,
    "sandboxed_goose_disclosure_visible_insert": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_visible_insert
        AFTER INSERT ON messages
        WHEN {_visible('NEW')}
          AND EXISTS (
              SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = NEW.session_id
          )
        BEGIN
            {_capture_statement('NEW', 'visible-insert')}
        END
    """,
    "sandboxed_goose_disclosure_visible_update": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_visible_update
        AFTER UPDATE OF message_id, session_id, role, content_json,
                        created_timestamp, metadata_json ON messages
        WHEN {_visible('NEW')}
          AND EXISTS (
              SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = NEW.session_id
          )
        BEGIN
            {_capture_statement('NEW', 'visible-update')}
        END
    """,
    "sandboxed_goose_disclosure_pre_archive": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_pre_archive
        BEFORE UPDATE OF session_id, metadata_json ON messages
        WHEN OLD.session_id = NEW.session_id
          AND {_visible('OLD')}
          AND NOT ({_visible('NEW')})
          AND EXISTS (
              SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = OLD.session_id
          )
        BEGIN
            {_capture_statement('OLD', 'pre-archive')}
        END
    """,
    "sandboxed_goose_disclosure_message_delete": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_message_delete
        BEFORE DELETE ON messages
        WHEN EXISTS (
            SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = OLD.session_id
        )
        BEGIN
            UPDATE {MANAGED_TABLE}
            SET coverage_epoch = coverage_epoch + 1,
                coverage_complete = 0,
                coverage_reason = 'message-delete',
                deletion_events = deletion_events + 1
            WHERE session_id = OLD.session_id;
        END
    """,
    "sandboxed_goose_disclosure_session_move": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_session_move
        BEFORE UPDATE OF session_id ON messages
        WHEN OLD.session_id != NEW.session_id
          AND EXISTS (
              SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = OLD.session_id
          )
        BEGIN
            UPDATE {MANAGED_TABLE}
            SET coverage_epoch = coverage_epoch + 1,
                coverage_complete = 0,
                coverage_reason = 'message-session-move',
                deletion_events = deletion_events + 1
            WHERE session_id = OLD.session_id;
        END
    """,
    "sandboxed_goose_disclosure_session_delete": f"""
        CREATE TRIGGER sandboxed_goose_disclosure_session_delete
        BEFORE DELETE ON sessions
        WHEN EXISTS (
            SELECT 1 FROM {MANAGED_TABLE} WHERE session_id = OLD.id
        )
        BEGIN
            UPDATE {MANAGED_TABLE}
            SET coverage_epoch = coverage_epoch + 1,
                coverage_complete = 0,
                coverage_reason = 'session-delete',
                deletion_events = deletion_events + 1
            WHERE session_id = OLD.id;
        END
    """,
}

_OBJECT_DEFINITIONS: Final[dict[str, tuple[str, str]]] = {
    **{name: ("table", sql) for name, sql in _TABLE_DEFINITIONS.items()},
    **{name: ("trigger", sql) for name, sql in _TRIGGER_DEFINITIONS.items()},
}
SCHEMA_FINGERPRINT: Final = hashlib.sha256(
    "\n".join(
        f"{name}\0{object_type}\0{_normalize_sql(sql)}"
        for name, (object_type, sql) in sorted(_OBJECT_DEFINITIONS.items())
    ).encode("utf-8")
).hexdigest()


def bootstrap_disclosure_ledger(
    database: Path,
    session_id: str,
    *,
    limits: LedgerLimits | None = None,
) -> LedgerStatus:
    """Atomically install/verify the ledger and register exactly one session."""

    active_limits = limits if limits is not None else LedgerLimits()
    resolved = _resolve_database(database)
    _validate_session_id(session_id)
    connection: sqlite3.Connection | None = None
    installed = False
    try:
        connection = sqlite3.connect(resolved, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        _validate_stock_schema(connection)
        owned = _read_owned_objects(connection)
        if not owned:
            _create_schema(connection)
            installed = True
        else:
            _verify_schema_objects(owned)
        _verify_meta(connection)
        _require_session(connection, session_id)
        managed = connection.execute(
            f"SELECT * FROM {MANAGED_TABLE} WHERE session_id = ?", (session_id,)
        ).fetchone()
        if managed is None:
            _register_session(connection, session_id, active_limits)
        else:
            _require_limits(managed, active_limits)
        status = _read_status(connection, resolved, session_id)
        connection.commit()
        return status
    except DisclosureLedgerError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.rollback()
        if "sandboxed_goose_ledger_overflow" in str(error):
            raise DisclosureLedgerOverflow(
                "disclosure ledger quota cannot cover the managed session"
            ) from error
        action = "install" if installed else "verify"
        raise DisclosureLedgerUnavailable(f"cannot {action} disclosure ledger: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def verify_disclosure_ledger(database: Path, session_id: str) -> LedgerStatus:
    """Verify all owned objects and accounting without repairing any state."""

    resolved = _resolve_database(database)
    _validate_session_id(session_id)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN")
        _validate_stock_schema(connection)
        _verify_schema_objects(_read_owned_objects(connection))
        _verify_meta(connection)
        _require_session(connection, session_id)
        status = _read_status(connection, resolved, session_id)
        connection.rollback()
        return status
    except DisclosureLedgerError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise DisclosureLedgerUnavailable(f"cannot verify disclosure ledger: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _resolve_database(database: Path) -> Path:
    try:
        resolved = database.expanduser().resolve(strict=True)
        details = resolved.stat()
    except OSError as error:
        raise DisclosureLedgerUnavailable(f"cannot inspect Goose session database: {error}") from error
    if not stat.S_ISREG(details.st_mode):
        raise DisclosureLedgerUnavailable("Goose session database is not a regular file")
    return resolved


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
        raise DisclosureLedgerUnavailable("Goose session ID must be a non-empty trimmed string")
    try:
        encoded = session_id.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DisclosureLedgerUnavailable("Goose session ID is not valid UTF-8") from error
    if len(encoded) > 256:
        raise DisclosureLedgerUnavailable("Goose session ID exceeds 256 UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in session_id):
        raise DisclosureLedgerUnavailable("Goose session ID contains a control character")


def _validate_stock_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "id": ("INTEGER", 0, 1),
        "message_id": ("TEXT", 0, 0),
        "session_id": ("TEXT", 1, 0),
        "role": ("TEXT", 1, 0),
        "content_json": ("TEXT", 1, 0),
        "created_timestamp": ("INTEGER", 1, 0),
        "metadata_json": ("TEXT", 0, 0),
    }
    columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute("PRAGMA table_info(messages)")
    }
    if any(columns.get(name) != shape for name, shape in expected.items()):
        raise DisclosureLedgerUnavailable("unsupported stock Goose messages schema")
    messages_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
    ).fetchone()
    messages_sql = _normalize_sql(str(messages_sql_row[0] if messages_sql_row else "")).upper()
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in messages_sql:
        raise DisclosureLedgerUnavailable(
            "stock Goose messages IDs are not verified as AUTOINCREMENT"
        )
    foreign_keys = connection.execute("PRAGMA foreign_key_list(messages)").fetchall()
    if not any(
        str(row[2]) == "sessions" and str(row[3]) == "session_id" and str(row[4]) == "id"
        for row in foreign_keys
    ):
        raise DisclosureLedgerUnavailable("stock Goose messages session binding is unsupported")
    sessions = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute("PRAGMA table_info(sessions)")
    }
    if sessions.get("id") != ("TEXT", 0, 1):
        raise DisclosureLedgerUnavailable("unsupported stock Goose sessions schema")
    try:
        if connection.execute("SELECT json_valid('{}')").fetchone()[0] != 1:
            raise DisclosureLedgerUnavailable("SQLite JSON functions are unavailable")
    except sqlite3.Error as error:
        raise DisclosureLedgerUnavailable("SQLite JSON functions are unavailable") from error


def _read_owned_objects(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT name, type, sql FROM sqlite_master
        WHERE name GLOB ? AND type IN ('table', 'trigger')
        """,
        (f"{OBJECT_PREFIX}*",),
    ).fetchall()
    return {
        str(row["name"]): (str(row["type"]), str(row["sql"] or "")) for row in rows
    }


def _create_schema(connection: sqlite3.Connection) -> None:
    for sql in _TABLE_DEFINITIONS.values():
        connection.execute(sql)
    for sql in _TRIGGER_DEFINITIONS.values():
        connection.execute(sql)
    connection.execute(
        f"INSERT INTO {META_TABLE} (singleton, schema_version, schema_fingerprint) "
        "VALUES (1, ?, ?)",
        (LEDGER_SCHEMA_VERSION, SCHEMA_FINGERPRINT),
    )
    _verify_schema_objects(_read_owned_objects(connection))


def _verify_schema_objects(actual: dict[str, tuple[str, str]]) -> None:
    if set(actual) != set(_OBJECT_DEFINITIONS):
        missing = sorted(set(_OBJECT_DEFINITIONS) - set(actual))
        unexpected = sorted(set(actual) - set(_OBJECT_DEFINITIONS))
        raise DisclosureLedgerUnavailable(
            f"disclosure ledger object set mismatch; missing={missing}, unexpected={unexpected}"
        )
    for name, (expected_type, expected_sql) in _OBJECT_DEFINITIONS.items():
        actual_type, actual_sql = actual[name]
        if actual_type != expected_type or _normalize_sql(actual_sql) != _normalize_sql(
            expected_sql
        ):
            raise DisclosureLedgerUnavailable(f"disclosure ledger object was altered: {name}")


def _verify_meta(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        f"SELECT singleton, schema_version, schema_fingerprint FROM {META_TABLE}"
    ).fetchall()
    if len(rows) != 1:
        raise DisclosureLedgerUnavailable("disclosure ledger metadata is missing or duplicated")
    row = rows[0]
    if (
        row["singleton"] != 1
        or row["schema_version"] != LEDGER_SCHEMA_VERSION
        or row["schema_fingerprint"] != SCHEMA_FINGERPRINT
    ):
        raise DisclosureLedgerUnavailable("unsupported disclosure ledger metadata")


def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
    ).fetchone()
    if exists is None:
        raise DisclosureLedgerUnavailable("the bound Goose session does not exist")


def _register_session(
    connection: sqlite3.Connection, session_id: str, limits: LedgerLimits
) -> None:
    ambiguous_rows = int(
        connection.execute(
            f"""
            SELECT count(*) FROM messages
            WHERE session_id = ? AND NOT ({_visible('messages')})
            """,
            (session_id,),
        ).fetchone()[0]
    )
    complete = int(ambiguous_rows == 0)
    reason = "bootstrap-complete" if complete else "preexisting-ambiguous-rows"
    connection.execute(
        f"""
        INSERT INTO {MANAGED_TABLE} (
            session_id, ledger_schema_version, coverage_epoch, coverage_complete,
            coverage_reason, ambiguous_rows_at_bootstrap, deletion_events,
            max_entries, max_stored_bytes, max_content_bytes, max_metadata_bytes,
            max_message_id_bytes, max_role_bytes
        ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            LEDGER_SCHEMA_VERSION,
            complete,
            reason,
            ambiguous_rows,
            limits.max_entries,
            limits.max_stored_bytes,
            limits.max_content_bytes,
            limits.max_metadata_bytes,
            limits.max_message_id_bytes,
            limits.max_role_bytes,
        ),
    )
    connection.execute(
        f"INSERT INTO {ACCOUNTING_TABLE} "
        "(session_id, entry_count, stored_bytes, omitted_entries) VALUES (?, 0, 0, 0)",
        (session_id,),
    )
    seed = _capture_statement("source", "bootstrap", seed=True)
    connection.execute(seed, (session_id,))


def _require_limits(row: sqlite3.Row, limits: LedgerLimits) -> None:
    installed = LedgerLimits(
        max_entries=int(row["max_entries"]),
        max_stored_bytes=int(row["max_stored_bytes"]),
        max_content_bytes=int(row["max_content_bytes"]),
        max_metadata_bytes=int(row["max_metadata_bytes"]),
        max_message_id_bytes=int(row["max_message_id_bytes"]),
        max_role_bytes=int(row["max_role_bytes"]),
    )
    if installed != limits:
        raise DisclosureLedgerUnavailable(
            "managed session ledger limits do not match the requested limits"
        )


def _read_status(
    connection: sqlite3.Connection, database: Path, session_id: str
) -> LedgerStatus:
    row = connection.execute(
        f"""
        SELECT managed.*, accounting.entry_count, accounting.stored_bytes,
               accounting.omitted_entries
        FROM {MANAGED_TABLE} AS managed
        JOIN {ACCOUNTING_TABLE} AS accounting
          ON accounting.session_id = managed.session_id
        WHERE managed.session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise DisclosureLedgerUnavailable("the bound Goose session is not ledger-managed")
    if int(row["ledger_schema_version"]) != LEDGER_SCHEMA_VERSION:
        raise DisclosureLedgerUnavailable("managed session uses an unsupported ledger schema")
    aggregate = connection.execute(
        f"""
        SELECT count(*) AS entry_count,
               COALESCE(sum(stored_bytes), 0) AS stored_bytes,
               COALESCE(sum(CASE WHEN omission_flags != 0 THEN 1 ELSE 0 END), 0)
                   AS omitted_entries,
               COALESCE(max(coverage_epoch), 0) AS maximum_epoch,
               COALESCE(sum(CASE WHEN ledger_schema_version != ? THEN 1 ELSE 0 END), 0)
                   AS wrong_schema_entries
        FROM {ENTRY_TABLE} WHERE session_id = ?
        """,
        (LEDGER_SCHEMA_VERSION, session_id),
    ).fetchone()
    expected_accounting = (
        int(aggregate["entry_count"]),
        int(aggregate["stored_bytes"]),
        int(aggregate["omitted_entries"]),
    )
    actual_accounting = (
        int(row["entry_count"]),
        int(row["stored_bytes"]),
        int(row["omitted_entries"]),
    )
    if actual_accounting != expected_accounting:
        raise DisclosureLedgerUnavailable("disclosure ledger accounting mismatch")
    coverage_epoch = int(row["coverage_epoch"])
    if int(aggregate["maximum_epoch"]) > coverage_epoch or int(
        aggregate["wrong_schema_entries"]
    ):
        raise DisclosureLedgerUnavailable("disclosure ledger entry state is invalid")
    limits = LedgerLimits(
        max_entries=int(row["max_entries"]),
        max_stored_bytes=int(row["max_stored_bytes"]),
        max_content_bytes=int(row["max_content_bytes"]),
        max_metadata_bytes=int(row["max_metadata_bytes"]),
        max_message_id_bytes=int(row["max_message_id_bytes"]),
        max_role_bytes=int(row["max_role_bytes"]),
    )
    if actual_accounting[0] > limits.max_entries or actual_accounting[1] > (
        limits.max_stored_bytes
    ):
        raise DisclosureLedgerUnavailable("disclosure ledger accounting exceeds its limits")
    return LedgerStatus(
        database=database,
        session_id=session_id,
        schema_version=LEDGER_SCHEMA_VERSION,
        schema_fingerprint=SCHEMA_FINGERPRINT,
        coverage_epoch=coverage_epoch,
        coverage_complete=bool(row["coverage_complete"]),
        coverage_reason=str(row["coverage_reason"]),
        ambiguous_rows_at_bootstrap=int(row["ambiguous_rows_at_bootstrap"]),
        deletion_events=int(row["deletion_events"]),
        ledger_entries=actual_accounting[0],
        stored_bytes=actual_accounting[1],
        omitted_entries=actual_accounting[2],
        limits=limits,
    )
