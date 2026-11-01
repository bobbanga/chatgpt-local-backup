"""Account-independent local vault for ChatGPT exports."""

from .archive import (
    ArchiveError,
    default_archive_root,
    ingest,
    list_runs,
    search,
    validate,
)

__all__ = [
    "ArchiveError",
    "default_archive_root",
    "ingest",
    "list_runs",
    "search",
    "validate",
]

__version__ = "0.1.0"
