"""Local-first ChatGPT export archive primitives.

Raw copied runs are the source of truth. SQLite is only a rebuildable index.
The module uses only the Python standard library and does not depend on Octop.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ContextManager, Iterable, Iterator

SOURCE_KINDS = {"open_source", "official", "manual"}
ARCHIVE_ROOT_ENV = "CHATGPT_ARCHIVE_ROOT"


def default_archive_root() -> Path:
    """Return the vault directory, independent of any host application.

    ``CHATGPT_ARCHIVE_ROOT`` overrides the default ``~/.chatgpt-local-backup``.
    """
    override = os.environ.get(ARCHIVE_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chatgpt-local-backup"


class ArchiveError(ValueError):
    """Raised when an export cannot be safely archived or compared."""


@dataclass(frozen=True)
class FileRecord:
    rel_path: str
    size: int
    sha256: str
    media_type: str | None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_account_id(account_id: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in account_id.strip())
    if not value:
        raise ArchiveError("account_id must not be empty")
    return value[:80]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(source: Path) -> list[Path]:
    if not source.exists():
        raise ArchiveError(f"source path does not exist: {source}")
    if source.is_file():
        return [source]
    return sorted(
        path for path in source.rglob("*")
        if path.is_file() and ".git" not in path.parts
        and not path.name.endswith((".db", ".sqlite", ".sqlite3"))
    )


def build_manifest(source: Path) -> list[FileRecord]:
    files = _source_files(source)
    base = source.parent if source.is_file() else source
    return [
        FileRecord(
            rel_path=path.name if source.is_file() else path.relative_to(base).as_posix(),
            size=path.stat().st_size,
            sha256=sha256_file(path),
            media_type=mimetypes.guess_type(path.name)[0],
        )
        for path in files
    ]


def _manifest_hash(records: Iterable[FileRecord]) -> str:
    payload = [record.__dict__ for record in records]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@contextmanager
def _export_root(path: Path) -> Iterator[Path]:
    """Yield a directory for either an unpacked export or a ZIP export."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve()
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    destination = (target / member.filename).resolve()
                    if destination != target and target not in destination.parents:
                        raise ArchiveError("zip export contains an unsafe path")
                    archive.extract(member, target)
            yield target
        return
    yield path


def _archive_parse_root(raw_run: Path) -> ContextManager[Path]:
    zip_files = list(raw_run.glob("*.zip"))
    if len(zip_files) == 1:
        return _export_root(zip_files[0])
    return _export_root(raw_run)


def _json_candidates(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix.lower() == ".json" and root.name != "manifest.json":
            yield root
        return
    for path in root.rglob("*.json"):
        if path.name != "manifest.json" and ".git" not in path.parts:
            yield path


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _conversation_id(item: dict[str, Any], fallback: str) -> str:
    for key in ("conversation_id", "id", "uuid"):
        if item.get(key):
            return str(item[key])
    return f"generated:{hashlib.sha256(fallback.encode()).hexdigest()[:20]}"


def _messages(item: dict[str, Any]) -> list[dict[str, Any]]:
    messages = item.get("messages")
    if isinstance(messages, list):
        return [entry for entry in messages if isinstance(entry, dict)]
    mapping = item.get("mapping")
    if not isinstance(mapping, dict):
        return []
    result: list[dict[str, Any]] = []
    for node in mapping.values():
        if isinstance(node, dict) and isinstance(node.get("message"), dict):
            result.append(node["message"])
    result.sort(key=lambda value: (str(value.get("create_time", "")), str(value.get("id", ""))))
    return result


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(str(part) for part in parts if part is not None)
        if content.get("text") is not None:
            return str(content["text"])
    return str(content) if content else ""


def _extract_conversations(root: Path) -> list[tuple[dict[str, Any], str]]:
    result: list[tuple[dict[str, Any], str]] = []
    for path in _json_candidates(root):
        parsed = _load_json(path)
        if isinstance(parsed, list):
            candidates = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("conversations"), list):
            candidates = parsed["conversations"]
        elif isinstance(parsed, dict) and any(
            key in parsed for key in ("mapping", "messages", "conversation_id")
        ):
            candidates = [parsed]
        else:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        for index, item in enumerate(candidates):
            if isinstance(item, dict):
                result.append((item, f"{relative}#{index}"))
    return result


