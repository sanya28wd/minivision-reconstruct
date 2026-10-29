"""Create a visibly synthetic case with known geometry and reviewed reconstruction inputs."""

import argparse
from pathlib import Path

from minivision_reconstruct.reconstruction.demo import create_controlled_demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--road-users", type=int, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args()
    result = create_controlled_demo(args.cases_root.resolve(), args.case_id, args.duration_seconds, args.road_users, args.ffmpeg, args.ffprobe)
    print(result.reconstruction_directory)


if __name__ == "__main__":
    main()
