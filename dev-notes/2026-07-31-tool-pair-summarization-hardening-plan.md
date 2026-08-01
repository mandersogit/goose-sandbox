# Tool-pair summarization hardening plan

> **Scope update (2026-07-31):** The
> [adversarial review synthesis](2026-07-31-ledger-adversarial-review-synthesis.md)
> defines the active milestone and supersedes this plan where they differ. The larger
> deterministic program below remains an extended-hardening catalog; fifty batches,
> exact complete namespaces/counts, and exhaustive transport/interleaving matrices are
> not current acceptance gates.

## Status and decision

Two independent adversarial reviews found that the first version of this plan was not
implementation-ready. Its proposed `expected_snapshot_id` handshake could not make
progress in a live Goose session, and its eager schema-v3 tree would exceed ContextFS
resource limits.

The revised decision is:

- stock, unmodified Goose is the only supported runtime contract; the tracked
  history-provenance patch is a development experiment and is not an installation or
  acceptance prerequisite;
- project-managed Goose runs continue to force
  `GOOSE_TOOL_PAIR_SUMMARIZATION=false`;
- projection correctness must nevertheless tolerate the feature being enabled and
  repeatedly mutating the session;
- cross-call consistency is provided by bounded, operation-pinned views, not by
  comparing every call with a newly generated snapshot;
- the projector becomes operation-aware before adding any second identity namespace;
- tool-pair summarization and full token-threshold compaction have separate acceptance
  gates; and
- deterministic providers and database state machines are the acceptance tests. A
  live language model is optional smoke coverage only.

The narrower milestone selected by the adversarial review synthesis is implemented.
The schema-v2 ledger captures bounded eligible forms and degrades without aborting
ordinary Goose writes. The public schema-v3 operation projector merges valid current
rows with same-incarnation, same-epoch captures; it rejects the experimental history
field, verifies the exact ledger at each request, pins bounded continuations, and keeps
exact physical-row lookup independent of the capped recent window.

This document remains an extended-hardening catalog. Its older checkpoint sections are
retained to explain how the implementation evolved; their stated limitations were true
at those checkpoints and are superseded by the current completion summary above.

## Current schema-v2 and public-operation checkpoint

Ledger schema v2 is fingerprinted in `tests/fixtures/disclosure-ledger-v2.json`. It adds
bounded timestamp handling, capture-disable/degradation state, capped bootstrap
ambiguity accounting, and database/session-incarnation identity. Runtime row, byte, and
accounting failures preserve the Goose write while advancing the coverage epoch and
hiding the now-ambiguous capture generation. Schema v1 remains only as a historical
fixture; there is no in-place pre-release migration, so an isolated development Goose
root containing v1 objects must be recreated before using this version.

The public schema-v3 operation path uses at most 256 recent descriptors, capped counts,
a 64 MiB aggregate source-content preflight, 4 MiB descriptor storage, 1 MiB transcript
and file limits, and the bounded process-local view cache. Stable physical files omit
view-relative ordinals and visibility. A validated source-row path can be queried
directly outside the recent window, and direct/Apptainer rendering starts from the same
immutable operation result.

At this checkpoint, `make all` passes Ruff, mypy, Pyright, and the ordinary suite with
191 passed and 9 opt-in external-runtime tests skipped. The real
`make test-apptainer-contextfs` proof also passes.

## Test foundation implemented

The following implementation-independent test foundation now exists:

- `tests/fixtures/stock-goose-session-v1.json` records the pinned stock message-table,
  visibility, tool-pair, summary, cutoff, and transaction-order shapes without the
  experimental provenance field.
- `tests/support/stock_goose.py` provides a deterministic SQLite writer oracle with
  the stock transaction boundaries for add, metadata archive, and atomic conversation
  replacement.
- `tests/test_stock_goose_contract.py` checks the canonical artifact against the
  optional pinned upstream checkout, the three separately observable archival commits,
  duplicate message-ID behavior, inherited summary timestamps, WAL replacement
  atomicity, fail-closed handling of unmanaged invisible rows, and the 255/256/257
  message and 699/700/701 event boundaries.
- `tests/test_goose_summarization_controls.py` runs a real Goose process against a
  deterministic OpenAI-compatible provider. With thirteen eligible pairs and an
  explicit cutoff of two, it proves that the project wrapper overrides an inherited
  `GOOSE_TOOL_PAIR_SUMMARIZATION=true`, makes no summary request, changes no original
  visibility, and inserts no summary. Its inverse control bypasses only the wrapper
  override and verifies the exact ten-pair request and persisted-row shape.
- `tests/test_live_test.py` verifies that the live driver forces summarization off and
  that optional source provenance accepts a clean stock checkout while rejecting a
  locally modified Goose tree. Source provenance is not a patch preflight.

