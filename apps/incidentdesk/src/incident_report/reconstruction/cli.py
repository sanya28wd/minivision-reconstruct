"""Local attach command; database and exports root come from owner configuration."""

import argparse
import json
import os
import uuid

import psycopg

from .config import ReconstructionSettings
from .database import local_database
from .store import Workspace, WorkspaceRefused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    attach = commands.add_parser("attach")
    attach.add_argument("--incident", required=True, type=uuid.UUID)
    attach.add_argument("--attached-by", required=True)
    attach.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        settings = ReconstructionSettings.from_env()
        if not settings.enabled:
            raise WorkspaceRefused("Reconstruction workspace is disabled")
        database = os.environ.get("DATABASE_URL", "")
        if not database:
            raise WorkspaceRefused("DATABASE_URL is required")
        with psycopg.connect(local_database(database)) as connection:
            workspace = Workspace(connection, settings)
            links = workspace.attach(args.incident, args.bundle, args.attached_by)
            result = [
                {
                    "link_id": str(link.link_id),
                    "import_id": link.import_id,
                    "view": link.view_sha256,
                    "note": workspace.attachment_note(link),
                }
                for link in links
            ]
        print(json.dumps(result))
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    except psycopg.Error:
        parser.exit(2, "Local workspace database is unavailable\n")
