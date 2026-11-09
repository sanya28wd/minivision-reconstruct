"""Handmade graph variations on the real producer JSON fixture, for boundary tests."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[3] / "contracts/evidence-v1/fixtures"


def synthetic_bundle(
    root: Path,
    *,
    rejected: bool = False,
    modified: bool = False,
    transform: Callable[[dict[str, list[dict[str, Any]]], dict[str, Any]], None] | None = None,
) -> Path:
    real = FIXTURES / "mig02-001-07-occluded-impact"
    shutil.copytree(real, root, dirs_exist_ok=True)
    envelope = json.loads((root / "bundle.json").read_bytes())
    documents: dict[str, list[dict[str, Any]]] = {}
    old_hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.name != "bundle.json":
            name = path.relative_to(root).as_posix()
            content = path.read_bytes()
            old_hashes[name] = hashlib.sha256(content).hexdigest()
            documents[name] = (
                [json.loads(line) for line in content.splitlines()]
                if name.endswith(".jsonl")
                else [json.loads(content)]
            )
    # A handmade complete graph: remove absent presentation references and overlay review at an exact source time.
    # Producer bytes in the real fixture are NEVER changed; these are explicit test-only variations.
    for name, rows in documents.items():
        for row in rows:
            if "artifacts" in row:
                row["artifacts"] = [
                    a
                    for a in row["artifacts"]
                    if str(Path(name).parent / a["filename"]).replace("\\", "/") in documents
                ]
            if "document" in row and "decisions" in row["document"]:
                for decision in row["document"]["decisions"]:
                    decision["action"] = "reject" if rejected else "modify" if modified else "confirm"
                    decision["modified_peak_time"] = None
            if "proposal" in row and "peak_time" in row:
                row["status"] = (
                    "REJECTED_BY_REVIEWER"
                    if rejected
                    else "MODIFIED_BY_REVIEWER"
                    if modified
                    else "CONFIRMED_BY_REVIEWER"
                )
                row["peak_time"] = row["proposal"]["peak_time"] + (0.001 if modified else 0)
                row["decision"]["action"] = "reject" if rejected else "modify" if modified else "confirm"
                row["decision"]["modified_peak_time"] = row["peak_time"] if modified else None
            if "event_status" in row:
                row["event_status"] = (
                    "REJECTED_BY_REVIEWER"
                    if rejected
                    else "MODIFIED_BY_REVIEWER"
                    if modified
                    else "CONFIRMED_BY_REVIEWER"
                )
    if modified:
        for name, rows in documents.items():
            if "reviews/revision-003/revision.json" in name:
                event_path = next(n for n in documents if "reviewed/revision-003/reviewed_events.jsonl" in n)
                rows[0]["document"]["decisions"][0] = documents[event_path][0]["decision"]
    if transform is not None:
        transform(documents, envelope)
    # Recompute every reference to changed content, from leaves up the acyclic manifest/revision graph.
    hashes = dict(old_hashes)
    for _ in range(20):
        replacements = {old_hashes[n]: hashes[n] for n in hashes}

        def replace(value: Any, replacements: dict[str, str] = replacements) -> Any:
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            return replacements.get(value, value) if isinstance(value, str) else value

        next_hashes = {}
        for name, original in documents.items():
            rows = replace(original)
            content = (
                "".join(json.dumps(row) + "\n" for row in rows) if name.endswith(".jsonl") else json.dumps(rows[0])
            ).encode()
            (root / name).write_bytes(content)
            next_hashes[name] = hashlib.sha256(content).hexdigest()
        if next_hashes == hashes:
            break
        hashes = next_hashes
    envelope["artifacts"] = hashes
    (root / "bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    return root


def two_view_bundle(root: Path) -> Path:
    """One application case, two byte-unchanged producer scenes with track IDs 1/2."""
    root.mkdir(parents=True, exist_ok=True)
    envelopes = []
    for name, view in [("mig02-001-01-rear-end", "first-view"), ("mig02-001-07-occluded-impact", "second-view")]:
        producer = FIXTURES / name
        envelope = json.loads((producer / "bundle.json").read_bytes())
        for artifact in envelope["artifacts"]:
            destination = root / artifact
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(producer / artifact, destination)
        binding = envelope["sources"][0]
        binding["view_id"] = view
        binding["producer_case_id"] = envelope["case_id"]
        envelopes.append(envelope)
    shared = envelopes[0]
    shared["case_id"] = "shared-case"
    shared["sources"].extend(envelopes[1]["sources"])
    shared["artifacts"].update(envelopes[1]["artifacts"])
    (root / "bundle.json").write_text(json.dumps(shared), encoding="utf-8")
    return root