The real control test passed against a clean build of upstream commit
`ee61c7c499dbf08786a75948d949639cbab14150`, made from `git archive HEAD`; the tested
binary contained no project provenance patch. It was rerun after ledger startup and
capture were implemented: the no-tool initialization request established the managed
session before the first provider request, disabled mode produced no archive captures,
and enabled mode retained the exact pre-archive forms of all twenty originals in the
ten-pair batch. The ordinary suite leaves this test skipped unless `GOOSE_BIN` names an
executable. Run it explicitly with:

```bash
GOOSE_BIN=/absolute/path/to/stock/goose \
  local.venv/bin/pytest -q tests/test_goose_summarization_controls.py
```

These tests establish the input and control oracles. They do not claim that
operation-pinned views, ledger-backed projection, or the repeated fifty-batch
acceptance gate has already been implemented.

## Historical schema-v1 ledger checkpoint

The first implementation of `sandboxed_goose.contextfs.disclosure_ledger` used ledger
schema version 1:

- fifteen exact, fingerprinted, namespaced SQLite tables and static triggers, recorded
  in `tests/fixtures/disclosure-ledger-v1.json`;
- one `BEGIN IMMEDIATE` bootstrap that validates the pinned stock schema, registers
  exactly the bound session, seeds only currently agent-visible rows, and rolls back
  the whole installation if coverage cannot fit;
- persistent insert, visible-update, and pre-archive capture that remains active while
  the MCP process is absent and never imports an already invisible row;
- nondeletable physical/epoch entries that refresh in place only while their source is
  visible, exact row/byte accounting, hard per-field and per-session limits, and bounded
  omission records for oversized source fields;
- transaction-aborting quota and unavailable-state failures, so a stock Goose insert,
  visible update, or visibility transition cannot commit after losing its last
  disclosed form; and
- conservative coverage-epoch advancement on deletion or session movement, with old
  epochs retained but ineligible for future projection.

Both normal stdio entry points run the shared bootstrap before starting their
framework's protocol loop. Direct protocol tests observe a prepared ledger after tool
listing through those entry points. Request-bound enforcement and fail-closed behavior
for unprepared framework/module-level servers remain required. A manual MCP launch
without `AGENT_SESSION_ID` remains available for tool-list inspection; once Goose
supplies that binding, a missing database or invalid ledger fails normal startup.

Fifteen ledger state-machine tests cover bootstrap idempotence and rollback,
cross-session isolation, persistent capture, invisible-to-visible transitions,
archive-at-quota UPSERT behavior, row and byte rollback, omission records, deletion
epochs, deletion rejection and identity immutability, altered objects, accounting
corruption, and immutable installed limits. The projector does not expose ledger
entries yet; that remains behind the operation-pinned-view work.

At this checkpoint, `make all` passes Ruff, mypy, Pyright, and the complete ordinary
suite: 122 tests passed and 9 explicitly configured integration tests were skipped
because their external runtimes were not selected. The real summarization control
described above was run separately with `GOOSE_BIN` pointing at the clean stock build.

## Historical operation-pinned view-store checkpoint

`sandboxed_goose.contextfs.view_store` now provides the internal foundation for the
next projection contract:

- explicit `recent-tree`, `directory-page`, `exact-object`, `transcript`, and
  `manifest` operation identities bound to the exact session, canonical path, and
  projection schema;
- immutable bounded descriptor data and materialized file results with a required full
  256-bit snapshot digest;
- opaque random 256-bit view IDs and exact continuation binding to the ledger schema
  version, schema fingerprint, and coverage epoch;
- a thread-safe process-local LRU with the planned four-view per-session, sixteen-view
  per-process, 32 MiB cache, and ten-minute idle hard maxima; and
- typed `view_expired`, `view_mismatch`, and `view_too_large` failures without echoing
  attacker-controlled tokens.

Thirty-four focused tests cover validation, immutable copying, token collision and
entropy-source failures, binding mismatch, ledger revocation, idle refresh and expiry,
session/process/byte LRU behavior, non-destructive oversize failure, explicit
revocation, hard-limit tightening, accounting, and concurrent creation. Branch
coverage for the module is 99%; the only uncovered lines are defensive assertions for
internally inconsistent LRU or byte accounting.

At this checkpoint the store deliberately owned no database transaction and was not yet
exposed by either MCP adapter. The later schema-v3 public broker now uses it.

With this foundation included, `make all` passes Ruff, mypy, Pyright, and the complete
ordinary suite: 156 tests passed and 9 explicitly configured integration tests were
skipped because their external runtimes were not selected.

## Historical operation descriptor-query checkpoint

