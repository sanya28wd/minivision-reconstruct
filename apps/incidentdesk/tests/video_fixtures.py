"""Valid synthetic MP4s, with distinct inert free boxes for replay keys."""

import struct

from incident_report.samples import SAMPLE_VIEW_BYTES


def synthetic_video(marker: bytes) -> bytes:
    return SAMPLE_VIEW_BYTES + struct.pack(">I4s", len(marker) + 8, b"free") + marker
