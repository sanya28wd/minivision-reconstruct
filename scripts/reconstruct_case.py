"""Launch with streamlit run scripts/reconstruct_case.py -- --cases-root cases --media-port 8766 --ffmpeg ffmpeg."""

from minivision_reconstruct.reconstruction.ui import run_reconstruction_app


if __name__ == "__main__":
    run_reconstruction_app()