The first internal schema-v3 query/materialization slice implemented:

- one read-only transaction verifies the exact session, all ledger objects and
  accounting, and the ledger schema/fingerprint/coverage epoch before querying rows;
- a `LIMIT 8193` preflight bounds the complete currently projectable stock-row set at
  8,192 descriptors and checks the 64 MiB aggregate raw-content limit before fetching
  any content;
- installed ledger limits bound individual content and message IDs, oversized values
  become deterministic omission states, retained identity bytes are preflighted, and
  descriptor serialization is capped at 4 MiB;
- the content query streams one at most 512 KiB row at a time from the same SQLite
  snapshot instead of retaining the full aggregate in Python;
- canonical descriptors include physical identity, visibility, normalized-content and
  stable-file SHA-256 digests, exact counts, operation binding, and ledger generation;
  their complete bytes produce the 256-bit snapshot ID; and
- `manifest` and descriptor-bounded physical message `exact-object` operations
  materialize only their requested bounded file. Stable message files contain no
  ordinal or visibility field. On-demand exact lookup beyond the descriptor bound
  remains at implementation step 7.

The query still uses current stock rows plus explicit metadata provenance only and
sets `ledger_history_merged=false`; it does not expose ledger entries. A direct
store/query integration test proves that an exact-object view retains its immutable
sanitized bytes after stock Goose archives the row, while a later deletion-driven
coverage-epoch change revokes that view. A concurrent WAL writer inserted a new row
between preflight and content loading without mixing generations: the pinned result
contained the old row/count set and a fresh operation contained the new set.

Twenty operation tests plus the verified-read-snapshot lifecycle test cover exact
session and visibility isolation, full fingerprints, per-field/row/aggregate/descriptor
limits, omission behavior, strict dynamic SQLite types, ledger absence/tampering,
canonical operation paths, stable sanitization, archive pinning, deletion revocation,
same-snapshot concurrency, and bounded injected failures. With this slice included,
`make all` passes Ruff, mypy, Pyright, and the complete ordinary suite: 177 tests passed
and 9 explicitly configured integration tests were skipped.

## Verified Goose writer contract

The tests must reproduce the pinned Goose implementation rather than a convenient
approximation:

- Tool-pair summarization is enabled by default unless explicitly disabled.
- A batch contains ten eligible tool pairs, and at most one batch is scheduled per
  `Agent::reply` call.
- A batch is eligible only when the visible eligible count is strictly greater than
  `cutoff + 10`.
- Goose first asks the provider to generate summaries. A provider failure for one pair
  causes no database mutation for that pair; other successfully generated summaries
  remain eligible for application.
- For each successful summary, Goose commits three separately visible writes in this
  order: archive the request row, archive the response row, insert the generated
  user-role/agent-only summary. A storage failure stops later writes in the apply loop.
- Generated summaries inherit the original response timestamp, so a newly inserted
  row can sort far behind the newest row.
- Goose persists tool-loop messages incrementally. A `session_context` request and its
  response advance the same session before the model can make its next tool call.
- Tool-pair summarization updates original rows in place, preserving SQLite row IDs and
  Goose message IDs.
- Stock Goose changes those rows to `agentVisible=false` without recording that they
  were previously agent-visible. An invisible stock row is therefore ambiguous: it
  might be an archived tool message or content intentionally hidden from the model.
- Full compaction is different: `replace_conversation` deletes and reinserts the
  conversation inside one `BEGIN IMMEDIATE` transaction. Readers see either the old or
  new conversation, never the empty intermediate state.
- The normal Goose session writer uses WAL mode. Rollback-journal behavior is not a
  normative product-path requirement.

These details determine the failure states, fixture sizes, cursors, and concurrency
barriers below.

## Required invariants

The implementation must preserve all of these properties while a hostile model makes
arbitrary valid `session_context` requests and Goose writes concurrently:

1. No row from another session, no never-agent-visible row, and no filtered content is
   disclosed.
2. One response describes one immutable operation view. Its counts, entries, bytes,
   hashes, and continuation state cannot mix database generations.
3. A path never aliases a different physical or logical message.
4. A continuation either returns bytes from its original pinned view or fails with a
   typed, bounded error. It never silently switches to current database state.
5. Repeated tool calls can make forward progress even though those calls append rows to
   the projected session.
6. Listing limits affect discovery only. A validated exact identity remains
   retrievable outside the recent window while its underlying identity exists and its
   project-owned disclosure-ledger record remains valid.
7. All host, cache, ContextFS-node, file, bundle, response, and query work is bounded.
8. Direct and Apptainer/FUSE transports implement the same public contract.

## Operation-pinned views

