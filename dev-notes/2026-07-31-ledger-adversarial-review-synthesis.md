# Disclosure-ledger adversarial review synthesis

**Status:** scoped milestone implemented and verified, 2026-07-31

## Decision

The disclosure ledger is a reliability mechanism for a bounded session projection. It
is not an audit system and it does not need to reconstruct the exact bytes sent to
every model provider.

The milestone is complete when ordinary and reasonably malformed Goose session data
cannot make the projector crash, perform unbounded work, mix sessions or database
generations, alias one message as another, or report a stronger fact than the available
evidence supports. The representation may group uncommon cases into coarse states such
as `ambiguous`, `omitted`, `unavailable`, or `atLeast`, provided those states remain
stable and are not factually false.

This decision narrows the more exhaustive program in the
[tool-pair summarization hardening plan](2026-07-31-tool-pair-summarization-hardening-plan.md).
That plan remains useful as a catalog of possible hardening work, but its fifty-batch,
complete-namespace, exact-count, and exhaustive transport matrices are not gates for
the current milestone.

The breadth-first implementation described here is now present. Schema-v2 ledger
capture degrades without aborting Goose writes, schema-v3 operation views merge current
and valid same-epoch captured rows, every public request revalidates the exact ledger
and database/session incarnation, and bounded process-local views pin continuations.
The rest of this note preserves the review reasoning and the stop rule used to avoid
turning the ledger into an audit or arbitrary-database-authentication system.

## Trust boundary

The hostile language model may make arbitrary valid MCP calls and may cause stock Goose
to append or summarize ordinary session messages. It cannot write the sessions database
directly from the sandbox. Stock Goose and this project's installer are trusted
database writers.

Legacy, partially upgraded, or corrupt database values are possible inputs. They must
be rejected, omitted, or represented as degraded using bounded work. The current goal
does not include authenticating the database against an arbitrary local SQL writer.
Consequently, ledger hashes, triggers, and schema checks detect incompatibility and
ordinary corruption; they are not tamper-proof provenance.

## Representation contract

The projection should make only these coarse claims:

- **Currently eligible:** a valid current Goose row says it is eligible for agent
  context.
- **Ledger captured:** this project captured the row, in the same session incarnation
  and coverage epoch, while Goose metadata said it was eligible for agent context.
- **Ambiguous or unavailable:** the evidence is absent, malformed, stale, incomplete,
  or outside a bound, so the row is not projected as historical content.

`agentVisible` is Goose's eligibility marker. It is not proof that a provider received
every content block. Provider adapters may omit UI/control blocks, and Goose may
persist some agent-only control responses without making another provider request.
Names and documentation must therefore avoid claims such as “actually disclosed,”
“provider-wire exact,” or “last bytes seen by the model.” Known UI/control-only block
types should be conservatively omitted or placed in an explicitly non-conversational
bucket; an exhaustive provider-by-provider reconstruction is not required.

Counts and listings may be capped. A result with `atLeast: 8192` and `truncated: true`
is preferable to an exact full-session scan. Recent listings should be a bounded,
contiguous newest suffix. Exact access may use a validated physical row identity even
when that row falls outside the listing window.

## Review findings and disposition

Two independent adversarial reviews agreed on the principal boundary problems. One
review also reproduced several malformed-data and database-replacement cases against
the current implementation. The findings are grouped by the amount of work justified
for this milestone.

| Finding | Disposition |
| --- | --- |
| The stock projection still accepts the experimental `historicallyAgentVisible` metadata field. A session row can therefore claim unsupported history without a same-epoch ledger entry. | **Implemented.** The supported stock predicate accepts only valid current `agentVisible` rows; historical content requires a valid same-incarnation, same-epoch capture. |
| A module-level server can expose `session_context` without the normal startup bootstrap, and startup binding alone does not protect every request. | **Implemented.** Module-level server singletons were removed and each public operation verifies the exact ledger/session binding. |
| Some dynamic SQLite values, notably `created_timestamp`, can be fetched or serialized without a useful type/byte bound. The legacy projector also fetches content before applying its result limit. | **Implemented.** Public operations use type-checked preflight queries, bounded values, capped counts, and bounded materialization. Invalid rows are omitted or degraded. |
| A pinned view is bound to a session ID and ledger epoch but not sufficiently to the database/session incarnation. Replacing the database with another containing the same identifiers can preserve a stale view. | **Implemented.** Views bind to opaque database and session-incarnation digests in addition to ledger generation. |
| `NULL` or malformed message metadata has different semantics in stock Goose and the projector. | **Handle conservatively.** Normal Goose serializes valid metadata. For legacy or corrupt rows, omit the row from historical projection and report degraded coverage instead of guessing. |
| Full counts, namespace discovery, and descriptor scans can become unbounded, while byte-budget selection may skip a large row and admit older small rows. | **Implemented.** Counts and recent discovery are capped; byte selection is a contiguous newest suffix; exact physical lookup is independent of the recent window. |
| Per-session ledger quotas accumulate across epochs and can eventually abort ordinary Goose writes. | **Implemented.** Quota/accounting failure advances the epoch, disables capture, and allows the Goose write to commit. |
| Ledger rows can be refreshed in place and content integrity is not authenticated against a same-database writer. | **Document, do not deepen.** Stop calling the ledger append-only or tamper-proof. Strict schema, type, accounting, and cross-field validation are sufficient for the trusted-writer boundary. |
| Session deletion leaves retained ledger content and there is no global retention policy. | **Defer full lifecycle work.** Record the limitation and add an explicit cleanup/retention facility before broad multi-user deployment; it does not block bounded parsing and projection. |
| Row-ID mutation, unusual `INSERT OR REPLACE` conflicts, arbitrary extra SQLite objects, repeated random-token injection, and foreign-key-disabled session reuse create exotic failure modes. | **Bucket and defer.** Incarnation binding plus conservative omission covers the meaningful consequence. Do not build separate machinery for every arbitrary SQL-writer behavior. |
| Current tests sometimes use words such as “prove” for an ordering observation or a narrower protocol path. | **Correct claims as touched.** Keep representative tests, but describe exactly the path and invariant they exercise. |

