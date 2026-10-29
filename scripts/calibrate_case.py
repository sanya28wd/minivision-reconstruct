"""Run the local Streamlit road-calibration application."""

from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.perception.calibration_ui import run_calibration_app


if __name__ == "__main__":
    run_calibration_app()
