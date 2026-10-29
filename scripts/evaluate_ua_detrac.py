"""Run the constrained Week 2 UA-DETRAC-derived image evaluation."""

import argparse
from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.ua_detrac_evaluation import evaluate_subset


def main(arguments: list[str]) -> int:
    """Execute one explicit aggregate vehicle evaluation."""
    parser = argparse.ArgumentParser(description="Evaluate YOLO against the declared UA-DETRAC-derived image subset.")
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--iou-threshold", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    try:
        result = evaluate_subset(parsed.images, parsed.labels, load_detector_configuration(parsed.config), parsed.iou_threshold, parsed.output)
    except EvidenceError as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"Aggregate vehicle evaluation: TP={result.true_positives}, FP={result.false_positives}, FN={result.false_negatives}, precision={result.precision:.4f}, recall={result.recall:.4f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