> **Implementation note:** The active milestone implements the core lifecycle below
> with a 256-descriptor recent window, on-demand physical exact lookup, pinned file
> chunks, and matching adapter error behavior. The original 8,192-descriptor complete
> namespace, cursor pagination, reversible logical lookup, and typed JSON error body in
> the remainder of this section are extended-hardening ideas, not descriptions of the
> current public contract.

### Why snapshot comparison is insufficient

Do not implement the original optional `expected_snapshot_id` design. A first tool call
is persisted before the second tool call begins, so a fresh projection necessarily has
a different generation. Retrying creates another tool exchange and another generation;
the requested old snapshot is already gone. This would make transcript chunking and
pagination livelock in exactly the real workflow the tests need to protect.

`snapshot_id` remains useful as an advisory database-generation fingerprint, but it is
not a continuation handle.

### View lifecycle

Add a framework-neutral `SessionViewStore`, instantiated once per stdio MCP server and
shared by all `session_context` calls handled by that server.

The first call for an operation:

1. opens one read-only SQLite transaction;
2. validates the exact session and disclosure-ledger coverage state;
3. reads a bounded descriptor set covering the entire projectable namespace;
4. materializes only the requested directory page or file bytes;
5. computes a full 256-bit snapshot fingerprint and exact file hashes;
6. stores the sanitized immutable descriptors/bytes in the bounded view store; and
7. returns an opaque, random `view_id` bound to the session, operation, path, and
   projection schema.

A continuation supplies `view_id` and the prior `next_offset` or `next_cursor`. Before
serving cached immutable material, it performs only a bounded session-existence and
ledger-coverage-epoch check. Ordinary inserts and visibility updates do not change that
epoch, so database writes after view creation are intentionally absent from the view.
A deletion or session removal revokes the view. A caller starts a new view to observe
ordinary additions.

Do not retain a long-lived SQLite transaction: that would hold WAL read state, permit
unbounded WAL growth, and couple correctness to process lifetime. Cache only sanitized
projection data; the continuation revocation check is a new short read transaction.

Use these initial hard limits, adjusting them only with measured tests:

- 8,192 descriptors per view;
- 4 MiB of descriptor data per view;
- 64 MiB of projectable raw source content considered per view, preflighted with SQL
  lengths before content is fetched;
- the existing 1 MiB per-file limit;
- 32 MiB total cached sanitized content per MCP process;
- four live views per session and sixteen per process;
- 128 directory entries per page; and
- a ten-minute idle lease, with LRU eviction inside the byte/count limits.

Creating excess views may evict older views from the same session. An evicted, expired,
unknown, wrong-session, or wrong-operation token returns a typed `view_expired` or
`view_mismatch` error. Tokens must carry at least 256 bits of entropy and must never
encode a database path, session secret, message content, or raw message ID.

If the descriptor set exceeds its row or byte bound, return `view_too_large`; do not
silently claim complete pagination. Explicitly bounded recent-tree operations may still
report truncation in their manifest.

The descriptor query must use `LIMIT max_rows + 1`, inspect source lengths before
returning content to Python, and substitute a deterministic omission descriptor for an
individual row over the source-content limit. It must reject an over-limit aggregate
before fetching the bounded rows' content. Row-count limits without source-byte limits
are insufficient because thousands of individually valid 512 KiB rows would otherwise
exhaust memory.

### Snapshot and file identity

The v3 `snapshot_id` is a full SHA-256 digest over a canonical descriptor containing:

- the projection schema and all projection options;
- the exact session ID and disclosure-ledger schema/coverage epoch;
- total/current/historical/projectable counts used by the manifest;
- every ordered projectable row's physical ID, logical ID status, role, timestamp,
  visibility class, and normalized-content digest; and
- all generated index/transcript inputs and truncation decisions.

It must not be the current truncated 80-bit hash over recent files. A mutation outside
the recent 256-message window must change the fresh fingerprint if it changes anything
queryable or reported by the projection.

Every public file envelope also reports the full SHA-256 of that view's exact file
bytes. Multi-chunk reads must supply their `view_id`; an optional
`expected_file_sha256` provides an additional caller check but does not replace the
pinned view.

## Operation-aware projection and transport

The current projector eagerly builds the whole tree. At maximum settings it already
uses roughly 256 message files plus 700 event files under a 1,024-node ceiling. Eagerly
duplicating 256 messages under a logical-ID namespace would exceed that ceiling, and
duplicated content can exceed the 8 MiB aggregate bundle limit.

Refactor projection around an explicit request before adding schema v3:

- `recent-tree`: a bounded convenience tree and manifest;
- `directory-page`: at most 128 entries from a cached ordered descriptor list;
- `exact-object`: one physical or logical message/event object;
- `transcript`: one bounded generation-scoped transcript file; and
- `manifest`: one bounded manifest.

