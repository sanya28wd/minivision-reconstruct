"""Select an ACCIDENT subset (synthetic Week 8, or the real-footage evaluation set) as a download list, or register downloaded clips in data/registry.csv."""

import argparse
from datetime import date
from pathlib import Path

from minivision_reconstruct.incident.accident_benchmark import RealSubsetPlan, SubsetPlan, register_subset, select_real_subset, select_subset, write_selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="Write the stratified, scene-disjoint selection to download.")
    select.add_argument("--manifest", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--per-type-dev", type=int, required=True)
    select.add_argument("--per-type-heldout", type=int, required=True)
    select.add_argument("--seed", type=int, required=True)
    real = commands.add_parser("select-real", help="Write the real-clip development/test selection (geo-aware, one clip per source).")
    real.add_argument("--manifest", type=Path, required=True)
    real.add_argument("--output", type=Path, required=True)
    real.add_argument("--per-type-dev", type=int, required=True)
    real.add_argument("--per-type-test", type=int, required=True)
    real.add_argument("--min-height", type=int, required=True)
    real.add_argument("--seed", type=int, required=True)
    real.add_argument("--exclude", default="", help="Comma-separated clip IDs already inspected (write --exclude=... since IDs may start with '-'); "
                      "they and their sources never enter the selection.")
    register = commands.add_parser("register", help="Hash downloaded clips and append their registry rows.")
    register.add_argument("--selection", type=Path, required=True)
    register.add_argument("--data-root", type=Path, required=True, help="Folder that contains synthetic_videos/ from the Kaggle download.")
    register.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "select-real":
        rows = select_real_subset(args.manifest, RealSubsetPlan(args.per_type_dev, args.per_type_test, args.min_height, args.seed, frozenset(clip for clip in args.exclude.split(",") if clip)))
        write_selection(rows, args.output)
        print(f"Selected {len(rows)} real clips into {args.output} "
              f"({sum(row['split'] == 'real_dev' for row in rows)} development, {sum(row['split'] == 'real_test' for row in rows)} test).")
    elif args.command == "select":
        rows = select_subset(args.manifest, SubsetPlan(args.per_type_dev, args.per_type_heldout, args.seed))
        write_selection(rows, args.output)
        print(f"Selected {len(rows)} clips into {args.output}. Download these files from the Kaggle dataset, keeping their paths:")
        for row in rows:
            print(f"  {row['split']:14s} {row['rgb_path']}\n  {'':14s} {row['annotations_path']}")
    else:
        registered, missing = register_subset(args.selection, args.data_root, args.registry, Path.cwd(), date.today().isoformat())
        print(f"Registered {len(registered)} clips; {len(missing)} still missing: {', '.join(missing) or 'none'}")


if __name__ == "__main__":
    main()
