# Goose history-provenance patch

This patch is retained as a development experiment and reference. It is not part of
the supported product runtime, which must work with stock, unmodified Goose. Do not
build deployment assumptions or acceptance tests around this patch.

The supported replacement capture mechanism is now the project-owned disclosure
ledger installed by both MCP startup paths. The current projector does not consume
ledger history yet, so the patch remains comparison evidence rather than a shortcut
around the operation-pinned-view and ledger-integration work.

`0001-preserve-agent-visible-history-provenance.patch` was developed and verified
against Goose commit `ee61c7c499dbf08786a75948d949639cbab14150` (version `1.45.0`).

For comparison experiments only, apply it idempotently to a clean or compatible Goose
checkout:

```bash
GOOSE_SOURCE_DIR=/absolute/path/to/goose make goose-patches
```

Without `GOOSE_SOURCE_DIR`, the helper uses the repository-local `goose-dev` path.

The patch adds the serialized `historicallyAgentVisible` message-metadata field and
sets it when full compaction or tool-pair summarization archives a row that is
currently agent-visible. It deliberately leaves ordinary user-only/invisible rows
unmarked. The Python projector treats this explicit field as permission to re-disclose
the archived row to the same session.

Rows compacted before this patch was installed cannot be classified retroactively and
remain excluded. Re-check and rebase the patch when upgrading Goose; a failed
`git apply --check` is a compatibility failure, not a reason to apply it fuzzily.
