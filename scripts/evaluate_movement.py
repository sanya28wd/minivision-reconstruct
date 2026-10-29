"""Report movement, stop, and maneuver timing error against annotated staged-motion ground truth."""

import argparse
from pathlib import Path

from minivision_reconstruct.movement.validation import evaluate_movement


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-ids", nargs="+", required=True)
    parser.add_argument("--movement-run", required=True)
    parser.add_argument("--evaluation-root", type=Path, required=True)
    parser.add_argument("--evaluation-id", required=True)
    args = parser.parse_args()
    print(evaluate_movement(args.cases_root.resolve(), tuple(args.case_ids), args.movement_run, args.evaluation_root.resolve(), args.evaluation_id))


if __name__ == "__main__":
    main()