## Implemented ledger milestone

The breadth-first boundary pass completed these items before freezing the ledger format:

1. Removed the experimental historical metadata predicate from the stock path.
2. Required a verified exact-session ledger binding at every public `session_context`
   request and removed unprepared module-level server instances.
3. Bounded and type-checked values entering descriptors, files, manifests, and errors;
   invalid rows become omitted or degraded.
4. Bound views to a database and session incarnation in addition to the coverage epoch.
5. Adopted the truthful visibility vocabulary and filtered known UI/control-only
   content without attempting exact provider-wire replay.
6. Implemented non-bricking quota behavior so lost historical coverage degrades the
   projection instead of aborting ordinary Goose writes.

The operation projection then exposed this small, stable public surface:

- bounded manifest with capped counts and explicit truncation/degradation;
- bounded recent-message listing;
- bounded transcript chunks;
- exact physical-row lookup with validated identifiers; and
- one shared success envelope and matching error semantics for the FastMCP and official
  MCP adapters.

The merge rule is deliberately simple: combine valid current rows with valid
same-incarnation, same-epoch ledger captures in one read snapshot; omit everything
ambiguous. Stable files should contain stable facts. Ordinals, current visibility,
counts, and cursors belong in the view-scoped manifest or index.

## Proportionate tests

Keep tests that cover different failure classes, not every permutation:

- unsupported historical metadata alone never admits a row;
- missing or mismatched request/session preparation fails closed;
- oversized or wrongly typed timestamp, ID, metadata, and content values remain
  bounded and yield omission/degradation;
- cross-session rows never appear;
- replacing the database or reusing a session identity revokes a pinned view;
- one operation sees one SQLite/WAL snapshot;
- quota exhaustion degrades historical coverage without breaking an ordinary Goose
  write;
- two or three deterministic summarization batches preserve reachable captured history;
- the disabled control produces no summarization; and
- representative direct and Apptainer/FUSE list/read/error results agree.

One stock-Goose integration run for enabled summarization and one disabled control are
enough for this milestone. A live language model is optional smoke coverage. Fifty
batches, all provider formatter differences, exhaustive malformed DDL, every FUSE/WAL
interleaving, and exact enumeration of an arbitrarily large namespace are extended
hardening, not acceptance requirements.

## Stop rule

Stop ledger work and return to broader sandbox/tool development when all of the
following are true:

- supported inputs and representative malformed rows cannot cause unbounded reads,
  serialization, or cache growth;
- no other-session row or ambiguous historical row is projected;
- a response cannot mix database generations or reuse a path for different content;
- database/session replacement revokes pinned state;
- manifests and errors report coarse evidence, limits, and degradation truthfully;
- repeated summarization works in a small deterministic stock-Goose test; and
- summarization remains absent when the project says it is disabled.

That is the intended “first 80% done very well, remaining 20% done sufficiently”
boundary. Further ledger work should require either a newly observed production
failure or a deliberate expansion of the threat model.

## Verification at completion

`make all` passed Ruff, mypy, Pyright, and the complete ordinary suite: 191 tests
passed and 9 opt-in external-runtime tests were skipped because no real Goose/session
fixture was selected. `make test-apptainer-contextfs` also passed the real
Apptainer/FUSE mechanics, isolation, projection-policy, and cleanup proof. The
previously recorded stock-Goose enabled/disabled summarization control remains the
real-writer integration evidence; a live language model is not an acceptance gate.
