"""Producer revision progression without trusting envelope creation time."""

import json
from datetime import datetime

from incident_report.evidence_import.models import ImportDecision, SourceBinding


def _selected(decision: ImportDecision, source: SourceBinding) -> tuple[str, str, set[str], datetime | None]:
    manifest = json.loads(decision.raw_artifacts[source.reviewed_manifest])
    root = manifest["source_incident_manifest_sha256"]
    head = manifest.get("review_revision_sha256") or root
    by_hash = {digest: path for path, digest in decision.source_hashes.items()}
    chain = set()
    cursor = head
    while cursor != root:
        if cursor in chain:
            raise ValueError("Cyclic review chain")
        chain.add(cursor)
        document = json.loads(decision.raw_artifacts[by_hash[cursor]])
        cursor = document["previous_sha256"]
    chain.add(root)
    producer = json.loads(decision.raw_artifacts[source.incident_manifest])
    stamp = producer.get("created_at")
    created = datetime.fromisoformat(stamp) if stamp else None
    if created is not None and created.tzinfo is None:
        created = None
    return root, head, chain, created


def is_successor(old: ImportDecision, new: ImportDecision, view: str) -> bool:
    if old.envelope is None or new.envelope is None:
        return False
    previous = next((s for s in old.envelope.sources if s.original_video_sha256 == view), None)
    incoming = next((s for s in new.envelope.sources if s.original_video_sha256 == view), None)
    if previous is None or incoming is None:
        return False
    try:
        old_root, old_head, old_chain, old_time = _selected(old, previous)
        new_root, new_head, new_chain, new_time = _selected(new, incoming)
        if old_root == new_root:
            return old_head == new_head or old_head in new_chain
        return old_time is not None and new_time is not None and new_time > old_time
    except (KeyError, ValueError, TypeError):
        return False
