"""Byte-preserving evidence hashing helpers."""

from hashlib import sha256
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file, streamed without loading it fully."""
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()
