from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandboxed_goose.contextfs.view_store import (
    MAX_CACHED_BYTES,
    MAX_IDLE_LEASE_SECONDS,
    MAX_VIEW_DESCRIPTOR_BYTES,
    MAX_VIEW_DESCRIPTORS,
    MAX_VIEW_FILE_BYTES,
    MAX_VIEWS_PER_PROCESS,
    MAX_VIEWS_PER_SESSION,
    LedgerCoverageIdentity,
    MaterializedViewFile,
    SessionOperation,
    SessionOperationRequest,
    SessionOperationResult,
    SessionViewStore,
    SessionViewStoreLimits,
    ViewExpiredError,
    ViewMismatchError,
    ViewTooLargeError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tokens(*values: int) -> Callable[[int], bytes]:
    iterator: Iterator[int] = iter(values)

    def next_token(size: int) -> bytes:
        assert size == 32
        return bytes([next(iterator)]) * size

    return next_token


def _request(
    session_id: str = "primary",
    operation: SessionOperation = SessionOperation.EXACT_OBJECT,
    path: str = "session/messages/by-source-row/00000000000000000001.json",
    schema: int = 3,
) -> SessionOperationRequest:
    return SessionOperationRequest(
        session_id=session_id,
        operation=operation,
        path=path,
        projection_schema_version=schema,
    )


def _coverage(
    epoch: int = 1,
    *,
    schema_version: int = 1,
    fingerprint: str = "b" * 64,
) -> LedgerCoverageIdentity:
    return LedgerCoverageIdentity(
        schema_version=schema_version,
        schema_fingerprint=fingerprint,
        coverage_epoch=epoch,
    )


def _result(
    marker: str = "one",
    *,
    coverage_epoch: int = 1,
    content_size: int = 0,
) -> SessionOperationResult:
    descriptor_data = f'{{"marker":"{marker}"}}'.encode()
    snapshot_id = hashlib.sha256(descriptor_data).hexdigest()
    content = marker.encode() + b"x" * content_size
    return SessionOperationResult.from_files(
        snapshot_id=snapshot_id,
        ledger_coverage=_coverage(coverage_epoch),
        descriptor_count=1,
        descriptor_data=descriptor_data,
        files={"object.json": content},
    )


def test_operation_types_freeze_files_and_validate_full_snapshot_identity() -> None:
    files = {"z.json": b"z", "a.json": b"a"}
    result = SessionOperationResult.from_files(
        snapshot_id="a" * 64,
        ledger_coverage=_coverage(2),
        descriptor_count=2,
        descriptor_data=b"descriptors",
        files=files,
    )
    files["a.json"] = b"changed"

    assert [file.path for file in result.files] == ["a.json", "z.json"]
    assert result.file_bytes("a.json") == b"a"
    with pytest.raises(KeyError):
        result.file_bytes("missing.json")
    with pytest.raises(ValueError, match="256-bit"):
        SessionOperationResult.from_files(
            snapshot_id="goose-short",
            ledger_coverage=_coverage(),
            descriptor_count=0,
            descriptor_data=b"",
            files={},
        )


@pytest.mark.parametrize(
    "path",
    ["/absolute", "trailing/", "a//b", "a/./b", "a/../b", "line\nbreak"],
)
def test_operation_types_reject_noncanonical_or_controlled_paths(path: str) -> None:
    with pytest.raises(ValueError):
        _request(path=path)
    with pytest.raises(ValueError):
        MaterializedViewFile(path, b"content")


def test_result_file_count_is_preflighted_before_freezing() -> None:
    files = {f"objects/{index:05}.json": b"" for index in range(MAX_VIEW_DESCRIPTORS + 1)}
    with pytest.raises(ViewTooLargeError, match="file count"):
        SessionOperationResult.from_files(
            snapshot_id="a" * 64,
            ledger_coverage=_coverage(),
            descriptor_count=0,
            descriptor_data=b"",
            files=files,
        )


def test_operation_request_validation_is_strict_and_bounded() -> None:
    assert _request(path="").path == ""
    with pytest.raises(ValueError, match="must not be empty"):
        _request(session_id="")
    with pytest.raises(TypeError, match="must be a string"):
        _request(session_id=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="trimmed"):
        _request(session_id=" primary")
    with pytest.raises(ValueError, match="256 UTF-8"):
        _request(session_id="s" * 257)
    with pytest.raises(ValueError, match="valid UTF-8"):
        _request(session_id="\ud800")
    with pytest.raises(TypeError, match="SessionOperation"):
        SessionOperationRequest("primary", "manifest", "", 3)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer"):
        SessionOperationRequest("primary", SessionOperation.MANIFEST, "", True)
    with pytest.raises(ValueError, match="depth"):
        _request(path="/".join("a" for _ in range(17)))
    with pytest.raises(ValueError, match="component"):
        _request(path="x" * 256)


def test_result_container_and_ledger_identity_validation_is_strict() -> None:
    with pytest.raises(ValueError, match="schema_fingerprint"):
        LedgerCoverageIdentity(1, "not-a-digest", 1)
    with pytest.raises(ValueError, match="positive"):
        LedgerCoverageIdentity(0, "b" * 64, 1)
    with pytest.raises(ValueError, match="positive"):
        LedgerCoverageIdentity(1, "b" * 64, 0)
    with pytest.raises(TypeError, match="must be bytes"):
        MaterializedViewFile("file", bytearray(b"mutable"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="LedgerCoverageIdentity"):
        SessionOperationResult("a" * 64, None, 0, b"", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="descriptor_data"):
        SessionOperationResult(
            "a" * 64,
            _coverage(),
            0,
            bytearray(),  # type: ignore[arg-type]
            (),
        )
    with pytest.raises(ValueError, match="non-negative"):
        SessionOperationResult("a" * 64, _coverage(), -1, b"", ())
    with pytest.raises(TypeError, match="integer"):
        SessionOperationResult("a" * 64, _coverage(), True, b"", ())
    with pytest.raises(TypeError, match="immutable tuple"):
        SessionOperationResult("a" * 64, _coverage(), 0, b"", [])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MaterializedViewFile"):
        SessionOperationResult("a" * 64, _coverage(), 0, b"", ("not-a-file",))  # type: ignore[arg-type]
    duplicate = MaterializedViewFile("same", b"content")
    with pytest.raises(ValueError, match="unique"):
        SessionOperationResult("a" * 64, _coverage(), 0, b"", (duplicate, duplicate))
    with pytest.raises(ViewTooLargeError, match="file count"):
        SessionOperationResult(
            "a" * 64,
            _coverage(),
            0,
            b"",
            (duplicate,) * (MAX_VIEW_DESCRIPTORS + 1),
        )
    with pytest.raises(TypeError, match="mapping"):
        SessionOperationResult.from_files(
            snapshot_id="a" * 64,
            ledger_coverage=_coverage(),
            descriptor_count=0,
            descriptor_data=b"",
            files=[],  # type: ignore[arg-type]
        )


def test_view_ids_are_256_bit_opaque_tokens_and_collisions_are_retried() -> None:
    store = SessionViewStore(token_bytes=_tokens(0xAB, 0xAB, 0xCD))
    first = store.create(_request(session_id="secret-session"), _result("first"))
    second = store.create(_request(path="different"), _result("second"))

    assert first.view_id == "ab" * 32
    assert second.view_id == "cd" * 32
    assert len(bytes.fromhex(first.view_id)) == 32
    assert "secret-session" not in first.view_id
    assert "object" not in first.view_id


@pytest.mark.parametrize(
    "mismatched",
    [
        _request(session_id="other"),
        _request(operation=SessionOperation.MANIFEST),
        _request(path="other.json"),
        _request(schema=4),
    ],
)
def test_continuation_requires_exact_session_operation_path_and_schema_binding(
    mismatched: SessionOperationRequest,
) -> None:
    store = SessionViewStore(token_bytes=_tokens(1))
    view = store.create(_request(), _result())

    with pytest.raises(ViewMismatchError) as caught:
        store.get(view.view_id, mismatched, current_ledger_coverage=_coverage())
    assert caught.value.code == "view_mismatch"
    assert caught.value.retryable is False
    assert (
        store.get(view.view_id, _request(), current_ledger_coverage=_coverage()).view_id
        == view.view_id
    )


@pytest.mark.parametrize(
    "current_coverage",
    [
        None,
        _coverage(2),
        _coverage(schema_version=2),
        _coverage(fingerprint="c" * 64),
    ],
)
def test_missing_session_or_changed_ledger_identity_revokes_view(
    current_coverage: LedgerCoverageIdentity | None,
) -> None:
    store = SessionViewStore(token_bytes=_tokens(2))
    view = store.create(_request(), _result(coverage_epoch=1))

    with pytest.raises(ViewExpiredError, match="revoked") as caught:
        store.get(view.view_id, _request(), current_ledger_coverage=current_coverage)
    assert caught.value.code == "view_expired"
    assert caught.value.retryable is True
    assert store.live_views == 0
    with pytest.raises(ViewExpiredError, match="unavailable"):
        store.get(view.view_id, _request(), current_ledger_coverage=_coverage())


def test_idle_lease_refreshes_on_success_and_expires_at_the_boundary() -> None:
    clock = FakeClock()
    store = SessionViewStore(
        SessionViewStoreLimits(idle_lease_seconds=10),
        clock=clock,
        token_bytes=_tokens(3),
    )
    view = store.create(_request(), _result())

    clock.now = 9
    refreshed = store.get(view.view_id, _request(), current_ledger_coverage=_coverage())
    assert refreshed.last_accessed_at == 9
    clock.now = 18
    assert (
        store.get(view.view_id, _request(), current_ledger_coverage=_coverage()).last_accessed_at
        == 18
    )
    clock.now = 28
    with pytest.raises(ViewExpiredError):
        store.get(view.view_id, _request(), current_ledger_coverage=_coverage())


def test_per_session_limit_evicts_that_sessions_least_recently_used_view() -> None:
    store = SessionViewStore(
        SessionViewStoreLimits(max_views_per_session=2, max_views_per_process=4),
        token_bytes=_tokens(10, 11, 12, 13),
    )
    first = store.create(_request(path="first"), _result("first"))
    second = store.create(_request(path="second"), _result("second"))
    other = store.create(_request(session_id="other", path="other"), _result("other"))
    store.get(first.view_id, _request(path="first"), current_ledger_coverage=_coverage())
    third = store.create(_request(path="third"), _result("third"))

    with pytest.raises(ViewExpiredError):
        store.get(second.view_id, _request(path="second"), current_ledger_coverage=_coverage())
    assert store.get(first.view_id, _request(path="first"), current_ledger_coverage=_coverage())
    assert store.get(third.view_id, _request(path="third"), current_ledger_coverage=_coverage())
    assert store.get(
        other.view_id,
        _request(session_id="other", path="other"),
        current_ledger_coverage=_coverage(),
    )
    assert store.live_views_for_session("primary") == 2
    assert store.live_views_for_session("other") == 1


def test_process_count_limit_uses_global_lru_order() -> None:
    store = SessionViewStore(
        SessionViewStoreLimits(max_views_per_session=2, max_views_per_process=2),
        token_bytes=_tokens(20, 21, 22),
    )
    first = store.create(_request(session_id="one", path="first"), _result("first"))
    second = store.create(_request(session_id="two", path="second"), _result("second"))
    store.get(
        first.view_id,
        _request(session_id="one", path="first"),
        current_ledger_coverage=_coverage(),
    )
    third = store.create(_request(session_id="three", path="third"), _result("third"))

    with pytest.raises(ViewExpiredError):
        store.get(
            second.view_id,
            _request(session_id="two", path="second"),
            current_ledger_coverage=_coverage(),
        )
    assert store.get(
        first.view_id,
        _request(session_id="one", path="first"),
        current_ledger_coverage=_coverage(),
    )
    assert store.get(
        third.view_id,
        _request(session_id="three", path="third"),
        current_ledger_coverage=_coverage(),
    )


def test_byte_limit_evicts_lru_but_oversized_create_is_non_destructive() -> None:
    first_result = _result("first", content_size=600)
    second_result = _result("second", content_size=600)
    probe = SessionViewStore(token_bytes=_tokens(30))
    probe_view = probe.create(_request(path="first"), first_result)
    byte_limit = probe_view.cached_bytes + 100

    store = SessionViewStore(
        SessionViewStoreLimits(max_cached_bytes=byte_limit),
        token_bytes=_tokens(31, 32),
    )
    first = store.create(_request(path="first"), first_result)
    second = store.create(_request(path="second"), second_result)
    with pytest.raises(ViewExpiredError):
        store.get(first.view_id, _request(path="first"), current_ledger_coverage=_coverage())
    assert store.get(second.view_id, _request(path="second"), current_ledger_coverage=_coverage())

    with pytest.raises(ViewTooLargeError, match="cache byte limit"):
        store.create(_request(path="huge"), _result("huge", content_size=byte_limit))
    assert store.get(second.view_id, _request(path="second"), current_ledger_coverage=_coverage())
    assert store.live_views == 1


def test_result_limits_fail_before_material_enters_the_store() -> None:
    with pytest.raises(ViewTooLargeError, match="descriptor count"):
        SessionOperationResult.from_files(
            snapshot_id="a" * 64,
            ledger_coverage=_coverage(),
            descriptor_count=MAX_VIEW_DESCRIPTORS + 1,
            descriptor_data=b"",
            files={},
        )
    with pytest.raises(ViewTooLargeError, match="descriptor data"):
        SessionOperationResult.from_files(
            snapshot_id="a" * 64,
            ledger_coverage=_coverage(),
            descriptor_count=1,
            descriptor_data=b"x" * (MAX_VIEW_DESCRIPTOR_BYTES + 1),
            files={},
        )
    with pytest.raises(ViewTooLargeError, match="materialized file"):
        MaterializedViewFile("large", b"x" * (MAX_VIEW_FILE_BYTES + 1))

    shared_mebibyte = b"x" * MAX_VIEW_FILE_BYTES
    with pytest.raises(ViewTooLargeError, match="hard process cache"):
        SessionOperationResult(
            snapshot_id="a" * 64,
            ledger_coverage=_coverage(),
            descriptor_count=33,
            descriptor_data=b"",
            files=tuple(
                MaterializedViewFile(f"objects/{index:02}.json", shared_mebibyte)
                for index in range(33)
            ),
        )


def test_unknown_and_malformed_tokens_have_the_same_bounded_failure() -> None:
    store = SessionViewStore()
    messages = []
    for token in ("attacker-controlled-secret", "f" * 64):
        with pytest.raises(ViewExpiredError) as caught:
            store.get(token, _request(), current_ledger_coverage=_coverage())
        messages.append(str(caught.value))

    assert messages == [
        "session view is unavailable; start a new operation",
        "session view is unavailable; start a new operation",
    ]
    assert all("attacker" not in message for message in messages)


def test_store_rejects_invalid_collaborator_types_and_entropy_sources_atomically() -> None:
    store = SessionViewStore(token_bytes=lambda _: b"short")
    with pytest.raises(TypeError, match="request"):
        store.create(object(), _result())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="result"):
        store.create(_request(), object())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="exactly 32 bytes"):
        store.create(_request(), _result())
    assert store.live_views == 0

    collisions = SessionViewStore(token_bytes=lambda size: b"x" * size)
    first = collisions.create(_request(path="first"), _result("first"))
    with pytest.raises(RuntimeError, match="repeatedly"):
        collisions.create(_request(path="second"), _result("second"))
    assert collisions.live_views == 1
    assert collisions.get(
        first.view_id,
        _request(path="first"),
        current_ledger_coverage=_coverage(),
    )


