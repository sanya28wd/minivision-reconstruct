"""The evidence storage seam (RPT-03): write-once, SHA-256 keyed, verified while streaming.

Every test goes through the EvidenceStore interface (put / get / exists) of the local backend.
The expected digest of b"abc" is the published SHA-256 test vector (FIPS 180-2), not computed here.
"""

from __future__ import annotations

import hashlib
import io
import stat
from pathlib import Path
from typing import BinaryIO, cast

import pytest

import incident_report
from incident_report.storage import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceStore,
    HashMismatch,
    InvalidEvidenceKey,
    LocalEvidenceStore,
    StorageConfigError,
)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
PACKAGE_DIR = Path(incident_report.__file__).resolve().parent


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"


@pytest.fixture
def store(root: Path) -> EvidenceStore:
    return LocalEvidenceStore(root)


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


class LazyStream(io.RawIOBase):
    """A stream that makes `total` bytes on demand and refuses to be read all at once."""

    def __init__(self, total: int, chunk: bytes = b"0123456789abcdef" * 1024) -> None:
        self._left = total
        self._chunk = chunk
        self.largest_read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("the store tried to read the whole stream into memory")
        self.largest_read = max(self.largest_read, size)
        n = min(size, self._left, len(self._chunk))
        self._left -= n
        return self._chunk[:n]


def test_a_stored_object_is_read_back_by_its_sha256(store: EvidenceStore) -> None:
    result = store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    assert (result.sha256, result.size, result.created) == (ABC_SHA256, 3, True)
    assert store.exists(ABC_SHA256)
    with store.get(ABC_SHA256) as stream:
        assert stream.read() == b"abc"


def test_get_returns_a_stream_that_can_be_read_in_pieces(store: EvidenceStore) -> None:
    content = b"x" * 5000
    key = hashlib.sha256(content).hexdigest()
    store.put(io.BytesIO(content), sha256=key)

    with store.get(key) as stream:
        assert stream.read(1000) == content[:1000]
        assert stream.read(1000) == content[1000:2000]


def test_content_that_does_not_match_the_key_is_refused_and_leaves_nothing_behind(
    store: EvidenceStore, root: Path
) -> None:
    with pytest.raises(HashMismatch):
        store.put(io.BytesIO(b"not abc"), sha256=ABC_SHA256)

    assert not store.exists(ABC_SHA256)
    assert _files_under(root) == []  # no partial object and no staging leftovers


def test_a_failing_stream_leaves_nothing_behind(store: EvidenceStore, root: Path) -> None:
    class Broken(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int | None = -1) -> bytes:
            raise OSError("connection lost")

    with pytest.raises(OSError, match="connection lost"):
        store.put(cast(BinaryIO, Broken()), sha256=ABC_SHA256)

    assert _files_under(root) == []


def test_putting_the_same_content_again_is_idempotent_and_changes_nothing(store: EvidenceStore, root: Path) -> None:
    first = store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)
    (path,) = _files_under(root)
    before = path.stat()

    again = store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    assert (again.sha256, again.size, again.created) == (first.sha256, first.size, False)
    assert _files_under(root) == [path]
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == (before.st_mtime_ns, before.st_ino)
    assert path.read_bytes() == b"abc"


def test_wrong_content_for_an_existing_key_is_refused_and_the_original_stays(store: EvidenceStore) -> None:
    store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    with pytest.raises(HashMismatch):
        store.put(io.BytesIO(b"something else"), sha256=ABC_SHA256)

    with store.get(ABC_SHA256) as stream:
        assert stream.read() == b"abc"


def test_an_existing_object_that_does_not_fit_its_key_is_never_overwritten(store: EvidenceStore, root: Path) -> None:
    store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)
    (path,) = _files_under(root)
    path.chmod(0o644)
    path.write_bytes(b"damaged on disk")  # simulate corruption of the stored object

    with pytest.raises(EvidenceConflict):
        store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    assert path.read_bytes() == b"damaged on disk"  # reported, never silently replaced


def test_a_same_size_object_with_different_bytes_under_the_key_is_a_conflict_and_left_untouched(
    store: EvidenceStore, root: Path
) -> None:
    store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)
    (path,) = _files_under(root)
    path.chmod(0o644)
    path.write_bytes(b"xyz")  # same length as b"abc", different bytes: damage or tampering
    before = path.stat()

    with pytest.raises(EvidenceConflict):
        store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    assert path.read_bytes() == b"xyz"
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == (before.st_mtime_ns, before.st_ino)
    assert _files_under(root) == [path]  # nothing left in staging


def test_stored_objects_are_read_only_on_disk(store: EvidenceStore, root: Path) -> None:
    store.put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    (path,) = _files_under(root)
    assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0


def test_a_large_stream_is_hashed_and_stored_in_pieces_never_whole(store: EvidenceStore, root: Path) -> None:
    total = 24 * 1024 * 1024
    reference = hashlib.sha256()
    probe = LazyStream(total)
    while piece := probe.read(1 << 20):
        reference.update(piece)

    stream = LazyStream(total)
    result = store.put(cast(BinaryIO, stream), sha256=reference.hexdigest())

    assert result.size == total and result.created
    assert 0 < stream.largest_read < total  # bounded reads (the stream raises on read-all)
    assert _files_under(root)[0].stat().st_size == total


def test_a_key_that_is_not_a_lowercase_sha256_is_refused_before_any_path_is_built(
    store: EvidenceStore, root: Path
) -> None:
    for bad in ["", "../../etc/passwd", "ABC" + ABC_SHA256[3:], ABC_SHA256[:-1], ABC_SHA256 + "0", "g" * 64]:
        with pytest.raises(InvalidEvidenceKey):
            store.put(io.BytesIO(b"abc"), sha256=bad)
        with pytest.raises(InvalidEvidenceKey):
            store.get(bad)
        with pytest.raises(InvalidEvidenceKey):
            store.exists(bad)
    assert _files_under(root) == []


def test_getting_an_unknown_key_says_so(store: EvidenceStore) -> None:
    assert not store.exists(ABC_SHA256)
    with pytest.raises(EvidenceNotFound):
        store.get(ABC_SHA256)


def test_objects_survive_a_new_store_on_the_same_root(root: Path) -> None:
    LocalEvidenceStore(root).put(io.BytesIO(b"abc"), sha256=ABC_SHA256)

    with LocalEvidenceStore(root).get(ABC_SHA256) as stream:
        assert stream.read() == b"abc"


def test_the_local_backend_refuses_a_root_inside_the_package_directory() -> None:
    with pytest.raises(StorageConfigError, match="package"):
        LocalEvidenceStore(PACKAGE_DIR / "evidence")
    with pytest.raises(StorageConfigError, match="package"):
        LocalEvidenceStore(PACKAGE_DIR)


def test_the_local_backend_refuses_a_relative_root() -> None:
    with pytest.raises(StorageConfigError, match="absolute"):
        LocalEvidenceStore(Path("evidence"))
