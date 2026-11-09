"""Package unchanged producer JSON artifacts for the existing evidence importer."""

from datetime import UTC, datetime
import json
from pathlib import Path
from pathlib import PureWindowsPath, PurePosixPath
import shutil
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident import review


def producer_revision():
    repository = Path(__file__).resolve().parents[2]
    command = ["git"]
    pointer = repository / ".git"
    if pointer.is_file():
        location = pointer.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
        windows = PureWindowsPath(location)
        if windows.drive:
            metadata = (
                Path("/mnt") / windows.drive[0].lower() / Path(*windows.parts[1:])
            )
        else:
            metadata = (repository / location).resolve()
        command.extend(["--git-dir=" + str(metadata), "--work-tree=" + str(repository)])
    try:
        return subprocess.run(
            [*command, "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("Producer revision is unavailable") from error


def selected_artifacts(case, run, incident, reviewed, rec, body, video_hash):
    """Follow the selected manifests' hash closure, excluding unrelated histories."""
    from integration.tools_worker.replay import safe_file

    index = {}
    for path in sorted(case.rglob("*")):
        if path.suffix in {".json", ".jsonl"} and not {
            "annotations",
            "exports",
            "snapshots",
        }.intersection(path.relative_to(case).parts):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Linked producer artifact")
            index.setdefault(sha256_file(path), path)
    movement = (
        case
        / "analysis/movement"
        / json.loads((incident / "manifest.json").read_bytes())["movement_run"]
    )
    pending = [
        run / "manifest.json",
        movement / "manifest.json",
        incident / "manifest.json",
        reviewed / "manifest.json",
        case / "metadata/evidence_manifest.json",
    ]
    if (case / "metadata/video_metadata.json").is_file():
        pending.append(case / "metadata/video_metadata.json")
    pending.extend(
        case.parent / rec[name]
        for name in ("source_tracks_path", "source_calibration_path")
    )
    for category, field in (
        ("scenes", "scene_revision"),
        ("markers", "marker_revision"),
    ):
        if body[field] != "initial":
            pending.append(run / category / body[field] / "revision.json")
    selected = set()
    while pending:
        path = pending.pop()
        path = safe_file(case, path.relative_to(case).as_posix())
        if path in selected:
            continue
        selected.add(path)
        if path.name == "revision.json" and (path.parent / "sha256.json").is_file():
            pending.append(path.parent / "sha256.json")
        raw = path.read_text(encoding="utf-8")
        documents = (
            [json.loads(line) for line in raw.splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else [json.loads(raw)]
        )
        nodes = list(documents)
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                for artifact in node.get("artifacts", ()):
                    child = path.parent / artifact["filename"]
                    if child.suffix in {".json", ".jsonl"}:
                        pending.append(child)
                for name, value in node.items():
                    if (
                        name.endswith("_sha256")
                        and isinstance(value, str)
                        and value != video_hash
                    ):
                        if value not in index:
                            raise ValueError("Selected producer dependency is missing")
                        pending.append(index[value])
                    elif isinstance(value, (dict, list)):
                        nodes.append(value)
            elif isinstance(node, list):
                nodes.extend(node)
    return sorted(selected)


def export_bundle(exports_root, case, run, incident, evidence, body):
    from integration.tools_worker.replay import safe_file

    if exports_root is None:
        raise ValueError("Configure the evidence exports root")
    template = body["export_template"]
    sources = [
        source
        for source in template["sources"]
        if source["view_id"] == body["source_view_id"]
    ]
    if len(sources) != 1:
        raise ValueError("Select one attached source")
    source = sources[0]
    if (source.get("producer_case_id") or template["case_id"]) != case.name or source[
        "original_video_sha256"
    ] != evidence.preserved_sha256:
        raise ValueError("Different attached evidence")
    revision = body["review_revision"]
    # Public loading validates the revision identifier and its integrity before joins.
    review.effective_view(incident, revision)
    reviewed = incident / "reviewed" / revision
    if not reviewed.exists():
        reviewed = review.export_reviewed_view(incident, revision)
    manifest = json.loads((reviewed / "manifest.json").read_bytes())
    if manifest["review_revision"] != revision or manifest[
        "source_incident_manifest_sha256"
    ] != sha256_file(incident / "manifest.json"):
        raise ValueError("Different reviewed view")
    for artifact in manifest["artifacts"]:
        if sha256_file(safe_file(reviewed, artifact["filename"])) != artifact["sha256"]:
            raise ValueError("Reviewed artifact changed")
    rec = json.loads((run / "manifest.json").read_bytes())
    movement_run = json.loads((incident / "manifest.json").read_bytes())["movement_run"]
    relative = lambda path: path.relative_to(case.parent).as_posix()
    old_mapping = str(
        PurePosixPath(source["reconstruction_manifest"]).parent / "replay_frames.jsonl"
    )
    transformations = [
        {**item, "mapping": relative(run / "replay_frames.jsonl")}
        if item
        == {"operation": "producer reconstruction replay", "mapping": old_mapping}
        else item
        for item in source["transformations"]
    ]
    selected = {
        **source,
        "producer_case_id": case.name,
        "reconstruction_manifest": relative(run / "manifest.json"),
        "movement_manifest": f"{case.name}/analysis/movement/{movement_run}/manifest.json",
        "incident_manifest": relative(incident / "manifest.json"),
        "reviewed_manifest": relative(reviewed / "manifest.json"),
        "tracking_run": rec["tracking_run"],
        "track_revision": rec["track_revision"],
        "calibration_revision": rec["calibration_revision"],
        "reconstruction_run": run.name,
        "movement_run": movement_run,
        "incident_run": incident.name,
        "review_revision": revision,
        "transformations": transformations,
        "frame_origin": 0,
    }
    producer = producer_revision()
    destination = exports_root / ("native-" + uuid4().hex)
    with TemporaryDirectory(prefix=".native-export-", dir=exports_root) as temporary:
        staged = Path(temporary)
        artifacts = {}
        for path in selected_artifacts(
            case, run, incident, reviewed, rec, body, evidence.preserved_sha256
        ):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Linked or special producer artifact")
            name = relative(safe_file(case, path.relative_to(case).as_posix()))
            output = staged / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
            artifacts[name] = sha256_file(output)
        envelope = {
            **template,
            "producer_revision": producer,
            "created_at": datetime.now(UTC).isoformat(),
            "processing_policy": "local_only",
            "sources": [selected],
            "artifacts": artifacts,
        }
        (staged / "bundle.json").write_text(
            json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        staged.rename(destination)
    return {"bundle": destination.name, "revision": revision}
