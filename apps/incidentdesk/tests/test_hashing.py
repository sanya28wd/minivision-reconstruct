"""Fingerprints: known-answer tests with literals computed independently of the code."""

from __future__ import annotations

import io

from incident_report.domain import Role
from incident_report.hashing import hash_stream, incident_input_hash, sha256_hex


def test_sha256_of_a_known_string() -> None:
    # FIPS 180-4 / NIST example vector for "abc".
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_stream_hash_matches_the_whole_file_hash_across_chunk_boundaries() -> None:
    # 2.5 MiB is larger than two read chunks, so the loop runs more than once.
    data = bytes(range(256)) * (10 * 1024)
    digest, size = hash_stream(io.BytesIO(data))
    assert digest == sha256_hex(data)
    assert size == len(data)


def test_input_hash_pins_the_documented_canonical_form() -> None:
    # sha256 of the JSON text [["exterior","<64 a>"]] with no spaces, computed with coreutils:
    #   printf '%s' '[["exterior","aaaa...a"]]' | sha256sum
    assert incident_input_hash([(Role.EXTERIOR, "a" * 64)]) == (
        "e585b90806845f32c64a00d175699c6c71e32208d5a71878a5db78f711b5212a"
    )


def test_input_hash_ignores_order_but_not_role_or_content() -> None:
    a = (Role.EXTERIOR, "a" * 64)
    b = (Role.INTERIOR, "b" * 64)
    assert incident_input_hash([a, b]) == incident_input_hash([b, a])
    assert incident_input_hash([a]) != incident_input_hash([(Role.INTERIOR, "a" * 64)])
    assert incident_input_hash([a]) != incident_input_hash([(Role.EXTERIOR, "c" * 64)])
