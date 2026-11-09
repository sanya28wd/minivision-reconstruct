"""Resolve producer manifest input before invoking workflows that trust its paths."""

import json
from pathlib import PurePosixPath
from threading import Lock

from minivision_reconstruct.domain.models import EvidenceManifest, FrameRate
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.video.reader import read_frame_at


class VerificationCache:
    """Hash a file once per path/size/mtime; compare every requested expected hash."""

    def __init__(self):
        self._lock = Lock()
        self._verified = {}

    def verify(self, path, expected):
        with self._lock:
            before = path.stat()
            key = (path, before.st_size, before.st_mtime_ns)
            digest = self._verified.get(key)
            if digest is None:
                digest = sha256_file(path)
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != key[1:]:
                    raise ValueError("Evidence changed during verification")
                # Retain only the current version of each file.
                self._verified = {k: v for k, v in self._verified.items() if k[0] != path}
                self._verified[key] = digest
            if digest != expected:
                raise ValueError("Evidence hash mismatch")


def confined_file(base, relative):
    """Check only the requested path and its ancestors, never scan the whole case."""
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Invalid evidence path")
    result = base / relative
    current = result
    while current != base:
        if current.is_symlink():
            raise ValueError("Linked evidence path")
        current = current.parent
    if not result.is_file() or not result.resolve(strict=True).is_relative_to(base.resolve(strict=True)):
        raise ValueError("Invalid evidence path")
    return result


def load_evidence(root, case_id, select_case, verification=None):
    case = select_case(root, case_id)
    manifest = EvidenceManifest(**json.loads((case / "metadata/evidence_manifest.json").read_bytes()))
    declared = manifest.preserved_relative_path
    path = PurePosixPath(declared)
    if (
        manifest.case_id != case_id
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in declared
        or not path.parts
        or path.parts[0] != case_id
    ):
        raise ValueError("Invalid preserved input")
    video = confined_file(root, declared)
    if not video.resolve(strict=True).is_relative_to(case):
        raise ValueError("Invalid preserved input")
    if verification is None:
        if sha256_file(video) != manifest.preserved_sha256:
            raise ValueError("Invalid preserved input")
    else:
        verification.verify(video, manifest.preserved_sha256)
    return case, manifest, video


def source_frame(manifest, video, frame_number):
    rate = (
        None
        if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None
        else FrameRate(manifest.frame_rate_numerator, manifest.frame_rate_denominator)
    )
    return read_frame_at(video, rate, frame_number)
