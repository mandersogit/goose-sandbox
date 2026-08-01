# Ledger and operation-view follow-up adversarial review

**Status:** reproduced findings fixed; final verification recorded below

Two independent Sol reviews and one unbudgeted, unsteered Fable review examined the
ledger-v2/schema-v3 checkpoint. The reviewers were asked to find concrete bugs and
gaps, while retaining the 80/20 boundary in the earlier
[review synthesis](2026-07-31-ledger-adversarial-review-synthesis.md). Each review was
read-only. Findings were reproduced locally before they entered the fix set.

## Fix set

- Eligibility now accepts only JSON boolean `true`; numeric `1`, `1.0`, strings, null,
  arrays, objects, malformed JSON, and oversized metadata do not grant eligibility.
- Two verified project-owned indexes bound ordinary current-row count and recent-window
  queries. The schema fingerprint pins both indexes, so a missing or altered index fails
  closed instead of silently restoring a full hidden-row scan.
- The ledger metadata contains a random persisted database nonce. View identity combines
  it with the filesystem identity, so atomic replacement and same-inode replacement by
  an independent database both revoke pinned views.
- A valid source row whose pretty normalized representation exceeds the file limit now
  becomes a stable `normalized-content-byte-limit` omission. It no longer makes manifest,
  recent-tree, transcript, exact-object, or the legacy whole-tree projector fail.
- Bounded SQLite text is fetched as bytes and decoded explicitly. Invalid UTF-8 message
  IDs become `invalid`; invalid UTF-8 content becomes an explicit omission. Current and
  ledger-captured forms remain stable across archival.
- Oversized and dynamically typed message IDs retain a stable coarse status across
  archival, and bytes that will not be retained no longer consume the recent-window
  identity budget.
- The sustained live driver recognizes declared lower-bound counts. Once the capped
  count saturates, marker presence and snapshot advancement continue to prove progress.
- The official SDK adapter now advertises and enforces `additionalProperties: false`,
  matching FastMCP for all three tools.
- Direct and Apptainer/FUSE reads now agree at and beyond EOF, and the host validator
  rejects content extending past the declared file size. A real subprocess test also
  exercises timeout process-group cleanup.
- Direct library callers share a bounded process-default view store, so a returned
  `view_id` is continuable. Dead v1 overflow handling and an unused operation enum were
  removed.

## Compatibility consequence

These changes deliberately alter the exact schema-v2 fingerprint while the package is
pre-alpha. A database containing the earlier fingerprint fails closed; there is no
automatic in-place migration. Recreate the isolated test Goose state/database before
using this revision. Keeping the numeric schema label at v2 reflects that this is the
review fix for the same not-yet-released format; the fingerprint is the exact runtime
compatibility key.

## Deliberately bucketed work

The project still does not authenticate an arbitrary writer with direct SQL access.
Unusual `INSERT OR REPLACE` displacement, invisible cross-session row moves, rollback to
a clone carrying the same database nonce, and payload forgery remain in that bucket.
Exact provider-wire replay, global retention/garbage collection, and exhaustive
provider/FUSE/WAL matrices also remain outside this milestone. These are stated
limitations, not stronger claims hidden behind the representation.

The owned indexes bound the normal stock-Goose query plans used by public operations.
A generalized SQLite progress-handler/deadline layer would be defense against a broader
corrupt-engine or arbitrary-writer threat model and is not required for this checkpoint.

## Verification

`make all` passed Ruff, mypy, Pyright, and the complete ordinary suite: 210 tests
passed and 9 opt-in external-runtime tests were skipped because no real Goose/session
fixture was selected. `make test-apptainer-contextfs` passed the real Apptainer/FUSE
mechanics, isolation, projection-policy, EOF, and cleanup proof.