def _db_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, source_kind TEXT NOT NULL,
          captured_at TEXT NOT NULL, source_path TEXT NOT NULL,
          manifest_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL,
          conversation_count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(account_id, source_kind, manifest_sha256)
        );
        CREATE TABLE IF NOT EXISTS files (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          rel_path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
          media_type TEXT, PRIMARY KEY(run_id, rel_path)
        );
        CREATE TABLE IF NOT EXISTS conversations (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          account_id TEXT NOT NULL, source_kind TEXT NOT NULL,
          conversation_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
          created_at TEXT, updated_at TEXT, message_count INTEGER NOT NULL DEFAULT 0,
          source_ref TEXT NOT NULL, raw_json TEXT NOT NULL, messages_json TEXT NOT NULL,
          PRIMARY KEY(run_id, conversation_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
          run_id UNINDEXED, conversation_id UNINDEXED, title, content, tokenize='unicode61'
        );
        CREATE INDEX IF NOT EXISTS idx_runs_account ON runs(account_id, captured_at);
        CREATE INDEX IF NOT EXISTS idx_conversations_id ON conversations(conversation_id);
        """
    )


def _copy_source(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination / source.name)
        return
    for path in _source_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def ingest(
    source_path: str | os.PathLike[str], *, account_id: str, source_kind: str,
    archive_root: str | os.PathLike[str], db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Copy one export into the raw archive and index it idempotently."""
    if source_kind not in SOURCE_KINDS:
        raise ArchiveError(f"source_kind must be one of: {', '.join(sorted(SOURCE_KINDS))}")
    source = Path(source_path).expanduser().resolve()
    root = Path(archive_root).expanduser().resolve()
    account = _safe_account_id(account_id)
    records = build_manifest(source)
    if not records:
        raise ArchiveError("source contains no files")
    manifest_sha = _manifest_hash(records)
    run_id = f"{account}-{source_kind}-{manifest_sha[:16]}"
    raw_run = root / "raw" / account / run_id
    db_file = Path(db_path).expanduser() if db_path else root / "index.sqlite3"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "chatgpt-archive.manifest.v1", "run_id": run_id,
        "account_id": account, "source_kind": source_kind, "captured_at": _utc_now(),
        "source_path": str(source), "manifest_sha256": manifest_sha,
        "files": [record.__dict__ for record in records],
    }
    manifest_path = raw_run / "manifest.json"
    if not raw_run.exists():
        raw_run.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            staged = Path(tmp) / "run"
            _copy_source(source, staged)
            copied_records = build_manifest(staged)
            if [record.__dict__ for record in copied_records] != [record.__dict__ for record in records]:
                raise ArchiveError("copied raw run failed manifest verification")
            (staged / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            staged.rename(raw_run)
    elif not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    with _archive_parse_root(raw_run) as parse_root:
        conversations = _extract_conversations(parse_root)
    with sqlite3.connect(db_file) as db:
        _db_schema(db)
        existing = db.execute(
            "SELECT run_id, conversation_count FROM runs "
            "WHERE account_id=? AND source_kind=? AND manifest_sha256=?",
            (account, source_kind, manifest_sha),
        ).fetchone()
        if existing:
            return {
                "run_id": existing[0], "account_id": account, "source_kind": source_kind,
                "raw_path": str(raw_run), "db_path": str(db_file),
                "file_count": len(records), "conversation_count": int(existing[1]),
                "idempotent": True,
            }
        db.execute(
            "INSERT INTO runs(run_id,account_id,source_kind,captured_at,source_path,"
            "manifest_sha256,manifest_json,conversation_count) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, account, source_kind, manifest["captured_at"], str(source),
             manifest_sha, json.dumps(manifest, ensure_ascii=False), len(conversations)),
        )
        db.executemany(
            "INSERT INTO files(run_id,rel_path,size,sha256,media_type) VALUES(?,?,?,?,?)",
            [(run_id, r.rel_path, r.size, r.sha256, r.media_type) for r in records],
        )
        rows: list[tuple[Any, ...]] = []
        fts_rows: list[tuple[str, str, str, str]] = []
        for item, source_ref in conversations:
            cid = _conversation_id(item, source_ref)
            messages = _messages(item)
            title = str(item.get("title") or "")
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            messages_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
            content = "\n".join(_message_text(message) for message in messages)
            rows.append((run_id, account, source_kind, cid, title, item.get("create_time"),
                         item.get("update_time"), len(messages), source_ref, raw, messages_json))
            fts_rows.append((run_id, cid, title, content))
        db.executemany(
            "INSERT OR REPLACE INTO conversations(run_id,account_id,source_kind,conversation_id,"
            "title,created_at,updated_at,message_count,source_ref,raw_json,messages_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        db.executemany(
            "INSERT INTO conversation_fts(run_id,conversation_id,title,content) VALUES(?,?,?,?)",
            fts_rows,
        )
    return {
        "run_id": run_id, "account_id": account, "source_kind": source_kind,
        "raw_path": str(raw_run), "db_path": str(db_file), "file_count": len(records),
        "conversation_count": len(conversations), "idempotent": False,
    }


