"""Shared SQL for conservative Goose agent-context eligibility checks."""

from __future__ import annotations


def agent_visible_sql(row: str, *, max_metadata_bytes: int) -> str:
    """Return static SQL that accepts only JSON boolean ``true`` visibility.

    ``row`` is an internal, fixed SQLite table or trigger-row name.  Callers must not
    pass user-controlled text.
    """

    prefix = f"{row}." if row else ""
    metadata = f"{prefix}metadata_json"
    return (
        f"CASE WHEN typeof({metadata}) = 'text' "
        f"AND length(CAST({metadata} AS BLOB)) <= {max_metadata_bytes} "
        f"THEN CASE WHEN json_valid({metadata}) "
        f"THEN json_type({metadata}, '$.agentVisible') = 'true' "
        "ELSE 0 END ELSE 0 END"
    )
