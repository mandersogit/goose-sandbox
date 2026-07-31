# Live session-context driver

> **Current contract:** The successful run recorded below used the earlier optional
> provenance-patch experiment. New supported runs use stock, unmodified Goose and
> force tool-pair summarization off. The driver's optional `--goose-source` input now
> records only a clean stock checkout; it does not require or accept a locally modified
> patch checkout. Historical recovery under enabled summarization is planned, not yet
> claimed, in `2026-07-31-tool-pair-summarization-hardening-plan.md`.

The sustained driver is implemented in `sandboxed_goose.live_test`. It separates two
phases over one isolated Goose root and exact session ID; the audit phase resumes the
session only after the initial phase has passed.

The `initial` phase runs 10–200 sequential real-model turns. Each turn places a unique
canary in the new user prompt, obtains the current transcript size with a one-byte
`session_context` read, and then uses `tail=true` to read a bounded 2 KiB tail. The
trusted MCP process converts that request to a concrete nonnegative UTF-8 boundary in
the fresh projection before launching the unchanged fixed reader. Both calls are forced
through the Apptainer-FUSE transport. A trusted host-side oracle reads the isolated
SQLite database after every Goose process and requires:

- exactly the selected adapter's namespaced `session_context` tool;
- correlated successful request and response IDs;
- the exact mandatory path, offset, and limit arguments;
- a valid read-only file envelope containing the current canary;
- an advancing exact-session projection and source-row count;
- absence of the same-database decoy canary; and
- the exact persisted assistant sentinel.

The driver fails without retrying and retains its complete mode-`0700` run directory.
It records one JSON report per successful turn, plus prompts, stdout, stderr, model
metadata, executable/image digests, Git provenance, state, and a summary. Its provider
environment is allowlisted; unrelated credentials and proxy settings are not forwarded
to Goose or its MCP subprocess.

The `audit` phase resumes only a passed initial run. Task one selects the prior initial
assistant reply from the trusted projection. Each later task selects the preceding
audit assistant reply. The model must list `session/messages/by-source-row`, read that
exact JSON file, and return its projection-only ordinal, source row ID, message ID,
creation time, role, and visibility in a strict plain-text record. The host independently
validates both the tool result and final record. This makes the projected filesystem
necessary even without inducing compaction: ordinary conversation context does not
contain those normalized projection fields.

The first larger live attempt exposed a time-of-check/time-of-use problem in ordinal
filenames: Goose's default-enabled tool-pair summarizer inserted 10 summary rows after
the host selected a target but before the MCP read, causing the same ordinal path to
name a different message. This was proactive per-tool history rewriting, not full
token-threshold compaction. The projection schema now addresses message and event files
by immutable SQLite source-row ID. `ordinal` and `contextVisibility` are intentionally
snapshot-relative fields, so the oracle pins stable message identity while deriving
those two values from the exact tool result observed by the model. The chained target
is always the preceding audit reply, keeping it within the bounded recent-message
projection even after truncation.

All project-managed Goose invocations now force
`GOOSE_TOOL_PAIR_SUMMARIZATION=false`. The shared wrapper overrides even an inherited
`true` value, and the live driver's sanitized environment applies the same invariant to
its direct isolation-control invocation. Stable source-row addressing remains required
for ordinary token-threshold compaction and other legitimate visibility changes.

## First live result

The first successful live tier on 2026-07-31 ran 10 resumed turns through the official
SDK adapter. All 10 strict reports passed. The database contained one two-message decoy
session and one 60-message primary session; the primary made exactly 20 tool calls, all
to its namespaced `session_context`. The projected transcript grew from 857 bytes at
the first size read to 47,068 bytes at the final tail read. Snapshot IDs and source-row
counts advanced on every turn, no report observed the decoy marker, the final
projection remained untruncated, and the per-read ContextFS run directory was empty at
reconciliation. Provider, model, host, prompts, outputs, and raw session artifacts stay
in the ignored private run directory rather than this public note.

## Larger live result

The same session subsequently completed 10 chained audit tasks. Every successful task
made exactly two calls to the official-SDK adapter's namespaced `session_context`: one
directory listing and one selected-message read. Task 1 preceded the addressing fix;
tasks 2–10 used stable source-row paths. The final database contained 157 primary-session
rows and the two-row decoy. Projection schema 2 exposed all 157 primary rows (116 current
and 41 historical) as 157 message and 157 event files without truncation; the decoy
marker remained absent. The private Apptainer run directory was empty after
reconciliation.

This retained run exercised the stable-addressing fix rather than merely avoiding its
failure mode. During task 7, the then-enabled tool-pair summarizer inserted 10 rows
between host selection and the FUSE-backed read. The selected message's snapshot
ordinal moved from 123 to 133, but its source-row path and source row ID remained
unchanged, and the model returned the exact fields from the file it actually read. The
strict oracle accepted that task and all subsequent tasks. Three earlier failed audit
attempts remain recorded as evidence of two provider final-format failures and the
ordinal-path race; recovery was explicit rather than automatic. Future project runs
disable that summarizer by construction.
