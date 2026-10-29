"""Run the local Streamlit track-review application."""

from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.perception.review_ui import run_review_app


if __name__ == "__main__":
    run_review_app()
