"""A bounded SQLite model of the stock Goose session-writer contract.

This is an oracle and fixture builder, not a replacement implementation. Its schema
and write boundaries are checked against the pinned upstream source when that checkout
is available.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stock-goose-session-v1.json"


def load_stock_goose_contract() -> dict[str, Any]:
    """Load the checked-in, reviewable stock-Goose row-shape contract."""

    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("stock-Goose contract artifact must be a JSON object")
    return value


def canonical_json(value: object) -> str:
    """Serialize fixtures the way Goose's compact database JSON is shaped."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def visible_metadata() -> dict[str, object]:
    return {"userVisible": True, "agentVisible": True}


def agent_only_metadata() -> dict[str, object]:
    return {"userVisible": False, "agentVisible": True}


def user_only_metadata() -> dict[str, object]:
    return {"userVisible": True, "agentVisible": False}


def tool_request_content(tool_id: str, ordinal: int) -> list[dict[str, object]]:
    return [
        {
            "type": "toolRequest",
            "id": tool_id,
            "toolCall": {
                "status": "success",
                "value": {
                    "name": "calculate",
                    "arguments": {"expression": f"{ordinal}+{ordinal}"},
                },
            },
        }
    ]


def tool_response_content(tool_id: str, ordinal: int) -> list[dict[str, object]]:
    return [
        {
            "type": "toolResponse",
            "id": tool_id,
            "toolResult": {
                "status": "success",
                "value": {
                    "content": [{"type": "text", "text": str(ordinal * 2)}],
                    "isError": False,
                },
            },
        }
    ]


@dataclass(frozen=True, slots=True)
class StockMessage:
    """One complete physical row from the stock messages table."""

    row_id: int
    message_id: str | None
    session_id: str
    role: str
    content_json: str
    created_timestamp: int
    metadata_json: str | None

    @property
    def content(self) -> object:
        return json.loads(self.content_json)

    @property
    def metadata(self) -> Mapping[str, object]:
        if self.metadata_json is None:
            return {}
        value = json.loads(self.metadata_json)
        if not isinstance(value, dict):
            raise AssertionError("fixture metadata must be an object")
        return value


class StockGooseDatabase:
    """Execute the relevant stock Goose writes with their real transaction boundaries."""

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(
        cls, path: Path, session_ids: Sequence[str] = ("primary", "decoy")
    ) -> StockGooseDatabase:
        contract = load_stock_goose_contract()
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    working_dir TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(str(contract["messages_table_sql"]))
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)"
            )
            connection.executemany(
                "INSERT INTO sessions (id, name) VALUES (?, ?)",
                [(session_id, session_id) for session_id in session_ids],
            )
            connection.commit()
        finally:
            connection.close()
        return cls(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        return connection

    def add_message(
        self,
        session_id: str,
        *,
        message_id: str,
        role: str,
        content: object,
        created_timestamp: int,
        metadata: Mapping[str, object],
    ) -> int:
        """Model stock ``add_message``: one immediate transaction per row."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    canonical_json(content),
                    created_timestamp,
                    canonical_json(dict(metadata)),
                ),
            )
            row_id = int(cursor.lastrowid)
            connection.commit()
            return row_id
        finally:
            connection.close()

    def add_tool_pair(
        self,
        session_id: str,
        ordinal: int,
        *,
        created_timestamp_base: int = 0,
    ) -> tuple[int, int]:
        tool_id = f"tool-{ordinal:03d}"
        request_created = created_timestamp_base + ordinal * 10
        request_id = self.add_message(
            session_id,
            message_id=f"request-{ordinal:03d}",
            role="assistant",
            content=tool_request_content(tool_id, ordinal),
            created_timestamp=request_created,
            metadata=visible_metadata(),
        )
        response_id = self.add_message(
            session_id,
            message_id=f"response-{ordinal:03d}",
            role="user",
            content=tool_response_content(tool_id, ordinal),
            created_timestamp=request_created + 1,
            metadata=visible_metadata(),
        )
        return request_id, response_id

    def archive_message(self, session_id: str, message_id: str) -> None:
        """Model stock ``update_message_metadata(...with_agent_invisible())``."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            selected = connection.execute(
                """
                SELECT metadata_json FROM messages
                WHERE message_id = ? AND session_id = ?
                """,
                (message_id, session_id),
            ).fetchone()
            if selected is None:
                raise LookupError(message_id)
            metadata = json.loads(str(selected[0]))
            if not isinstance(metadata, dict):
                raise ValueError("metadata_json must contain an object")
            metadata["agentVisible"] = False
            connection.execute(
                """
                UPDATE messages SET metadata_json = ?
                WHERE message_id = ? AND session_id = ?
                """,
                (canonical_json(metadata), message_id, session_id),
            )
            connection.commit()
        finally:
            connection.close()

    def add_summary(
        self,
        session_id: str,
        *,
        summary_id: str,
        tool_id: str,
        response_created_timestamp: int,
    ) -> int:
        return self.add_message(
            session_id,
            message_id=summary_id,
            role="user",
            content=[{"type": "text", "text": f"summary for {tool_id}"}],
            created_timestamp=response_created_timestamp,
            metadata=agent_only_metadata(),
        )

    def replace_conversation(
        self,
        session_id: str,
        messages: Iterable[tuple[str, str, object, int, Mapping[str, object]]],
    ) -> None:
        """Model stock ``replace_conversation`` as one immediate delete/insert transaction."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            connection.executemany(
                """
                INSERT INTO messages
                    (message_id, session_id, role, content_json,
                     created_timestamp, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        message_id,
                        session_id,
                        role,
                        canonical_json(content),
                        created_timestamp,
                        canonical_json(dict(metadata)),
                    )
                    for message_id, role, content, created_timestamp, metadata in messages
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def rows(self, session_id: str) -> list[StockMessage]:
        connection = self.connect()
        try:
            records = connection.execute(
                """
                SELECT id, message_id, session_id, role, content_json,
                       created_timestamp, metadata_json
                FROM messages WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
        return [
            StockMessage(
                row_id=int(record["id"]),
                message_id=record["message_id"],
                session_id=str(record["session_id"]),
                role=str(record["role"]),
                content_json=str(record["content_json"]),
                created_timestamp=int(record["created_timestamp"]),
                metadata_json=record["metadata_json"],
            )
            for record in records
        ]


def normalize_sql(value: str) -> str:
    """Normalize source SQL only enough to make whitespace changes irrelevant."""

    return " ".join(value.split()).rstrip(";")
