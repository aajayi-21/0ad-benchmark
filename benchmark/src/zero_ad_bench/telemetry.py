"""Incremental JSONL artifacts with an atomic, checksummed manifest."""

import hashlib
import json
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from zero_ad_bench import ARTIFACT_SCHEMA_VERSION, PACKAGE_VERSION


STREAMS = (
    "decisions",
    "model-calls",
    "observations",
    "actions",
    "events",
    "snapshots",
    "hashes",
)
TERMINAL_STATUSES = ("completed", "invalid", "failed", "interrupted")


def utc_now():
    return datetime.now(UTC).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, value):
    """Write JSON through a temporary file so readers never see a partial manifest."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_jsonl(path):
    """Return `(records, truncated)`. A partial trailing line marks an interrupted writer."""
    path = Path(path)
    if not path.is_file():
        return [], False
    records = []
    truncated = False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                truncated = True
                break
            records.append(json.loads(line))
    return records, truncated


def checksums(directory, exclude=("manifest.json",)):
    directory = Path(directory)
    result = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative in exclude or relative.endswith(".tmp"):
            continue
        result[relative] = sha256_file(path)
    return result


class EpisodeArtifacts:
    """Exclusive episode directory; every record is flushed as soon as it is appended."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.handles = {}
        self.counts = dict.fromkeys(STREAMS, 0)
        self.manifest = None
        # Controllers of different seats decide concurrently and record through the gateway.
        self.lock = threading.Lock()

    def start(self, manifest):
        self.manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runner_version": PACKAGE_VERSION,
            "status": "running",
            "started_utc": utc_now(),
            "finished_utc": None,
            "files": {},
            **manifest,
        }
        write_json_atomic(self.directory / "manifest.json", self.manifest)
        for stream in STREAMS:
            self.handles[stream] = (self.directory / f"{stream}.jsonl").open("a", encoding="utf-8")

    def append(self, stream, record):
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.lock:
            handle = self.handles[stream]
            handle.write(line)
            handle.flush()
            self.counts[stream] += 1

    def write_json(self, name, value):
        write_json_atomic(self.directory / name, value)

    def write_text(self, name, text):
        path = self.directory / name
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def copy_file(self, source, relative):
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def finish(self, status, **fields):
        """Close streams and publish the final manifest atomically. Idempotent per status."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Unknown terminal status {status}")
        for handle in self.handles.values():
            handle.close()
        self.handles = {}
        self.manifest.update(fields)
        self.manifest["status"] = status
        self.manifest["finished_utc"] = utc_now()
        self.manifest["record_counts"] = dict(self.counts)
        self.manifest["files"] = checksums(self.directory)
        write_json_atomic(self.directory / "manifest.json", self.manifest)
        return self.manifest
