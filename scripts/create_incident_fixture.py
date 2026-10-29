"""Create visibly synthetic incident scenario cases with exact collision, PET, and contribution ground truth for Week 8."""

import argparse
from pathlib import Path

from minivision_reconstruct.incident.staged_fixture import SCENARIOS, create_incident_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-prefix", required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, required=True, help="Scenario names, or every name for the full set.")
    parser.add_argument("--pixel-noise-sigma", type=float, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args()
    for seed in args.seeds:
        for case in create_incident_fixture(args.cases_root.resolve(), args.case_prefix, seed, args.pixel_noise_sigma, args.ffmpeg, args.ffprobe, tuple(args.scenarios)):
            print(case.case_id, case.scenario, case.reconstruction_directory, case.ground_truth_path)


if __name__ == "__main__":
    main()
