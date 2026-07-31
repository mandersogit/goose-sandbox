"""Constrained entry point for the in-container ContextFS frontend."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sandboxed_goose.contextfs.bundle import load_bundle
from sandboxed_goose.contextfs.model import toy_snapshot

APPTAINER_FUSE_MOUNTPOINT = "/dev/fd/3"
APPTAINER_SESSION_BUNDLE = "/run/sandboxed-goose/session-context.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse only the argument shape emitted by attached Apptainer FUSE mode."""

    parser = argparse.ArgumentParser(
        description="Serve a bounded, read-only ContextFS snapshot inside Apptainer."
    )
    parser.add_argument("--bundle", help="use the trusted session snapshot bundle")
    parser.add_argument("mountpoint")
    parser.add_argument("-f", "--foreground", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.mountpoint != APPTAINER_FUSE_MOUNTPOINT:
        parser.error(f"mountpoint must be {APPTAINER_FUSE_MOUNTPOINT}")
    if not args.foreground:
        parser.error("attached Apptainer mode must supply -f")
    if args.bundle is not None and args.bundle != APPTAINER_SESSION_BUNDLE:
        parser.error(f"bundle must be {APPTAINER_SESSION_BUNDLE}")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    """Load the approved generation and serve it on Apptainer's FUSE descriptor."""

    args = parse_args(argv)
    try:
        from sandboxed_goose.contextfs.fuse import serve
    except ModuleNotFoundError as error:
        if error.name in {"pyfuse3", "trio"}:
            raise SystemExit(
                "ContextFS requires the image-provided pyfuse3 and trio packages"
            ) from error
        raise
    snapshot = load_bundle(Path(args.bundle)) if args.bundle is not None else toy_snapshot()
    serve(snapshot, args.mountpoint)


if __name__ == "__main__":
    main()