Each Apptainer invocation receives only the directories and files necessary for that
operation. Both `Snapshot.from_files` and `encode_bundle` must succeed whenever the
corresponding direct operation succeeds.

Keep the baked in-container reader's envelope as a small internal transport contract.
After validating that envelope, the trusted host adds the public v2 metadata
(`schema_version`, `snapshot_id`, `view_id`, file hash, cursor data). Direct mode goes
through the same public-envelope builder. This avoids teaching the container how to
interpret host cache tokens and prevents old and new envelope key sets from being
accidentally mixed.

View and cursor validation happens on the host before Apptainer starts, so typed view
errors do not have to cross the current generic process-exit boundary. If the internal
reader contract itself changes, rebuild the ContextFS image in the same change.

Represent public failures with one framework-neutral error result and make both MCP
adapters emit the same JSON body with MCP `isError=true`. The body includes public
schema version, a stable code, retryability, and a bounded non-sensitive message. At
minimum distinguish `invalid_request`, `not_found`, `view_expired`, `view_mismatch`,
`view_too_large`, `database_busy`, and `transport_failure`.

Define and test one POSIX-like rule for offsets: an offset at or beyond EOF returns an
empty string and no next offset. The direct reader, mounted reader, and Apptainer host
validator currently disagree for offsets beyond EOF and must be aligned.

## Projection schema v3

### Stable object files

Identity files contain no ordinal, current/historical visibility, event ordinal, or
other generation-scoped field.

- `session/messages/by-source-row/<20-digit-row>.json` is the physical identity for a
  row. It may contain source row ID, message ID, role, creation time, normalized
  content, and omissions.
- `session/events/by-source-row/<row>-<content-index>.json` is the physical content-
  block identity and contains no snapshot ordinals or visibility.
- `session/messages/by-message-id/<encoded-id>.json` is an on-demand logical identity
  file. It excludes source row ID as well as generation fields, so retained originals
  are structurally capable of retaining the same bytes across row replacement. This
  does not override a stock-Goose deletion coverage-epoch revocation.

The stable-byte guarantee is scoped to supported Goose history transformations.
Explicitly test the other in-place writer, `update_tool_request_meta`: it changes
`content_json`, but the resulting provider `_meta` must remain filtered so identity
bytes do not change.

### Logical message-ID paths

Do not use a bare SHA-256 filename backed by an imaginary exact SQL lookup. Goose stores
only the raw message ID, so resolving a digest would require an unbounded scan or a new
indexed database column.

Use a strictly validated, reversible base64url encoding of the UTF-8 message ID, with a
version prefix. Decode it on the trusted host, enforce raw/encoded length and path-
component limits, and issue an exact parameterized query constrained by both session
and raw message ID. Missing, empty, oversized, malformed, or duplicate IDs receive an
explicit degraded logical-identity status and retain only physical addressing.

The fixture must reproduce Goose's duplicate-ID behavior: metadata lookup reads one
matching row and its update affects all rows with that `(session_id, message_id)`.

### Generation-scoped files

Move mutable state into view-scoped files/envelopes:

- a paged message index contains ordering, visibility, physical/logical paths, cursors,
  and the view/snapshot IDs;
- the transcript header contains its view and snapshot IDs;
- the manifest contains exact counts, ledger coverage status, limits, truncation reasons,
  logical-identity degradation, and complete file hashes for materialized files; and
- directory envelopes report `cursor` and `next_cursor` as well as `view_id`.

Byte-budget selection must have a documented meaning. Prefer a contiguous newest
suffix and stop at the first message that cannot fit, rather than skipping a large row
and admitting older small rows accidentally.

## Stock-Goose disclosure ledger

### Why a project-owned ledger is necessary

The tracked patch added `historicallyAgentVisible` before stock Goose hid a row. Its
only purpose was to provide evidence that re-disclosing that row to the same model was
safe. It was not a sandbox boundary. Production cannot depend on it.

There is no safe post-hoc inference for an already summarized stock-Goose session.
`agentVisible=false` covers both summarized former context and content that was never
shown to the model. Projecting all invisible rows would disclose the latter; excluding
them is safe but incomplete. Summary text does not contain a reliable structured link
back to every original row.

Preserve bounded Goose visibility evidence without changing Goose by installing a
project-owned capture ledger and SQLite triggers in the stock sessions database.
Extra tables/triggers are owned and versioned by this project; no Goose source patch or
custom binary is required.

### Atomic capture contract

During stdio extension initialization, before acknowledging MCP initialization and
before Goose can make its first provider request:

