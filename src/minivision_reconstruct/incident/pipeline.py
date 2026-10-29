"""Integrity-checked Week 8 incident analysis of one movement run, written as one immutable run directory."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter, ValidationError

from minivision_reconstruct import __version__
from minivision_reconstruct.domain.models import EvidenceManifest
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.collision import IncidentInputs, propose_incidents
from minivision_reconstruct.incident.contribution import ContributionInputs, assess_contributions
from minivision_reconstruct.incident.models import IncidentConfig, IncidentError, IncidentManifest
from minivision_reconstruct.incident.pet import post_encroachment
from minivision_reconstruct.movement.models import MovementEvent, MovementManifest, MovementObservation, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext
from minivision_reconstruct.movement.pipeline import _write_jsonl
from minivision_reconstruct.movement.road_use import road_geometry
from minivision_reconstruct.reconstruction.models import ArtifactHash, require_child, require_identifier
from minivision_reconstruct.reconstruction.pipeline import load_calibration
from minivision_reconstruct.reconstruction.storage import load_bundle, read_record, read_records, verify_hash, write_record


PIPELINE_VERSION = f"{__version__}+incident-2"
LIMITATIONS: tuple[str, ...] = (
    "Proximity is measured between box bottom-centre contact points, not vehicle bodies or footprints.",
    "No speed, velocity, acceleration, or time-to-collision is calculated; PET uses source-frame times and positions only.",
    "Image box overlap is supporting evidence only; it never creates or confirms a candidate.",
    "Heading jumps come from smoothed Week 7 headings, which are unavailable once a road user is at rest.",
    "Every candidate is a system proposal for reviewer confirmation, not a determination that a collision occurred.",
    "Contribution assessments are analytical proposals from observed actions, not official determinations; no road-rule source is applied.",
)


def load_incident_config(path: Path) -> IncidentConfig:
    """Read the explicit incident configuration; no field has a default."""
    return read_record(path, IncidentConfig)


def analyze_incident(cases_root: Path, case_id: str, movement_run: str, config_path: Path, run_id: str) -> Path:
    """Compute PET conflicts, collision, near-miss, and single-vehicle candidates, and contribution assessments for one movement run."""
    require_identifier(case_id, "case ID")
    require_identifier(movement_run, "movement run")
    require_identifier(run_id, "incident run")
    cases_root = cases_root.resolve()
    case_root = cases_root / case_id
    movement_directory = require_child(case_root / "analysis" / "movement" / movement_run, case_root)
    movement_manifest_path = movement_directory / "manifest.json"
    movement_hash = sha256_file(movement_manifest_path)
    movement = read_record(movement_manifest_path, MovementManifest)
    for item in movement.artifacts:
        verify_hash(require_child(movement_directory / item.filename, movement_directory), item.sha256)
    reconstruction_directory = require_child(case_root / "analysis" / "reconstruction" / movement.reconstruction_run, case_root)
    verify_hash(reconstruction_directory / "manifest.json", movement.source_reconstruction_manifest_sha256)
    bundle = load_bundle(reconstruction_directory, "initial", "initial")
    try:
        evidence = TypeAdapter(EvidenceManifest).validate_json((case_root / "metadata" / "evidence_manifest.json").read_bytes())
    except (OSError, ValidationError) as error:
        raise IncidentError(f"Invalid evidence manifest for case '{case_id}': {error}") from error
    if evidence.frame_rate_decimal is None or evidence.frame_rate_decimal <= 0:
        raise IncidentError(f"Case '{case_id}' has no valid frame rate; incident timing cannot be bounded.")
    calibration_path = require_child(cases_root / movement.source_calibration_path, case_root)
    verify_hash(calibration_path, movement.source_calibration_sha256)
    calibration = load_calibration(calibration_path, evidence, case_root)
    config = load_incident_config(config_path)
    config_hash = sha256_file(config_path)
    context = MotionContext(case_id, 1 / evidence.frame_rate_decimal, movement.unit, movement.world_is_right_handed, f"incident-pipeline {PIPELINE_VERSION}")
    smoothed = read_records(movement_directory / "smoothed.jsonl", SmoothedPoint)
    observations = read_records(movement_directory / "movement_observations.jsonl", MovementObservation)
    movement_events = read_records(movement_directory / "events.jsonl", MovementEvent)
    conflicts = post_encroachment(smoothed, config.conflict, context)
    events = propose_incidents(IncidentInputs(
        smoothed=smoothed, movement=observations, events=movement_events, conflicts=conflicts,
        observations=bundle.observations, lanes=road_geometry(calibration).lanes,
    ), config, context)
    assessments = assess_contributions(events, ContributionInputs(
        smoothed=smoothed, movement=observations, events=movement_events, conflicts=conflicts,
        world_is_right_handed=movement.world_is_right_handed, metric=movement.calibration_mode == "metric",
    ), config, context)
    if len({event.event_id for event in events}) != len(events) or len({item.conflict_id for item in conflicts}) != len(conflicts):
        raise IncidentError("Incident analysis produced duplicate IDs; report this input for investigation.")
    root = case_root / "analysis" / "incident"
    root.mkdir(exist_ok=True)
    destination = root / run_id
    if destination.exists():
        raise IncidentError(f"Incident run '{destination}' already exists; choose a new run ID.")
    with TemporaryDirectory(prefix=".incident-", dir=root) as temporary:
        staged = Path(temporary)
        _write_jsonl(staged / "conflicts.jsonl", conflicts)
        _write_jsonl(staged / "events.jsonl", events)
        _write_jsonl(staged / "assessments.jsonl", assessments)
        (staged / "config.json").write_bytes(config_path.read_bytes())
        limitations = LIMITATIONS + (() if movement.calibration_mode == "metric" else (
            "Relative calibration: distance thresholds are applied in relative units and are not physical measurements.",))
        manifest = IncidentManifest(
            schema_version="1.0", case_id=case_id, run_id=run_id, movement_run=movement_run, source_movement_manifest_sha256=movement_hash,
            config_id=config.config_id, config_sha256=config_hash, pipeline_version=PIPELINE_VERSION, unit=movement.unit,
            conflict_count=len(conflicts), event_count=len(events), assessment_count=len(assessments), created_at=datetime.now(UTC).isoformat(),
            limitations=limitations,
            artifacts=tuple(ArtifactHash(filename=path.name, sha256=sha256_file(path)) for path in sorted(staged.iterdir())),
        )
        verify_hash(movement_manifest_path, movement_hash)
        write_record(staged / "manifest.json", manifest)
        staged.rename(destination)
    return destination
