from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from chatgpt_archive import ArchiveError, default_archive_root, ingest, search, validate
from chatgpt_archive.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def _write_export(
    root: Path, conversation_id: str = "conv-1", text: str = "hello archive"
) -> Path:
    root.mkdir()
    payload = [
        {
            "conversation_id": conversation_id,
            "title": "Archive test",
            "create_time": 1,
            "update_time": 2,
            "mapping": {
                "node": {
                    "message": {
                        "id": "msg-1",
                        "author": {"role": "user"},
                        "content": {"parts": [text]},
                        "create_time": 1,
                    }
                }
            },
        }
    ]
    (root / "conversations.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "asset.txt").write_text("attachment", encoding="utf-8")
    return root


def test_ingest_preserves_raw_manifest_and_is_idempotent(tmp_path: Path) -> None:
    source = _write_export(tmp_path / "source")
    archive = tmp_path / "archive"

    first = ingest(
        source, account_id="account one", source_kind="open_source", archive_root=archive
    )
    second = ingest(
        source, account_id="account one", source_kind="open_source", archive_root=archive
    )

    assert first["conversation_count"] == 1
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["run_id"] == first["run_id"]
    raw = Path(first["raw_path"])
    assert (raw / "manifest.json").exists()
    assert (raw / "conversations.json").exists()
    assert (raw / "asset.txt").read_text() == "attachment"
    assert len(search(first["db_path"], "hello", account_id="account one")) == 1


def test_ingest_preserves_database_and_code_files(tmp_path: Path) -> None:
    source = _write_export(tmp_path / "source")
    sqlite_bytes = b"SQLite format 3\x00\x00embedded fixture"
    (source / "embedded.sqlite").write_bytes(sqlite_bytes)
    (source / "helper.py").write_text("print('kept')\n", encoding="utf-8")

    result = ingest(source, account_id="one", source_kind="open_source", archive_root=tmp_path / "archive")
    raw = Path(result["raw_path"])

    assert (raw / "embedded.sqlite").read_bytes() == sqlite_bytes
    assert (raw / "helper.py").read_text(encoding="utf-8") == "print('kept')\n"


def test_ingest_rejects_vault_inside_source(tmp_path: Path) -> None:
    source = _write_export(tmp_path / "source")
    with pytest.raises(ArchiveError, match="archive_root must be outside the source directory"):
        ingest(
            source,
            account_id="one",
            source_kind="open_source",
            archive_root=source / "vault",
        )


def test_search_uses_local_fts_and_account_filter(tmp_path: Path) -> None:
    source = _write_export(tmp_path / "source")
    archive = tmp_path / "archive"
    result = ingest(source, account_id="one", source_kind="open_source", archive_root=archive)

    rows = search(result["db_path"], "hello", account_id="one")
    assert [row["conversation_id"] for row in rows] == ["conv-1"]
    assert search(result["db_path"], "hello", account_id="missing") == []


def test_validation_classifies_primary_only_and_mismatch(tmp_path: Path) -> None:
    primary = _write_export(tmp_path / "primary")
    official = _write_export(tmp_path / "official", conversation_id="conv-2", text="different")
    report = validate(primary, official)

    assert report["summary"]["open_source_only"] == 1
    assert report["summary"]["official_only"] == 1
    assert report["summary"]["content_mismatch"] == 0
    assert report["summary"]["verified"] == 0
    assert report["not_comparable"]


def test_validation_verified_and_content_mismatch(tmp_path: Path) -> None:
    verified_primary = _write_export(tmp_path / "verified-primary", text="same body")
    verified_official = _write_export(tmp_path / "verified-official", text="same body")
    verified = validate(verified_primary, verified_official)
    assert verified["summary"]["verified"] == 1
    assert verified["verified"] == ["conv-1"]

    primary = _write_export(tmp_path / "primary", conversation_id="conv-9", text="alpha")
    official = _write_export(tmp_path / "official", conversation_id="conv-9", text="beta")
    report = validate(primary, official)
    assert report["summary"]["content_mismatch"] == 1
    assert report["content_mismatch"][0]["conversation_id"] == "conv-9"
    assert report["summary"]["verified"] == 0


@pytest.mark.parametrize("kind", ["open_source", "official", "manual"])
def test_supported_source_kinds(tmp_path: Path, kind: str) -> None:
    source = _write_export(tmp_path / f"source-{kind}")
    result = ingest(source, account_id="a", source_kind=kind, archive_root=tmp_path / "archive")
    assert result["source_kind"] == kind


def test_official_zip_indexes_conversations_and_rejects_traversal(tmp_path: Path) -> None:
    export = _write_export(tmp_path / "export")
    zip_path = tmp_path / "official.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in export.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(export).as_posix())

    result = ingest(
        zip_path,
        account_id="zip-user",
        source_kind="official",
        archive_root=tmp_path / "archive",
    )
    assert result["conversation_count"] == 1
    assert (Path(result["raw_path"]) / "official.zip").is_file()
    assert search(result["db_path"], "hello", account_id="zip-user")[0]["conversation_id"] == "conv-1"

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.txt", "nope")
    with pytest.raises(ArchiveError, match="unsafe path"):
        validate(zip_path, bad)

    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip archive")
    with pytest.raises(ArchiveError, match="invalid zip export"):
        validate(invalid, zip_path)


def test_default_archive_root_is_not_under_octop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHATGPT_ARCHIVE_ROOT", raising=False)
    root = default_archive_root()
    assert root.name == ".chatgpt-local-backup"
    assert ".octop" not in root.parts

    monkeypatch.setenv("CHATGPT_ARCHIVE_ROOT", str(Path.home() / "custom-vault"))
    assert default_archive_root() == Path.home() / "custom-vault"
    assert ".octop" not in default_archive_root().parts


def test_cli_help_does_not_advertise_octop_paths() -> None:
    with pytest.raises(SystemExit) as exc:
        with redirect_stdout(io.StringIO()) as buf:
            build_parser().parse_args(["ingest", "--help"])
    assert exc.value.code == 0
    text = buf.getvalue()
    assert "~/.chatgpt-local-backup" in text
    assert ".octop" not in text


def test_cli_ingest_search_runs_and_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _write_export(tmp_path / "source", text="hello archive 关键词")
    archive = tmp_path / "vault"
    official = _write_export(tmp_path / "official", text="hello archive 关键词")

    assert main(
        ["ingest", str(source), "--account", "one", "--source-kind", "open_source", "--archive-root", str(archive)]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["idempotent"] is False
    assert ".octop" not in first["raw_path"]

    assert main(["ingest", str(source), "--account", "one", "--archive-root", str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)["idempotent"] is True

    assert main(["search", "hello", "--archive-root", str(archive), "--account", "one"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["conversation_id"] == "conv-1"

    assert main(["search", "关键词", "--archive-root", str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["conversation_id"] == "conv-1"

    assert main(["runs", "--archive-root", str(archive)]) == 0
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 1

    report_path = tmp_path / "validation.json"
    assert main(["validate", str(source), str(official), "--report", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["verified"] == 1


def test_optional_adapter_is_marked_optional_and_has_no_octop_default(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter_path = ROOT / "optional" / "octop-adapter" / "main.py"
    adapter_text = adapter_path.read_text(encoding="utf-8")
    readme = (ROOT / "optional" / "octop-adapter" / "README.md").read_text(encoding="utf-8")
    plugin = (ROOT / "optional" / "octop-adapter" / "plugin.yaml").read_text(encoding="utf-8")
    assert "Not required" in adapter_text
    assert ".octop" not in adapter_text
    assert "不是归档本身" in readme
    assert "Not required" in plugin
    assert ".octop" not in plugin

    monkeypatch.delenv("CHATGPT_ARCHIVE_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("chatgpt_archive_octop_adapter", adapter_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.default_archive_root().name == ".chatgpt-local-backup"
    assert ".octop" not in module.default_archive_root().parts
