"""Create visibly synthetic staged-motion cases with exact movement ground truth for Week 7 validation."""

import argparse
from pathlib import Path

from minivision_reconstruct.movement.staged_fixture import create_staged_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-prefix", required=True)
    parser.add_argument("--seeds", type=int, nargs="+", required=True)
    parser.add_argument("--pixel-noise-sigma", type=float, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args()
    for seed in args.seeds:
        result = create_staged_case(args.cases_root.resolve(), f"{args.case_prefix}-{seed:03d}", seed, args.pixel_noise_sigma, args.duration_seconds, args.ffmpeg, args.ffprobe)
        print(result.case_id, result.reconstruction_directory, result.ground_truth_path)


if __name__ == "__main__":
    main()