1. open the stock session database read-write with `BEGIN IMMEDIATE`;
2. create/verify namespaced ledger, coverage, accounting, and trigger objects;
3. register only the exact bound session in a project-owned managed-session table;
4. seed every currently `agentVisible` row for that session; and
5. commit before advertising tools.

Stock Goose supplies the session ID to its stdio extension as `AGENT_SESSION_ID`, so the
bootstrap can remain exactly session-scoped. On resume, an existing valid coverage
record continues the same ledger.

Use one static, idempotent trigger set guarded by membership in the managed-session
table; do not interpolate session IDs into trigger SQL and do not copy rows from
unregistered sessions. This supports multiple managed sessions in one Goose database
without per-session schema objects.

The triggers capture only registered rows proven visible by stock metadata:

- after insertion of an agent-visible row, insert its disclosed form;
- after an update that leaves or makes a row agent-visible, upsert its latest disclosed
  form; and
- before a transition from agent-visible to agent-invisible, ensure the `OLD` disclosed
  form has been captured.

Trigger writes occur in the same transaction as Goose's write. Therefore Goose cannot
commit a visible-to-invisible transition without also preserving the previously visible
row; polling and background-watcher races are eliminated. Trigger installation and
verification must be tested against unmodified Goose upgrades.

Store source row ID, session ID, logical message ID, role, creation time, bounded raw
content, bounded metadata needed for disclosure auditing, capture reason, coverage
epoch, and ledger schema version. Projection still performs the existing content and
audience sanitization. Never copy a row merely because it is invisible, and never
import an old invisible row during bootstrap.

Use per-row and per-session byte/count accounting. An oversized row receives a
deterministic omission record rather than unbounded raw content. When the ledger limit
is reached, the trigger uses `RAISE(ABORT, ...)` so Goose cannot commit the corresponding
insert or visible-to-invisible transition. A ledger quota, schema, or storage failure
must leave the original row visible or roll back the new row rather than silently lose
provenance. Surface a typed `ledger_overflow`/`ledger_unavailable` failure and do not let
a hostile model grow the database without bound.

### Coverage and deletion semantics

A new managed session is complete when bootstrap occurs before its first provider
request. A resumed session is complete only when its valid ledger coverage record
already exists. Bootstrapping an older session safely seeds current rows but must mark
history incomplete if any preexisting invisible/ambiguous rows exist.

Any deletion from the stock `messages` table advances a ledger coverage epoch. Rows
captured under an older epoch are no longer eligible for a fresh projection. This
conservative rule prevents explicitly cleared history from reappearing through the
ledger. It also means stock-Goose full compaction invalidates pre-compaction ledger
history; handling that separate delete/reinsert mechanism completely requires an
additional trusted lifecycle signal and is not part of the tool-pair summarization
gate.

At startup and before each fresh view, verify the names, SQL definitions, schema
version, session binding, accounting state, and coverage epoch of every project-owned
database object. Missing, altered, incomplete, overflowed, or unsupported ledger state
fails closed. A diagnostic view may still show current stock-Goose-visible rows, but it
must label history incomplete and never re-disclose an ambiguous invisible row.

## Deterministic test program

### A. Authoritative stock-Goose behavior

Run the enabled behavior tests against an unmodified pinned Goose build with the
project ledger installed. Produce a canonical SQL/JSON row-shape artifact consumed by
the Python fixtures. Copy the real Goose DDL, including `AUTOINCREMENT`, `timestamp`,
and `tokens`; add a conformance check that detects DDL, trigger, or artifact drift when
the pinned Goose source is available.

For fifty real batches, seed at least:

`cutoff + (50 * batch_size) + 1`

eligible tool pairs. With batch size ten and cutoff two, that is at least 503 pairs.
Call `Agent::reply` separately for every expected batch and assert that each reply
triggers at most one batch.

Identify auxiliary calls by the exact tool-pair-summarization system prompt/request
shape, not merely by the absence of tools. Assert `provider.manages_own_context()` is
false and verify for each successful batch:

- exactly ten summaries were requested and inserted;
- each summary has user role, agent-only metadata, a generated unique ID, and the
  original response timestamp;
- original physical row IDs, logical IDs, role, timestamp, and content are unchanged;
- stock Goose changes only visibility metadata on originals;
- the ledger retains the exact pre-transition disclosed form in the same commit;
- archived originals remain projectable from the ledger even though the stock row is
  now ambiguous and excluded; and
- unsummarized and never-agent-visible rows are not falsely captured or re-disclosed.

Separate failure cases causally:

- a provider summary failure performs no archival for that pair while later provider
  successes can still be applied;
