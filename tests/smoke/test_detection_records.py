"""Pure smoke coverage for Week 2 detection-record validation."""

from pathlib import Path

from minivision_reconstruct.domain.models import DetectorProvenance, RawDetection
from minivision_reconstruct.perception.workflow import _validated_records


def test_filters_unsupported_classes_and_clips_supported_boxes() -> None:
    provenance = DetectorProvenance(
        model_family="test",
        model_name="test-model",
        model_version="test-version",
        library_version="test-library",
        weight_source="test-source",
        weight_sha256="a" * 64,
        device="cpu",
        model_license="test-license",
        model_size_bytes=1,
    )
    records = _validated_records(
        (
            RawDetection("car", 0.8, -2.0, 5.0, 120.0, 80.0),
            RawDetection("traffic light", 0.9, 1.0, 1.0, 2.0, 2.0),
        ),
        "case-1",
        4,
        0.8,
        100,
        60,
        provenance,
        0.012,
    )
    assert len(records) == 1
    record = records[0]
    assert record.class_name == "car"
    assert record.class_id == 2
    assert (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2) == (0.0, 5.0, 100.0, 60.0)
    assert record.frame_number == 4
    assert record.timestamp_seconds == 0.8