def search(
    db_path: str | os.PathLike[str], query: str, *, account_id: str | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    terms = [term.replace('"', " ") for term in query.split() if term.strip()]
    expression = " AND ".join(f'"{term}"*' for term in terms)
    with sqlite3.connect(Path(db_path).expanduser()) as db:
        db.row_factory = sqlite3.Row
        params: list[Any] = [expression]
        account_sql = ""
        if account_id:
            account_sql = " AND c.account_id = ?"
            params.append(_safe_account_id(account_id))
        params.append(max(1, min(int(limit), 100)))
        rows = db.execute(
            "SELECT c.run_id,c.account_id,c.source_kind,c.conversation_id,c.title,c.created_at,"
            "c.updated_at,c.message_count FROM conversation_fts f "
            "JOIN conversations c ON c.run_id=f.run_id AND c.conversation_id=f.conversation_id "
            f"WHERE conversation_fts MATCH ?{account_sql} "
            "ORDER BY COALESCE(c.updated_at,c.created_at) DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _signature(item: dict[str, Any], ref: str) -> tuple[str, str, str, int] | None:
    cid = _conversation_id(item, ref)
    title = str(item.get("title") or "")
    messages = _messages(item)
    if not title and not messages and not item.get("create_time") and not item.get("update_time"):
        return None
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return cid, title, hashlib.sha256(payload.encode()).hexdigest(), len(messages)


def _signatures(root: Path) -> dict[str, tuple[str, str, str, int]]:
    result: dict[str, tuple[str, str, str, int]] = {}
    for item, ref in _extract_conversations(root):
        signature = _signature(item, ref)
        if signature:
            result.setdefault(signature[0], signature)
    return result


def validate(primary_path: str | os.PathLike[str], official_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Compare two exports without treating the official ZIP as the source of truth."""
    with _export_root(Path(primary_path).expanduser().resolve()) as primary_root:
        primary = _signatures(primary_root)
    with _export_root(Path(official_path).expanduser().resolve()) as official_root:
        official = _signatures(official_root)
    verified: list[str] = []
    mismatches: list[dict[str, Any]] = []
    for cid in sorted(set(primary) & set(official)):
        left, right = primary[cid], official[cid]
        if left[1:] == right[1:]:
            verified.append(cid)
        else:
            mismatches.append({
                "conversation_id": cid,
                "primary": {"title": left[1], "message_sha256": left[2], "message_count": left[3]},
                "official": {"title": right[1], "message_sha256": right[2], "message_count": right[3]},
            })
    primary_only = sorted(set(primary) - set(official))
    official_only = sorted(set(official) - set(primary))
    return {
        "schema": "chatgpt-archive.validation.v1", "generated_at": _utc_now(),
        "primary": {"path": str(Path(primary_path).expanduser().resolve()), "conversation_count": len(primary)},
        "official": {"path": str(Path(official_path).expanduser().resolve()), "conversation_count": len(official)},
        "verified": verified, "open_source_only": primary_only, "official_only": official_only,
        "content_mismatch": mismatches,
        "not_comparable": ["attachments and rich references are compared only when represented in both exports"],
        "summary": {"verified": len(verified), "open_source_only": len(primary_only),
                    "official_only": len(official_only), "content_mismatch": len(mismatches)},
    }


def list_runs(db_path: str | os.PathLike[str], *, account_id: str | None = None) -> list[dict[str, Any]]:
    with sqlite3.connect(Path(db_path).expanduser()) as db:
        db.row_factory = sqlite3.Row
        query = "SELECT * FROM runs"
        params: tuple[Any, ...] = ()
        if account_id:
            query += " WHERE account_id=?"
            params = (_safe_account_id(account_id),)
        rows = db.execute(query + " ORDER BY captured_at DESC", params).fetchall()
    return [dict(row) for row in rows]
