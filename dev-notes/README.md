# Development notes

This directory records implementation experiments, local verification, and the reasons
behind choices that are too provisional or historical for the user-facing guides.

- [2026-07-30: project bootstrap and Goose integration](2026-07-30-bootstrap.md)
- [2026-07-30: rootless Apptainer bring-up](2026-07-30-apptainer-bring-up.md)
- [2026-07-31: Apptainer research and project recommendations](2026-07-31-apptainer-research-and-recommendations.md)
- [2026-07-31: dynamic context filesystems, in-container FUSE, and nesting](2026-07-31-dynamic-context-fuse-and-nesting.md)
- [2026-07-31: ContextFS Apptainer proof](2026-07-31-contextfs-apptainer-proof.md)
- [2026-07-31: Goose session context projection](2026-07-31-goose-session-context-projection.md)
- [2026-07-31: real-session ContextFS test](2026-07-31-real-session-contextfs-test.md)
- [2026-07-31: live session-context driver](2026-07-31-live-session-context-driver.md)
- [2026-07-31: Goose control environment policy](2026-07-31-goose-control-environment.md)
- [2026-07-31: tool-pair summarization hardening plan](2026-07-31-tool-pair-summarization-hardening-plan.md)
- [2026-07-31: disclosure-ledger adversarial review synthesis](2026-07-31-ledger-adversarial-review-synthesis.md)
- [2026-07-31: ledger and operation-view follow-up adversarial review](2026-07-31-ledger-follow-up-adversarial-review.md)
- [2026-08-04: approximating Apptainer with Bubblewrap — system design](2026-08-04-bubblewrap-apptainer-approximation-design.md)

Commit records are stored in [`commit-plans/`](commit-plans/), beginning with the
[initial commit plan](commit-plans/2026-07-30-initial-commit.yaml).

The normative design and operating guidance remain in [`docs/`](../docs/). If a
development note and a current guide disagree, the current guide takes precedence.
Generated test state, model logs, dependency caches, and SIF images live under the
Git-ignored `.sandbox/` directory and are not part of this record.
