"""Report incident detection, impact timing and location, PET, and contribution agreement against scripted incident ground truth."""

import argparse
from pathlib import Path

from minivision_reconstruct.incident.validation import evaluate_incident


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-ids", nargs="+", required=True)
    parser.add_argument("--incident-run", required=True)
    parser.add_argument("--evaluation-root", type=Path, required=True)
    parser.add_argument("--evaluation-id", required=True)
    args = parser.parse_args()
    print(evaluate_incident(args.cases_root.resolve(), tuple(args.case_ids), args.incident_run, args.evaluation_root.resolve(), args.evaluation_id))


if __name__ == "__main__":
    main()