- a storage/process failure after request archival leaves one archived half;
- a failure after response archival leaves two archived halves and no summary;
- a summary-insert failure leaves both originals archived; and
- an apply-loop storage failure prevents later generated summaries from being applied.

Also inject ledger-trigger/accounting failures and prove they cannot allow Goose to
commit an archival transition while silently losing the last captured eligible form.
Validate
the documented overflow behavior separately from ordinary Goose storage failures.

Add startup-order tests in which the provider immediately returns a final answer and
never calls a tool. Trigger installation and the visible-row baseline must still finish
before Goose schedules or applies summarization. Restart Goose/MCP independently and
prove that persistent triggers continue capturing registered-session writes while the
MCP process is absent. Alter or remove each trigger/table and prove the next managed
startup refuses to proceed.

Run global Goose configuration mutations in a process-isolated test or behind a scoped
guard that restores state even on panic. Do not rely on cleanup at the successful end
of a test.

### B. Python projection state machine

Load the canonical Goose fixture, add a decoy session and invisible secrets, and model
at least fifty batches in the exact commit order. Take a fresh view before the batch
and after every individual commit, using barriers rather than scheduler luck.

Assert after every state:

- no cross-session or unproven content appears;
- every proven, nondeleted identity is exactly retrievable;
- identity-file hashes remain stable through supported mutations;
- a path never aliases a different source or logical message;
- counts and the full snapshot fingerprint describe the same transaction;
- a fresh fingerprint changes for a relevant mutation outside the recent window;
- an already pinned view remains byte-for-byte unchanged during later writes;
- each generated summary appears exactly once after insertion; and
- complete pinned pagination has no gaps or duplicates.

Cover 255/256/257-message and 699/700/701-event boundaries, all node/file/bundle/cache
limits, old and duplicate timestamps, malformed rows, missing/duplicate/oversized
message IDs, duplicate-ID metadata updates, `update_tool_request_meta`, partial batches,
and the raw-source aggregate limit.

### C. Real Goose multi-call pinned-view test

Use the real project wrapper and a deterministic local protocol stub. Make the provider:

1. request the first directory page or transcript chunk;
2. copy the returned `view_id` and cursor/offset into a second `session_context` call;
3. continue for enough calls to cross multiple pages/chunks; and
4. start a fresh view and prove that it sees the tool exchanges accumulated after the
   first view was created.

Goose must persist the intervening tool requests/responses normally. The old view must
still complete exactly once without livelock or accidental inclusion of its own later
calls. Prove that ledger bootstrap and trigger verification completed before the first
provider request, and run this public API contract through both MCP implementations
using an unmodified Goose binary.

### D. Concurrent WAL writer/reader test

Run the state-machine writer concurrently with fresh-view creation and pinned-view
continuations. Pause deterministically between request archival, response archival, and
summary insertion. Every result must be one complete view or one documented bounded
error, never mixed counts, malformed JSON, path aliasing, or secret disclosure.

Exercise the real WAL modes: active writer, checkpoints, crash-leftover `-wal`/`-shm`
sidecars, missing sidecars under read-only access, and database/session replacement.
Normal WAL reads should not routinely return busy errors; unexpected busy rates are a
diagnostic failure. Rollback-journal tests may remain optional compatibility coverage,
not an acceptance requirement for Goose.

### E. Direct/FUSE public-contract parity

Compare results through `render_session_context`, including the real Apptainer host
validator and a real in-container FUSE mount. Do not compare only two readers over the
same prebuilt projection.

Cover file offsets at zero, EOF, and beyond EOF; empty files; UTF-8 boundaries; tail
reads; mutable database state between chunks; old exact objects; every page; expired
and mismatched views; maximum node and bundle sizes; and host decoration of the public
v2 envelope. Every direct-success operation must also fit and succeed through
`Snapshot.from_files` and `encode_bundle`.

### F. Summarization-disabled behavior

Environment-value tests remain useful but are not sufficient. Add a real-wrapper test
with more than `cutoff + 10` preexisting tool pairs:

1. inherit `GOOSE_TOOL_PAIR_SUMMARIZATION=true` and prove the wrapper overrides it;
2. set `GOOSE_AUTO_COMPACT_THRESHOLD=0` to isolate this mechanism;
3. use the same deterministic provider path as the enabled control;
4. return one ordinary final response;
5. assert zero requests with the exact summarization prompt;
6. assert no preexisting row changed visibility and no archival ledger transition was
   needed; and
7. assert no tool-pair summary row was inserted.

The inverse control bypasses only the wrapper override, explicitly enables
summarization, and must produce the expected batch through the same provider. This
prevents the disabled test from passing vacuously because of a provider short-circuit,
an insufficient pair count, or a broken mock.

