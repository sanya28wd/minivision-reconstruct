"""The demo input for the walking skeleton.

SAMPLE_VIEW_BYTES is a deterministic one-frame black synthetic MP4. It is not incident footage.
The shipped synthetic recording is keyed to these exact bytes as an Exterior view.
Generated with ffmpeg 8.1: -f lavfi -i color=c=black:s=32x32:r=1 -frames:v 1 -c:v mpeg4
-fflags +bitexact -flags:v +bitexact -movflags frag_keyframe+empty_moov -f mp4 pipe:1.
The tiny encoded fixture is embedded so demo creation needs no model or ffmpeg process.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

SAMPLE_VIEW_BYTES = base64.b64decode(
    # wrapped so no line resembles a credential pattern (this is a video, not a secret)
    "AAAAIGZ0eXBpc29tAAACAGlzb21pc282aXNvMm1wNDEAAAL3bW9vdgAAAGxtdmhkAAAA"
    "AAAAAAAAAAAAAAAD6AAAAAAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAB"
    "AAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAh50"
    "cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAACAAAAAgAAAAAAG6bWRp"
    "YQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAAAABVxAAAAAAALWhkbHIAAAAAAAAAAHZp"
    "ZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABZW1pbmYAAAAUdm1oZAAAAAEAAAAA"
    "AAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASVzdGJsAAAA"
    "2XN0c2QAAAAAAAAAAQAAAMltcDR2AAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAACAAIABI"
    "AAAASAAAAAAAAAABCkxhdmMgbXBlZzQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAA"
    "T2VzZHMAAAAAA4CAgD4AAQAEgICAMCARAAAAAAMNQAADDUAFgICAHgAAAbABAAABtYkT"
    "AAABAAAAASAAxI2IAA0BBAQUQwaAgIABAgAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAA"
    "AAAAAw1AAAMNQAAAABBzdHRzAAAAAAAAAAAAAAAQc3RzYwAAAAAAAAAAAAAAFHN0c3oA"
    "AAAAAAAAAAAAAAAAAAAQc3RjbwAAAAAAAAAAAAAAKG12ZXgAAAAgdHJleAAAAAAAAAAB"
    "AAAAAQAAAAAAAAAAAAAAAAAAAD11ZHRhAAAANW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAA"
    "AG1kaXJhcHBsAAAAAAAAAAAAAAAACGlsc3QAAABwbW9vZgAAABBtZmhkAAAAAAAAAAEA"
    "AABYdHJhZgAAACR0ZmhkAAAAOQAAAAEAAAAAAAADFwAAQAAAAAAeAQEAAAAAABR0ZmR0"
    "AQAAAAAAAAAAAAAAAAAAGHRydW4AAAAFAAAAAQAAAHgCAAAAAAAAJm1kYXQAAAGzABAH"
    "AAABthYFGCPbfxtt+wAAwkYI9t/G234AAABDbWZyYQAAACt0ZnJhAQAAAAAAAAEAAAAA"
    "AAAAAQAAAAAAAAAAAAAAAAAAAxcBAQEAAAAQbWZybwAAAAAAAABD"
)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m incident_report.samples <output-file>", file=sys.stderr)
        return 2
    Path(argv[0]).write_bytes(SAMPLE_VIEW_BYTES)
    print(f"wrote {argv[0]} ({len(SAMPLE_VIEW_BYTES)} bytes); upload it as an Exterior view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
