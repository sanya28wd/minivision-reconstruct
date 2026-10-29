"""Create the synthetic boulevard, roundabout, and intersection replay gallery."""

import argparse
from pathlib import Path

from minivision_reconstruct.reconstruction.demo import create_demo_gallery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    args = parser.parse_args()
    for case in create_demo_gallery(args.cases_root.resolve(), args.ffmpeg, args.ffprobe):
        print(case.reconstruction_directory)


if __name__ == "__main__":
    main()
