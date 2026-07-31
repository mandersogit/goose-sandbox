import pytest

from sandboxed_goose.session_binding import (
    GOOSE_SESSION_ENV_KEY,
    GOOSE_SESSION_META_KEY,
    SessionBindingError,
    resolve_session_id,
)


def test_request_metadata_is_authoritative_and_case_insensitive() -> None:
    assert (
        resolve_session_id(
            {GOOSE_SESSION_META_KEY.upper(): "session-123"},
            {GOOSE_SESSION_ENV_KEY: "session-123"},
        )
        == "session-123"
    )


def test_session_environment_is_a_compatibility_fallback() -> None:
    assert resolve_session_id(None, {GOOSE_SESSION_ENV_KEY: "session-from-env"}) == (
        "session-from-env"
    )


def test_session_binding_fails_closed_on_missing_or_mismatched_identity() -> None:
    with pytest.raises(SessionBindingError, match="did not supply"):
        resolve_session_id({}, {})
    with pytest.raises(SessionBindingError, match="does not match"):
        resolve_session_id(
            {GOOSE_SESSION_META_KEY: "request-session"},
            {GOOSE_SESSION_ENV_KEY: "process-session"},
        )


@pytest.mark.parametrize("session_id", [" ", "contains\nnewline", "x" * 257])
def test_session_binding_rejects_invalid_values(session_id: str) -> None:
    with pytest.raises(SessionBindingError):
        resolve_session_id({GOOSE_SESSION_META_KEY: session_id}, {})
