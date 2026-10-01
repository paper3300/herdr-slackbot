"""Full result texts kept on disk for the [View full] button (STATE_DIR/results/<id>.md)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .state import atomic_write_text

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ResultStore:
    def __init__(self, directory: Path, keep: int = 200):
        self.dir = Path(directory)
        self.keep = keep

    def save(self, text: str, meta: dict) -> str:
        result_id = uuid.uuid4().hex
        atomic_write_text(self.dir / f"{result_id}.md", text)
        atomic_write_text(self.dir / f"{result_id}.json", json.dumps(meta, ensure_ascii=False))
        self.prune()
        return result_id

    def load(self, result_id: str) -> tuple[str, dict] | None:
        if not _ID_RE.match(result_id or ""):
            return None
        try:
            text = (self.dir / f"{result_id}.md").read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            meta = json.loads((self.dir / f"{result_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return text, meta

    def prune(self) -> None:
        try:
            files = sorted(self.dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self.keep:]:
            for path in (old, old.with_suffix(".json")):
                try:
                    path.unlink()
                except OSError:
                    pass
