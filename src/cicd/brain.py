from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any

from src.cicd.runner import CommandRunner


class BrainDeploymentArchiver:
    def __init__(
        self,
        *,
        manifest_path: str | Path = "reports/deployments/brain_archive_manifest.json",
        ingest_command: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.ingest_command = ingest_command if ingest_command is not None else os.environ.get("POLYLENS_BRAIN_INGEST_COMMAND", "")
        self.runner = runner or CommandRunner()

    def archive(self, report_path: str | Path, report: dict[str, Any] | None = None) -> dict[str, Any]:
        path = Path(report_path)
        digest = _sha256(path)
        manifest = self._load_manifest()
        if digest in manifest.get("digests", {}):
            return {"archived": False, "duplicate": True, "digest": digest, "record": manifest["digests"][digest]}
        record = {
            "path": str(path),
            "digest": digest,
            "commit": (report or {}).get("commit"),
            "status": (report or {}).get("status"),
        }
        ingest_result: dict[str, Any] | None = None
        if self.ingest_command:
            command = self._command(path)
            result = self.runner.run(command, timeout=300)
            ingest_result = result.to_dict()
            record["ingest_returncode"] = result.returncode
        manifest.setdefault("digests", {})[digest] = record
        self._save_manifest(manifest)
        return {"archived": True, "duplicate": False, "digest": digest, "record": record, "ingest": ingest_result}

    def _command(self, path: Path) -> list[str]:
        if "{path}" in self.ingest_command:
            return shlex.split(self.ingest_command.format(path=str(path)))
        return [*shlex.split(self.ingest_command), str(path)]

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"digests": {}}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"digests": {}}

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
