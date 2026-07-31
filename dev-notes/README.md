# Development notes

This directory records implementation experiments, local verification, and the reasons
behind choices that are too provisional or historical for the user-facing guides.

- [2026-07-30: project bootstrap and Goose integration](2026-07-30-bootstrap.md)
- [2026-07-30: rootless Apptainer bring-up](2026-07-30-apptainer-bring-up.md)
- [2026-07-31: Apptainer research and project recommendations](2026-07-31-apptainer-research-and-recommendations.md)
- [2026-07-31: dynamic context filesystems, in-container FUSE, and nesting](2026-07-31-dynamic-context-fuse-and-nesting.md)

Commit records are stored in [`commit-plans/`](commit-plans/), beginning with the
[initial commit plan](commit-plans/2026-07-30-initial-commit.yaml).

The normative design and operating guidance remain in [`docs/`](../docs/). If a
development note and a current guide disagree, the current guide takes precedence.
Generated test state, model logs, dependency caches, and SIF images live under the
Git-ignored `.sandbox/` directory and are not part of this record.
