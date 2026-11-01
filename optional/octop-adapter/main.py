"""Optional Octop adapter for the standalone ChatGPT archive.

Not required. Ingest, validate, search, and list runs with the
``chatgpt-archive`` command without installing Octop. Load this module only
when an Octop agent should expose that same core as tools.

The default vault is ``~/.chatgpt-local-backup`` (or ``$CHATGPT_ARCHIVE_ROOT``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load_core():
    """Import the standalone archive. Fall back to a repo checkout if needed."""
    try:
        from chatgpt_archive.archive import (
            default_archive_root,
            ingest,
            list_runs,
            search,
            validate,
        )
    except ImportError as exc:
        repo_root = Path(__file__).resolve().parents[2]
        package = repo_root / "chatgpt_archive" / "archive.py"
        if not package.is_file():
            raise ImportError(
                "chatgpt-local-backup is not installed. This Octop adapter is optional; "
                "install the package with `pip install -e .` before loading the plugin."
            ) from exc
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from chatgpt_archive.archive import (
            default_archive_root,
            ingest,
            list_runs,
            search,
            validate,
        )
    return default_archive_root, ingest, list_runs, search, validate


default_archive_root, ingest, list_runs, search, validate = _load_core()


def _config(tool_name: str) -> tuple[Path, Path]:
    from octop_harness.plugins import get_tool_config

    cfg = get_tool_config(tool_name) or {}
    configured = cfg.get("archive_root")
    archive_root = Path(str(configured)).expanduser() if configured else default_archive_root()
    database = cfg.get("database_path")
    db_path = Path(str(database)).expanduser() if database else archive_root / "index.sqlite3"
    return archive_root, db_path


async def archive_ingest(source_path: str, account_id: str, source_kind: str = "open_source") -> str:
    """Copy a local export into the raw archive and index its conversations."""
    archive_root, db_path = _config("archive_ingest")
    result = ingest(
        source_path,
        account_id=account_id,
        source_kind=source_kind,
        archive_root=archive_root,
        db_path=db_path,
    )
    return json.dumps(result, ensure_ascii=False)


async def archive_validate(primary_path: str, official_path: str) -> str:
    """Compare an open-source export with the official ZIP as a validation layer."""
    return json.dumps(validate(primary_path, official_path), ensure_ascii=False)


async def archive_search(query: str, account_id: str = "", limit: int = 20) -> str:
    """Search the local ChatGPT archive with SQLite FTS5."""
    _archive_root, db_path = _config("archive_search")
    return json.dumps(
        {
            "query": query,
            "results": search(db_path, query, account_id=account_id or None, limit=limit),
        },
        ensure_ascii=False,
    )


async def archive_runs(account_id: str = "") -> str:
    """List captured runs and their manifest hashes."""
    _archive_root, db_path = _config("archive_runs")
    return json.dumps(
        {"runs": list_runs(db_path, account_id=account_id or None)},
        ensure_ascii=False,
    )


def setup(ctx: Any) -> None:
    """Register archive tools on an Octop plugin context. Octop-only."""
    fields: list[dict[str, Any]] = [
        {"name": "archive_root", "type": "text", "required": False},
        {"name": "database_path", "type": "text", "required": False},
    ]
    ctx.tool(
        "archive_ingest",
        archive_ingest,
        description="Archive a local ChatGPT export",
        config_fields=fields,
    )
    ctx.tool(
        "archive_validate",
        archive_validate,
        description="Validate an open-source export against the official export",
        config_fields=fields,
    )
    ctx.tool(
        "archive_search",
        archive_search,
        description="Search the local ChatGPT archive",
        config_fields=fields,
    )
    ctx.tool(
        "archive_runs",
        archive_runs,
        description="List local ChatGPT archive runs",
        config_fields=fields,
    )