def test_store_rejects_invalid_clock_and_continuation_collaborators() -> None:
    with pytest.raises(RuntimeError, match="number"):
        _ = SessionViewStore(clock=lambda: "now").live_views  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="finite"):
        _ = SessionViewStore(clock=lambda: float("inf")).live_views

    store = SessionViewStore(token_bytes=_tokens(50))
    view = store.create(_request(), _result())
    with pytest.raises(TypeError, match="request"):
        store.get(view.view_id, object(), current_ledger_coverage=_coverage())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="current_ledger_coverage"):
        store.get(view.view_id, _request(), current_ledger_coverage=1)  # type: ignore[arg-type]


def test_explicit_session_revocation_and_process_clear_update_accounting() -> None:
    store = SessionViewStore(token_bytes=_tokens(40, 41, 42))
    store.create(_request(path="one"), _result("one"))
    store.create(_request(path="two"), _result("two"))
    store.create(_request(session_id="other", path="three"), _result("three"))

    assert store.revoke_session("primary") == 2
    assert store.live_views == 1
    assert store.cached_bytes > 0
    assert store.revoke_session("primary") == 0
    store.clear()
    assert store.live_views == 0
    assert store.cached_bytes == 0


@pytest.mark.parametrize(
    ("field", "value", "maximum"),
    [
        ("max_views_per_session", 5, MAX_VIEWS_PER_SESSION),
        ("max_views_per_process", 17, MAX_VIEWS_PER_PROCESS),
        ("max_cached_bytes", MAX_CACHED_BYTES + 1, MAX_CACHED_BYTES),
        ("idle_lease_seconds", MAX_IDLE_LEASE_SECONDS + 1, MAX_IDLE_LEASE_SECONDS),
    ],
)
def test_store_limits_can_only_tighten_hard_maxima(
    field: str,
    value: int,
    maximum: int,
) -> None:
    values = {
        "max_views_per_session": MAX_VIEWS_PER_SESSION,
        "max_views_per_process": MAX_VIEWS_PER_PROCESS,
        "max_cached_bytes": MAX_CACHED_BYTES,
        "idle_lease_seconds": MAX_IDLE_LEASE_SECONDS,
    }
    values[field] = value
    with pytest.raises(ValueError, match=str(maximum)):
        SessionViewStoreLimits(**values)


def test_store_limits_reject_nonpositive_boolean_and_inverted_counts() -> None:
    assert SessionViewStore().limits == SessionViewStoreLimits()
    with pytest.raises(ValueError, match="positive"):
        SessionViewStoreLimits(max_cached_bytes=0)
    with pytest.raises(TypeError, match="integer"):
        SessionViewStoreLimits(idle_lease_seconds=True)
    with pytest.raises(ValueError, match="cannot exceed"):
        SessionViewStoreLimits(max_views_per_session=3, max_views_per_process=2)


def test_concurrent_creation_preserves_process_and_session_limits() -> None:
    store = SessionViewStore()

    def create_view(index: int) -> None:
        store.create(_request(path=f"object-{index}"), _result(str(index)))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(create_view, range(64)))

    assert store.live_views == MAX_VIEWS_PER_SESSION
    assert store.live_views <= MAX_VIEWS_PER_PROCESS
    assert store.cached_bytes <= MAX_CACHED_BYTES
