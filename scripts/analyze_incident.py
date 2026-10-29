"""Derive Week 8 PET conflicts and collision, near-miss, and single-vehicle candidates from a Week 7 movement run."""

import argparse
from pathlib import Path

from minivision_reconstruct.incident.pipeline import analyze_incident


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--movement-run", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(analyze_incident(args.cases_root.resolve(), args.case_id, args.movement_run, args.config.resolve(), args.run_id))


if __name__ == "__main__":
    main()
