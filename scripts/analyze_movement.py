"""Derive Week 7 movement records, events, separation, and order from a Week 6 reconstruction run."""

import argparse
from pathlib import Path

from minivision_reconstruct.movement.pipeline import analyze_movement


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--reconstruction-run", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(analyze_movement(args.cases_root.resolve(), args.case_id, args.reconstruction_run, args.config.resolve(), args.run_id))


if __name__ == "__main__":
    main()
