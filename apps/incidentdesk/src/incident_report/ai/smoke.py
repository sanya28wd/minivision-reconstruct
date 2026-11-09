"""Owner-present smoke call on one PUBLIC research MP4. Never invoked by the app or tests."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import psycopg

from incident_report.agreement import AgreementSettings
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import PROMPT_VERSION, GeminiAIConnection, GeminiSettings, PublicResearchVideo
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.config import ConfigError, Environment, Settings
from incident_report.domain import Origin, Role, View
from incident_report.entry import analyse_incident
from incident_report.runlog import RunLog

KEY_NAMING_HELP = (
    "Set GEMINI_API_KEY from one of your keys, e.g. `$env:GEMINI_API_KEY = $env:GOOGLE_API_KEY_1` "
    "(this copies the value without printing it). This command reads only GEMINI_API_KEY or GOOGLE_API_KEY "
    "from the environment, never .env."
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=KEY_NAMING_HELP, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--role", choices=[role.value for role in Role], required=True)
    parser.add_argument("--public-research", action="store_true", help="declare this file public research footage")
    args = parser.parse_args(argv)
    transport = None
    try:
        video = PublicResearchVideo(args.video, args.public_research)
        settings = Settings.from_env()
        if settings.environment is not Environment.DEV:
            raise AIConnectionError("The owner smoke command is for the dev environment only.")
        model_settings = GeminiSettings.from_env()
        transport = GoogleVideoTransport.from_env(model_settings)
        connection = GeminiAIConnection(model_settings, video, transport=transport)
        with video.path.open("rb") as stream:
            report = analyse_incident(
                [View(stream, video.path.name, Role(args.role))],
                Origin.BENCHMARK,
                ai=connection,
                run_log=RunLog(settings.database_url),
                prompt_version=PROMPT_VERSION,
                agreement_settings=AgreementSettings(n_runs=1),
            )
        print(f"Overview recorded in Run {report.run_id}. Inspect the facts and raw response before accepting it.")
    except (AIConnectionError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, psycopg.Error):
        print(
            "The smoke call could not read the video or persist the Run. Check dev storage/database access.",
            file=sys.stderr,
        )
        return 1
    finally:
        if transport is not None:
            transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