Summarization itself is adapter-independent, so this expensive persisted-database test
need run once. Adapter signatures and the real multi-call view workflow remain
parameterized over both MCP implementations.

### G. Optional live smoke test

No live LLM is required for acceptance. After all deterministic gates pass, a short
live run may cross the real tool-call cutoff and confirm that managed Goose records no
archive transitions while disabled. It is evidence of deployment wiring, not a
substitute for any invariant above.

## Separate full-compaction gate

Do not hide full-compaction work inside the tool-pair summarization gate. Its atomic
delete/reinsert behavior has different identity and failure semantics.

With stock Goose, the ledger cannot safely distinguish `replace_conversation` used for
compaction from an explicit history clear using the same delete/reinsert storage path.
The safe baseline is therefore conservative: any delete advances the coverage epoch,
revokes pinned views, and makes pre-delete ledger rows unavailable to fresh views. A
post-compaction view can expose the new stock-agent-visible summary and other newly
proven rows, but cannot claim recovery of the deleted original detail.

The baseline full-compaction tests must prove:

- a reader sees only the complete pre- or post-compaction database state;
- a pre-delete pinned view is revoked before another chunk can disclose cached history;
- old physical source-row paths become nonexistent and are never reused;
- no pre-delete ledger row crosses the new coverage epoch; and
- clear followed by new messages cannot resurrect old ledger content.

Recovering retained original detail across stock-Goose full compaction requires a
future trusted lifecycle signal that unambiguously distinguishes compaction from clear.
Do not implement a heuristic based on row count, timestamps, summary text, or retained
message IDs and call it complete.

Tool-pair summarization can be declared hardened once its own gate passes even if this
separate full-compaction gate remains open.

## Implementation order

1. **Complete:** Implement and verify the stock-Goose ledger schema, triggers,
   bootstrap, coverage epochs, accounting, schema-v2 non-bricking degradation, and
   database/session incarnation binding.
2. **Complete:** Integrate ledger bootstrap into both MCP startup paths and exact-ledger
   verification into every public context request.
3. **Complete (internal foundation):** Add operation request/result types and the
   bounded `SessionViewStore`; test token binding, eviction, expiry, and resource
   limits.
4. **Complete:** Refactor public projection to operation-aware bounded descriptor
   queries and minimal operation bundles.
5. **Complete for the active milestone:** Expose a shared success envelope, matching
   adapter error behavior, and aligned direct/Apptainer list/read semantics.
6. **Complete for the active milestone:** Implement schema-v3 stable physical files,
   transcripts, full snapshot fingerprints, pinned multi-chunk reads, and the validated
   union of current stock rows with same-epoch ledger captures.
7. **Complete for physical identity:** Add on-demand exact physical lookup outside the
   recent window. Reversible logical-ID lookup remains deliberately deferred.
8. **Extended hardening:** Fifty-batch and exhaustive failure matrices are no longer an
   active acceptance gate; retain the deterministic three-batch and real enabled
   control coverage.
9. **Representative coverage complete:** WAL snapshot, public multi-call, and
   direct/Apptainer parity paths exist. Exhaustive interleaving matrices remain
   extended hardening.
10. **Complete early:** Extend the existing real-wrapper disabled regression and
    enabled inverse control to assert ledger startup ordering and enabled-mode capture.
11. Run the optional live smoke only after deterministic acceptance passes.

Do not retain intentionally failing tests in commits. Land each characterization test
with the implementation that makes its invariant pass.

## Tool-pair summarization acceptance gate

The projection is hardened for repeated tool-pair summarization only when all of the
following are true:

- every product-path test uses a stock Goose binary with no history-provenance patch;
- fifty or more real deterministic batches and every modeled partial commit state pass;
- atomic trigger tests prove no committed visible-to-invisible transition can outrun
  ledger capture;
- a real Goose multi-call traversal completes from one pinned view despite persistence
  of its own intervening tool exchanges;
- a fresh view observes relevant changes anywhere in the bounded queryable namespace,
  while an existing view remains immutable;
- proven old messages remain directly reachable after exceeding discovery limits;
- stable identity bytes do not change under summarization or filtered tool-metadata
  updates;
- pinned pagination is complete and duplicate-free;
- every successful operation remains below host cache, ContextFS node/file, bundle, and
  response limits;
- direct and FUSE public results agree for recent, historical, paged, exact, and error
  cases;
- missing, altered, incomplete, overflowed, and unsupported ledger states fail closed
  and are reported accurately;
- the enabled inverse control demonstrably creates repeated summaries with the exact
  persisted shape; and
- the project wrapper demonstrably produces zero auxiliary summaries and zero archive
  transitions despite an inherited true value and an exceeded cutoff.

Passing this gate requires no live language model.
