"""Typed, immutable incident artifacts: conflicts, candidates, contribution assessments, and review decisions.

Deliberately without speed, velocity, acceleration, or time-to-collision fields. Every system record is a
proposal; reviewer decisions are stored separately so the original proposal is never rewritten.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from minivision_reconstruct.movement.models import DistanceUnit, EventConfidence, EventStatus
from minivision_reconstruct.reconstruction.models import Artifact, ArtifactHash, Point, ReconstructionError


IncidentEventType = Literal["collision_candidate", "near_miss_candidate", "single_vehicle_candidate"]
CollisionType = Literal["rear_end", "sideswipe", "t_bone", "head_on", "single", "unknown"]
SignalName = Literal[
    "closest_approach", "simultaneous_occupancy",
    "post_event_stop", "heading_jump", "post_event_obstruction",
    "image_box_overlap",
]
SignalKind = Literal["proximity", "consequence", "supporting"]
SIGNAL_KINDS: dict[str, SignalKind] = {
    "closest_approach": "proximity",
    "simultaneous_occupancy": "proximity",
    "post_event_stop": "consequence",
    "heading_jump": "consequence",
    "post_event_obstruction": "consequence",
    "image_box_overlap": "supporting",
}
ContributionOutcome = Literal["ONE_CONTRIBUTOR", "MULTIPLE_CONTRIBUTORS", "NO_SUPPORTED_CONTRIBUTOR", "UNDETERMINED"]
ContributionRule = Literal[
    "did_not_stop_behind_stopped_road_user",
    "lane_change_into_occupied_lane",
    "entered_occupied_conflict_area",
    "no_visible_stop_at_stop_line",
    "turned_across_path",
]
UnresolvedKind = Literal["unavailable_frames", "interpolated_frames", "reviewer_track", "mirrored_coordinates", "non_metric_calibration"]
ReviewAction = Literal["confirm", "reject", "modify"]
NOT_A_VIOLATION_FINDING = "No approved agency rule source; this is an observed action, not a violation finding."


class IncidentError(ReconstructionError):
    """Invalid incident configuration, inputs, review decisions, or saved artifacts."""


# --- Configuration: every field is required; there are no code defaults. ---


class ConflictConfig(Artifact):
    conflict_radius_floor: float = Field(gt=0, description="Smallest conflict-area radius around a path crossing, in calibration units.")
    uncertainty_coverage_factor: float = Field(gt=0, description="Multiplier on the pair's combined position uncertainty when sizing the conflict area.")
    maximum_pet_seconds: float = Field(gt=0, description="Crossings whose PET exceeds this are not recorded as conflicts.")


class CollisionConfig(Artifact):
    contact_distance: float = Field(ge=0, description="Contact-point separation treated as possible contact, before adding uncertainty. Contact points are box bottom-centres, not bodies.")
    post_window_seconds: float = Field(gt=0, description="How long after the proximity peak a consequence signal may start.")
    heading_jump_degrees: float = Field(gt=0, description="Heading change that counts as an abrupt jump.")
    heading_jump_window_frames: int = Field(ge=1, description="Frames over which the heading jump is measured.")
    obstruction_seconds: float = Field(gt=0, description="Time stopped inside a lane after the peak that counts as post-event obstruction.")
    minimum_consequence_signals: int = Field(ge=0, description="Consequence signals required for a collision candidate; 0 is the proximity-only ablation.")


class NearMissConfig(Artifact):
    near_miss_pet_seconds: float = Field(gt=0, description="Positive PET at or below this, without consequences, is a near miss.")
    near_miss_distance: float = Field(gt=0, description="Minimum separation below this, without consequences, is a near miss.")


class SingleVehicleConfig(Artifact):
    partner_exclusion_distance: float = Field(gt=0, description="No other road user may be closer than this for a single-vehicle candidate.")


class CollisionTypeConfig(Artifact):
    same_direction_max_degrees: float = Field(gt=0, lt=90, description="Relative heading below this is rear-end or sideswipe.")
    rear_end_max_lateral_offset: float = Field(gt=0, description="Same-direction lateral offset at or below this is rear-end; above it, sideswipe.")
    t_bone_min_degrees: float = Field(gt=0, lt=180, description="Lower bound of the relative-heading band for a t-bone.")
    t_bone_max_degrees: float = Field(gt=0, lt=180, description="Upper bound of the relative-heading band for a t-bone.")
    head_on_min_degrees: float = Field(gt=0, le=180, description="Relative heading at or above this is head-on.")

    @model_validator(mode="after")
    def _ordered_bands(self) -> Self:
        if not self.same_direction_max_degrees <= self.t_bone_min_degrees < self.t_bone_max_degrees <= self.head_on_min_degrees:
            raise ValueError("Collision-type bands must satisfy same_direction_max <= t_bone_min < t_bone_max <= head_on_min.")
        return self


class ContributionConfig(Artifact):
    lookback_seconds: float = Field(gt=0, description="Pre-event window searched for each participant's actions.")
    lane_change_window_seconds: float = Field(gt=0, description="A lane change this close before the conflict can be a contributing action.")
    turn_window_seconds: float = Field(gt=0, description="A turn this close before the conflict can be a contributing action.")
    stop_line_window_seconds: float = Field(gt=0, description="A stop-line crossing this close before the conflict is checked for a visible stop.")


class IncidentValidationConfig(Artifact):
    impact_time_tolerance_seconds: float = Field(gt=0, description="Declared tolerance for impact-time error.")
    impact_location_tolerance: float = Field(gt=0, description="Declared tolerance for impact-location error, in calibration units.")
    pet_tolerance_seconds: float = Field(gt=0, description="Declared tolerance for PET error.")
    matching_gate_factor: float = Field(ge=1, description="Proposals are matched to truth within this multiple of the tolerance.")


class IncidentConfig(Artifact):
    schema_version: Literal["1.0"]
    config_id: str = Field(min_length=1)
    conflict: ConflictConfig
    collision: CollisionConfig
    near_miss: NearMissConfig
    single_vehicle: SingleVehicleConfig
    collision_type: CollisionTypeConfig
    contribution: ContributionConfig
    validation: IncidentValidationConfig

    @model_validator(mode="after")
    def _near_miss_beyond_contact(self) -> Self:
        if self.near_miss.near_miss_distance <= self.collision.contact_distance:
            raise ValueError("near_miss_distance must exceed contact_distance, or every near miss would also be contact.")
        return self


# --- Post-encroachment time ---


class ConflictRecord(Artifact):
    """PET at a crossing of two smoothed paths: time between the first road user leaving and the second entering."""

    conflict_id: str
    case_id: str
    track_first: int = Field(ge=1, description="Road user that entered the conflict area first; unless PET <= 0 it also left first.")
    track_second: int = Field(ge=1)
    location: Point
    radius: float = Field(gt=0)
    unit: DistanceUnit
    first_enter_frame: int = Field(ge=0)
    first_exit_frame: int = Field(ge=0)
    second_enter_frame: int = Field(ge=0)
    second_exit_frame: int = Field(ge=0)
    first_exit_time: float = Field(ge=0)
    second_enter_time: float = Field(ge=0)
    pet_seconds: float = Field(description="second_enter_time - first_exit_time; zero or negative means both occupied the area together.")
    pet_window: tuple[float, float]
    simultaneous_occupancy: bool
    evidence_signals: tuple[str, ...] = Field(min_length=1)
    confidence: EventConfidence

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.track_first == self.track_second:
            raise ValueError("A conflict needs two different road users.")
        if self.first_enter_frame > self.first_exit_frame or self.second_enter_frame > self.second_exit_frame:
            raise ValueError("Each road user must enter the conflict area before leaving it.")
        if abs(self.pet_seconds - (self.second_enter_time - self.first_exit_time)) > 1e-6:
            raise ValueError("pet_seconds must equal second_enter_time - first_exit_time.")
        if self.simultaneous_occupancy != (self.pet_seconds <= 0):
            raise ValueError("simultaneous_occupancy must be true exactly when PET is zero or negative.")
        if not self.pet_window[0] <= self.pet_seconds <= self.pet_window[1]:
            raise ValueError("pet_window must contain pet_seconds.")
        return self


# --- Collision, near-miss, and single-vehicle candidates (README §6.6) ---


class EvidenceSignal(Artifact):
    name: SignalName
    kind: SignalKind
    track_ids: tuple[int, ...] = Field(min_length=1)
    source_frames: tuple[int, ...] = Field(min_length=1)
    supporting_event_ids: tuple[str, ...]
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def _kind_matches_name(self) -> Self:
        if SIGNAL_KINDS[self.name] != self.kind:
            raise ValueError(f"Signal '{self.name}' is a {SIGNAL_KINDS[self.name]} signal, not {self.kind}.")
        return self


class PhaseBounds(Artifact):
    """README §7 step 13: the sequence split into pre-event, event, and post-event phases (source seconds)."""

    pre_event: tuple[float, float]
    event: tuple[float, float]
    post_event: tuple[float, float]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        bounds = (*self.pre_event, *self.event, *self.post_event)
        if any(later < earlier for earlier, later in zip(bounds, bounds[1:])):
            raise ValueError("Phases must be ordered and non-overlapping: pre-event, event, post-event.")
        return self


class IncidentEvent(Artifact):
    event_id: str
    case_id: str
    event_type: IncidentEventType
    track_ids: tuple[int, ...] = Field(min_length=1)
    start_frame: int = Field(ge=0)
    peak_frame: int = Field(ge=0, description="Proposed impact or closest-conflict frame.")
    end_frame: int = Field(ge=0)
    start_time: float = Field(ge=0)
    peak_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    time_window: tuple[float, float]
    location: Point
    location_uncertainty: float = Field(ge=0)
    unit: DistanceUnit
    signals: tuple[EvidenceSignal, ...] = Field(min_length=1)
    conflict_id: str | None
    phases: PhaseBounds
    predicted_collision_type: CollisionType
    confidence: EventConfidence
    provenance: Literal["inferred"]
    status: Literal["PROPOSED_BY_SYSTEM"]
    created_by: str

    @model_validator(mode="after")
    def _gate(self) -> Self:
        kinds = {signal.kind for signal in self.signals}
        if not self.start_frame <= self.peak_frame <= self.end_frame:
            raise ValueError("Frames must satisfy start <= peak <= end.")
        if not self.time_window[0] <= self.peak_time <= self.time_window[1]:
            raise ValueError("time_window must contain peak_time.")
        if self.event_type == "single_vehicle_candidate":
            if len(self.track_ids) != 1 or self.predicted_collision_type != "single":
                raise ValueError("A single-vehicle candidate has one road user and collision type 'single'.")
            if "consequence" not in kinds:
                raise ValueError("A single-vehicle candidate needs at least one consequence signal.")
        else:
            if len(self.track_ids) != 2 or self.predicted_collision_type == "single":
                raise ValueError("Collision and near-miss candidates involve exactly two road users.")
            if "proximity" not in kinds:
                raise ValueError("Collision and near-miss candidates need a proximity signal; image box overlap alone is never enough.")
        return self


# --- Contribution assessment (README §6.7) ---


class ActionEvidence(Artifact):
    """One observed action of one road user, linked to the movement events and frames that show it."""

    rule: ContributionRule
    track_id: int = Field(ge=1)
    description: str = Field(min_length=1)
    event_ids: tuple[str, ...] = Field(min_length=1)
    source_frames: tuple[int, ...] = Field(min_length=1)
    action_time: float = Field(ge=0)
    seconds_before_peak: float = Field(ge=0)


class UnresolvedFact(Artifact):
    kind: UnresolvedKind
    track_ids: tuple[int, ...]
    time_window: tuple[float, float] | None
    description: str = Field(min_length=1)
    decisive: bool = Field(description="True when the fact falls inside a window a rule depends on.")


class ContributionAssessment(Artifact):
    assessment_id: str
    case_id: str
    event_id: str
    involved_track_ids: tuple[int, ...] = Field(min_length=1)
    contributing_track_ids: tuple[int, ...]
    outcome: ContributionOutcome
    lookback_window: tuple[float, float]
    observed_actions: tuple[str, ...] = Field(description="Movement event IDs inside the lookback window, for every involved road user.")
    supporting_evidence: tuple[ActionEvidence, ...]
    counterevidence: tuple[ActionEvidence, ...]
    applicable_road_context_refs: tuple[str, ...]
    unresolved_facts: tuple[UnresolvedFact, ...]
    rule_source: None = Field(description="Always null until an approved agency rule source exists.")
    disclaimer: Literal["No approved agency rule source; this is an observed action, not a violation finding."]
    confidence: EventConfidence
    provenance: Literal["inferred"]
    status: Literal["PROPOSED_BY_SYSTEM"]
    created_by: str

    @model_validator(mode="after")
    def _outcome_consistent(self) -> Self:
        contributors = set(self.contributing_track_ids)
        if not contributors <= set(self.involved_track_ids):
            raise ValueError("Contributing road users must be involved in the event.")
        expected = {"ONE_CONTRIBUTOR": len(contributors) == 1, "MULTIPLE_CONTRIBUTORS": len(contributors) >= 2,
                    "NO_SUPPORTED_CONTRIBUTOR": not contributors, "UNDETERMINED": not contributors}
        if not expected[self.outcome]:
            raise ValueError(f"Outcome {self.outcome} does not match {len(contributors)} contributing road user(s).")
        if {item.track_id for item in self.supporting_evidence} != contributors:
            raise ValueError("Every contributor needs supporting evidence, and supporting evidence may only name contributors.")
        if any(fact.decisive for fact in self.unresolved_facts) and self.outcome != "UNDETERMINED":
            raise ValueError("A decisive unresolved fact requires an UNDETERMINED outcome.")
        if self.outcome == "NO_SUPPORTED_CONTRIBUTOR" and self.unresolved_facts:
            raise ValueError("NO_SUPPORTED_CONTRIBUTOR requires complete data; use UNDETERMINED when facts are unresolved.")
        return self


# --- Reviewer decisions: stored as revisions beside, never inside, the proposals ---


class ReviewDecision(Artifact):
    target_kind: Literal["event", "assessment"]
    target_id: str
    action: ReviewAction
    note: str = Field(max_length=2000)
    modified_peak_time: float | None = Field(ge=0)
    modified_location: Point | None
    modified_outcome: ContributionOutcome | None
    modified_contributing_track_ids: tuple[int, ...] | None
    reviewer: str = Field(min_length=1, max_length=120)
    decided_at: str

    @model_validator(mode="after")
    def _fields_match_action(self) -> Self:
        event_fields = (self.modified_peak_time, self.modified_location)
        assessment_fields = (self.modified_outcome, self.modified_contributing_track_ids)
        if self.action != "modify":
            if any(value is not None for value in (*event_fields, *assessment_fields)):
                raise ValueError("Only a 'modify' decision may change fields.")
        else:
            own, other = (event_fields, assessment_fields) if self.target_kind == "event" else (assessment_fields, event_fields)
            if all(value is None for value in own) or any(value is not None for value in other):
                raise ValueError(f"A 'modify' decision on an {self.target_kind} must change at least one of its own fields and no others.")
        if self.action in {"reject", "modify"} and not self.note.strip():
            raise ValueError("Rejecting or modifying a proposal requires a reviewer note.")
        return self


class IncidentReviewDocument(Artifact):
    schema_version: Literal["1.0"]
    decisions: tuple[ReviewDecision, ...]


class IncidentReviewRevision(Artifact):
    revision_id: str
    reviewer: str
    created_at: str
    previous_sha256: str
    document: IncidentReviewDocument


class ReviewedEvent(Artifact):
    """A proposal with the reviewer decision in force; the proposal itself is embedded unchanged."""

    proposal: IncidentEvent
    status: EventStatus
    peak_time: float = Field(ge=0)
    location: Point
    decision: ReviewDecision | None


class ReviewedAssessment(Artifact):
    proposal: ContributionAssessment
    status: EventStatus
    event_status: EventStatus = Field(description="Status of the assessed event; an assessment of a rejected event is moot.")
    outcome: ContributionOutcome
    contributing_track_ids: tuple[int, ...]
    decision: ReviewDecision | None


class ReviewedExportManifest(Artifact):
    schema_version: Literal["1.0"]
    case_id: str
    run_id: str
    review_revision: str
    source_incident_manifest_sha256: str
    review_revision_sha256: str | None = Field(description="Null for the unreviewed initial view.")
    created_at: str
    artifacts: tuple[ArtifactHash, ...]


class IncidentManifest(Artifact):
    schema_version: Literal["1.0"]
    case_id: str
    run_id: str
    movement_run: str
    source_movement_manifest_sha256: str
    config_id: str
    config_sha256: str
    pipeline_version: str
    unit: DistanceUnit
    conflict_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    assessment_count: int = Field(ge=0)
    created_at: str
    limitations: tuple[str, ...]
    artifacts: tuple[ArtifactHash, ...]
