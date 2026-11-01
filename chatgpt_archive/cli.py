"""Command-line entry point for the local ChatGPT archive."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .archive import (
    ArchiveError,
    default_archive_root,
    ingest,
    list_runs,
    search,
    validate,
)


def _add_archive_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help="Raw archive root (default: ~/.chatgpt-local-backup or $CHATGPT_ARCHIVE_ROOT)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite path (default: <archive-root>/index.sqlite3)",
    )


def resolve_archive_root(args: argparse.Namespace) -> Path:
    if getattr(args, "archive_root", None) is not None:
        return Path(args.archive_root).expanduser()
    return default_archive_root()


def resolve_database(args: argparse.Namespace) -> Path:
    if getattr(args, "database", None) is not None:
        return Path(args.database).expanduser()
    return resolve_archive_root(args) / "index.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local-first ChatGPT export archive (standalone; Octop is not required)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ingest_parser = commands.add_parser("ingest", help="copy and index an export")
    ingest_parser.add_argument("source", type=Path)
    ingest_parser.add_argument("--account", required=True)
    ingest_parser.add_argument(
        "--source-kind",
        choices=("open_source", "official", "manual"),
        default="open_source",
    )
    _add_archive_root(ingest_parser)

    validate_parser = commands.add_parser("validate", help="compare primary and official exports")
    validate_parser.add_argument("primary", type=Path)
    validate_parser.add_argument("official", type=Path)
    validate_parser.add_argument("--report", type=Path, default=None)

    search_parser = commands.add_parser("search", help="search the SQLite FTS5 index")
    search_parser.add_argument("query")
    search_parser.add_argument("--account", default=None)
    search_parser.add_argument("--limit", type=int, default=20)
    _add_archive_root(search_parser)

    runs_parser = commands.add_parser("runs", help="list captured runs")
    runs_parser.add_argument("--account", default=None)
    _add_archive_root(runs_parser)
    return parser


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "ingest":
        return ingest(
            args.source,
            account_id=args.account,
            source_kind=args.source_kind,
            archive_root=resolve_archive_root(args),
            db_path=resolve_database(args),
        )
    if args.command == "validate":
        result = validate(args.primary, args.official)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return result
    if args.command == "search":
        return {
            "query": args.query,
            "results": search(
                resolve_database(args),
                args.query,
                account_id=args.account,
                limit=args.limit,
            ),
        }
    return {"runs": list_runs(resolve_database(args), account_id=args.account)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except ArchiveError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
