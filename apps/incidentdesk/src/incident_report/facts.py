"""Typed facts contract and L0 citation validator (RPT-11, spec 001, ADR 0006).

Every fact extracted from footage carries:
- description: plain English observation (parties described by colour/make/body type, never raw IDs)
- view_id: fingerprint of the view it was observed in. Attached by the code, never returned by the model.
- timestamp_seconds: moment in seconds from the start of that view
- confidence: numeric confidence in [0.0, 1.0]

The L0 citation validator enforces:
1. The cited view exists in the request.
2. The cited timestamp is non-negative and within the view's duration.
3. Every fault- or violation-relevant fact carries a valid citation.
4. No raw IDs are used for parties.
5. Confidence meets the minimum threshold (project assumption, to be validated).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

# ADR 0006: Thresholds and parameters cite the research or measurement they come from,
# or are labelled "project assumption, to be validated".
DEFAULT_MIN_CONFIDENCE: float = 0.5  # project assumption, to be validated (ADR 0006)

# Pattern detecting raw synthetic IDs for parties (e.g. "Vehicle 1", "car_2", "V1", "object_0", "party 1", etc.)
# "Name vehicles by colour + make/model + body type, never raw IDs." (AGENTS.md, spec 001)
_RAW_ID_PATTERN = re.compile(
    r"^(?:vehicle|car|truck|van|bus|suv|auto|actor|party|object|entity|participant|v|c|p)[ _-]?\d+$",
    re.IGNORECASE,
)
_NUMERIC_OR_ID_PATTERN = re.compile(r"^(?:id[ _-]?)?\d+$", re.IGNORECASE)


def is_raw_id(text: str) -> bool:
    """Return True if text resembles a raw synthetic identifier instead of a descriptive entity."""
    stripped = text.strip()
    return bool(_RAW_ID_PATTERN.fullmatch(stripped) or _NUMERIC_OR_ID_PATTERN.fullmatch(stripped))


class CitedFact(BaseModel):
    """A single observation cited to a specific view and timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    description: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    # Required, no default (ADR 0006): a model that omits confidence must fail validation, never read as 1.0.
    confidence: float = Field(ge=0.0, le=1.0)


class PartyFact(CitedFact):
    """A road user (vehicle and driver, pedestrian, cyclist, animal).

    Vehicles must be described by colour + make/model + body type, never raw IDs (ADR 0004, AGENTS.md).
    """

    color: str | None = None
    make_model: str | None = None
    body_type: str | None = None
    # overview-10: the per-view checklist code (P1, P2, CAM). It names the party inside one view only and is
    # never a cross-view identity (agreement.party_canonical_key). None = no code was asked for.
    party_code: str | None = None

    @model_validator(mode="after")
    def _reject_raw_id(self) -> Self:
        if is_raw_id(self.description):
            raise ValueError(
                f"Party description {self.description!r} is a raw ID; use colour + make/model + body type."
            )
        return self


# UAE_ROAD_STRUCTURE_AND_PRIORITY.md section 5, S2-S4/S6 and P1-P7/P9-P10/P14/P19-P23.
ConflictZoneType = Literal[
    "signalised_junction",
    "unsignalised_junction",
    "roundabout_unsignalised",
    "roundabout_signalised",
    "mid_block",
    "slip_lane_or_ramp_merge",
    "u_turn_opening",
    "pedestrian_crossing",
    "service_road_connection",
    "car_park_or_driveway_access",
    "other",
    "unknown",
]
VisibilityConditions = Literal["clear", "night", "rain_or_fog_or_dust", "glare", "unknown"]
PartyType = Literal[
    "car",
    "taxi",
    "bus",
    "truck_or_heavy",
    "motorcycle",
    "bicycle_or_personal_transport",
    "pedestrian",
    "emergency_or_official_marked",
    "other",
    "unknown",
]
MotionState = Literal["moving", "stopped_in_lane", "parked_at_kerb", "reversing", "unknown"]
Movement = Literal[
    "straight",
    "left_turn",
    "right_turn",
    "u_turn",
    "lane_change_left",
    "lane_change_right",
    "merging",
    "entering_from_side_road",
    "entering_from_car_park_or_driveway",
    "entering_roundabout",
    "circulating_roundabout",
    "exiting_roundabout",
    "crossing_on_foot",
    "other",
    "unknown",
]
ApproachControl = Literal[
    "signal", "stop_sign_or_line", "give_way_sign_or_line", "officer", "none_confirmed", "not_in_view", "unknown"
]
SignalState = Literal[
    "red",
    "yellow",
    "green",
    "green_arrow",
    "flashing_yellow",
    "signal_dark",
    "not_visible",
    "not_applicable",
    "unknown",
]
RelativePosition = Literal[
    "B_from_A_left",
    "B_from_A_right",
    "B_ahead_oncoming",
    "B_ahead_same_direction",
    "B_behind",
    "B_alongside_adjacent_lane",
    "unknown",
]
RoundaboutEntryState = Literal[
    "already_inside_circulating", "entering_from_approach", "exiting", "not_in_roundabout", "unknown"
]
LaneLine = Literal["none", "broken_white", "solid_white", "solid_or_double_yellow", "hatched_or_chevron", "unknown"]
SignalGiven = Literal["left_indicator", "right_indicator", "hazards", "none_visible", "unknown"]
ImpactZone = Literal[
    "front",
    "front_left",
    "front_right",
    "left_side",
    "right_side",
    "rear",
    "rear_left",
    "rear_right",
    "other",
    "unknown",
]
CrossingPlace = Literal[
    "marked_crossing", "signalised_crossing", "outside_any_crossing", "on_slip_lane_crossing", "unknown"
]
PedestrianSignal = Literal["green", "flashing_green", "red", "no_pedestrian_signal", "not_visible", "unknown"]
EntryOrigin = Literal[
    "public_road_same_road", "side_road", "service_road", "car_park_or_driveway", "roadside_stop", "unpaved", "unknown"
]
YesNoUnknown = Literal["yes", "no", "unknown"]
StructuredField = Literal[
    "conflict_zone_type",
    "officer_directing_traffic",
    "posted_speed_limit_kmh",
    "visibility_conditions",
    "party_type",
    "motion_state_before_contact",
    "movement",
    "approach_control_faced",
    "signal_state_when_crossing_stop_line",
    "roundabout_entry_state",
    "lane_line_crossed_before_contact",
    "signal_given",
    "impact_zone_on_party",
    "emergency_lights",
    "emergency_siren",
    "pedestrian_crossing_place",
    "pedestrian_signal_state",
    "pedestrian_started_before_vehicle_arrived",
    "entry_origin",
    "relative_position_of_other",
]
SCENE_FIELDS = frozenset(
    ("conflict_zone_type", "officer_directing_traffic", "posted_speed_limit_kmh", "visibility_conditions")
)
FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "conflict_zone_type": get_args(ConflictZoneType),
    "officer_directing_traffic": get_args(YesNoUnknown),
    "visibility_conditions": get_args(VisibilityConditions),
    "party_type": get_args(PartyType),
    "motion_state_before_contact": get_args(MotionState),
    "movement": get_args(Movement),
    "approach_control_faced": get_args(ApproachControl),
    "signal_state_when_crossing_stop_line": get_args(SignalState),
    "roundabout_entry_state": get_args(RoundaboutEntryState),
    "lane_line_crossed_before_contact": get_args(LaneLine),
    "signal_given": get_args(SignalGiven),
    "impact_zone_on_party": get_args(ImpactZone),
    "emergency_lights": get_args(YesNoUnknown),
    "emergency_siren": get_args(YesNoUnknown),
    "pedestrian_crossing_place": get_args(CrossingPlace),
    "pedestrian_signal_state": get_args(PedestrianSignal),
    "pedestrian_started_before_vehicle_arrived": get_args(YesNoUnknown),
    "entry_origin": get_args(EntryOrigin),
    "relative_position_of_other": get_args(RelativePosition),
}


class StructuredFact(BaseModel):
    """One visible observation; values follow research section 5, never legal conclusions."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    subject: str = Field(min_length=1)
    field: StructuredField
    value: str
    other_party: str
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)

    @property
    def description(self) -> str:
        return f"{self.subject}: {self.field} = {self.value}" + (f" ({self.other_party})" if self.other_party else "")

    def citation(self) -> CitedFact:
        return CitedFact(
            description=self.description,
            view_id=self.view_id,
            timestamp_seconds=self.timestamp_seconds,
            confidence=self.confidence,
        )

    @model_validator(mode="after")
    def _allowed_value(self) -> Self:
        if self.field == "posted_speed_limit_kmh":
            valid = self.value in {"unknown", "not_visible"} or bool(re.fullmatch(r"[0-9]+", self.value))
        else:
            valid = self.value in FIELD_VALUES[self.field]
        if not valid:
            raise ValueError(f"Invalid value for {self.field}: {self.value!r}")
        if (self.field in SCENE_FIELDS) != (self.subject == "scene"):
            raise ValueError("Scene fields require scene; party fields require a party")
        if self.field == "relative_position_of_other":
            if not self.other_party or self.other_party == self.subject:
                raise ValueError("Pair field needs a different other_party")
        elif self.other_party:
            raise ValueError("Only pair fields may have other_party")
        return self


class TextReading(BaseModel):
    """Verbatim visible text, with camera claims explicitly unverified."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    kind: Literal["signboard", "road_name", "overlay_date", "overlay_time", "overlay_speed", "overlay_gps", "other"]
    text: str = Field(min_length=1)
    language: Literal["ar", "en", "other", "unknown"]
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _legible(self) -> Self:
        if not self.text.strip():
            raise ValueError("Text reading must not be blank")
        return self

    @property
    def description(self) -> str:
        label = "camera overlay (unverified)" if self.kind.startswith("overlay_") else self.kind
        return f"{label}: {self.text}"

    def citation(self) -> CitedFact:
        return CitedFact(
            description=self.description,
            view_id=self.view_id,
            timestamp_seconds=self.timestamp_seconds,
            confidence=self.confidence,
        )


# Observation vocabulary: RPT-30 card, PROFESSOR_REQUIREMENTS.md L17;
# seatbelt observations use the existing fines-table seatbelt row.
DriverObservationKind = Literal[
    "phone_in_hand",
    "phone_on_mount",
    "looking_at_phone",
    "tapping_phone",
    "smiling_at_phone",
    "seatbelt_worn",
    "seatbelt_not_worn",
    "seatbelt_not_visible",
]


class DriverObservation(BaseModel):
    """Visible interior observation over one view's own clock; no model verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)
    kind: DriverObservationKind
    subject: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    view_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _interval(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("Observation ends before it starts")
        return self

    @property
    def description(self) -> str:
        return f"{self.subject}: {self.kind}, {self.start_seconds:g}–{self.end_seconds:g}s ({self.end_seconds - self.start_seconds:g}s)"

    def citation(self, *, end: bool = False) -> CitedFact:
        return CitedFact(
            description=self.description,
            view_id=self.view_id,
            timestamp_seconds=self.end_seconds if end else self.start_seconds,
            confidence=self.confidence,
        )


# Collision observations (RPT-25). The model reports what touched what and what is visibly wrong with a
# person; code derives the official Accident Type and Accident Outcome (ADR 0007, rules/collisions.py).
# Kind vocabulary: GLOSSARY.md "Accident classification" (physical forms and outcome classes) in plain
# observable words; no severity scale (ADR 0004).
CollisionObservationKind = Literal[
    "vehicle_contact_vehicle",
    "vehicle_struck_pedestrian",
    "vehicle_overturned",
    "contact_other",
    "person_injured_visible",
    "person_dead_visible",
    "no_injury_visible",
    "vehicle_left_scene",
    "vehicle_stopped_at_scene",
    "vehicle_driven_away",
]
# Injury Grade terms are the three official grades (GLOSSARY.md Injury Grade); unknown is always allowed.
InjuryGradeValue = Literal["serious", "moderate", "minor", "unknown"]
ContactVisibility = Literal["visible", "possible_obscured"]
# overview-11 (spec 003 ticket 8): which video part a contact or contact interval was seen in. The model's times
# are on that part's clock; code rebases detail_window times onto the full clip once (ai.overview11).
ContactPart = Literal["whole_clip", "detail_window"]
_PAIR_KINDS = frozenset(("vehicle_contact_vehicle", "vehicle_struck_pedestrian"))
# The four kinds that are a contact; every other kind describes what happened after one (spec 003 1b).
CONTACT_KINDS = frozenset(
    ("vehicle_contact_vehicle", "vehicle_struck_pedestrian", "vehicle_overturned", "contact_other")
)


class CollisionObservation(BaseModel):
    # overview-6: vehicle_left_scene means continuously seen leaving after contact
    # without stopping; vehicle_stopped_at_scene records the contradictory sight.
    """One visible collision-related observation, cited to a view and moment; never an official label."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    kind: CollisionObservationKind
    subject: str = Field(min_length=1)
    other_party: str
    injury_grade: InjuryGradeValue
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    # overview-10 only (spec 003 section 1b): whether the touch itself is seen. None = not asked for.
    visibility: ContactVisibility | None = None
    # overview-11 only: the part the model saw it in and its time on that part's clock; timestamp_seconds is then the
    # full-clip time code computed. None = not asked for (records before overview-11).
    source_part: ContactPart | None = None  # default-ok: records before overview-11 have no video part
    part_time_seconds: float | None = Field(default=None, ge=0)  # default-ok: records before overview-11
    # overview-11 only: the sampling rate (frames per second) of the video part it was seen in, set by code from the
    # request's part plan, never by the model. None = unknown, which never counts as close-up (ai.dense).
    part_fps: float | None = Field(default=None, gt=0)  # default-ok: records before overview-11 have no part rate

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if is_raw_id(self.subject):
            raise ValueError("Collision observation subject must be a descriptive party, not a raw ID")
        if self.kind in _PAIR_KINDS:
            if not self.other_party.strip() or self.other_party == self.subject or is_raw_id(self.other_party):
                raise ValueError("This kind needs a different, descriptive other_party")
        elif self.kind == "contact_other":
            if not self.other_party.strip() or is_raw_id(self.other_party):
                raise ValueError("contact_other needs a plain-words description of what was touched")
        elif self.other_party:
            raise ValueError("Only contact kinds may have other_party")
        if self.injury_grade != "unknown" and self.kind != "person_injured_visible":
            raise ValueError("injury_grade is only for person_injured_visible")
        return self

    @property
    def description(self) -> str:
        text = f"{self.subject}: {self.kind.replace('_', ' ')}"
        if self.other_party:
            text += f" ({self.other_party})"
        if self.kind == "person_injured_visible":
            text += f", grade {self.injury_grade if self.injury_grade != 'unknown' else 'not visible'}"
        return text

    def citation(self) -> CitedFact:
        return CitedFact(
            description=self.description,
            view_id=self.view_id,
            timestamp_seconds=self.timestamp_seconds,
            confidence=self.confidence,
        )


# RPT-32 vocabulary: COMPARABLE_SYSTEMS.md section 1 and the approved ticket.
class DrowsinessObservation(BaseModel):
    """A visible cue, with nullable endpoints when the interval is not fully seen."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)
    kind: Literal["eye_closure", "yawning", "head_nodding", "eye_rubbing", "blank_stare", "lane_drift"]
    subject: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    start_seconds: float | None = Field(ge=0)
    end_seconds: float | None = Field(ge=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _interval(self) -> Self:
        if self.start_seconds is not None and self.start_seconds > self.timestamp_seconds:
            raise ValueError("Observation precedes its start")
        if self.end_seconds is not None and self.end_seconds < self.timestamp_seconds:
            raise ValueError("Observation follows its end")
        return self

    def citations(self) -> tuple[CitedFact, ...]:
        times = dict.fromkeys(
            t for t in (self.start_seconds, self.timestamp_seconds, self.end_seconds) if t is not None
        )
        return tuple(
            CitedFact(
                description=f"{self.subject}: {self.kind}",
                view_id=self.view_id,
                timestamp_seconds=t,
                confidence=self.confidence,
            )
            for t in times
        )


# overview-10 typed party checklist and incident assessment (spec 003 ticket 3, SCHEMA-CHECKLIST card).
# One source of truth: these Literal types feed both the Pydantic models and the Gemini response schema.
ChecklistPartyType = Literal[
    "sedan",
    "suv",
    "hatchback",
    "car_body_unclear",
    "van",
    "pickup",
    "truck",
    "bus",
    "motorcycle",
    "bicycle",
    "pedestrian",
    "other",
    "unknown",
]
ChecklistMarking = Literal["none_seen", "taxi", "emergency_or_official", "unknown"]
ColourFamily = Literal[
    "white",
    "silver",
    "grey",
    "black",
    "red",
    "blue",
    "green",
    "yellow_or_gold",
    "orange",
    "brown_or_beige",
    "other",
    "unknown",
]
ChecklistManoeuvre = Literal[
    "continue_same_road",
    "into_side_road",
    "out_of_side_road",
    "moving_off_from_kerb",
    "merging",
    "roundabout_entry",
    "circulating",
    "roundabout_exit",
    "lane_change",
    "u_turn",
    "crossing_on_foot",
    "other",
    "unknown",
]
TurnDirection = Literal["left", "right", "straight", "other", "unknown"]
RoundaboutExitPosition = Literal[
    "on_exit_curve_before_straight_lane", "reached_straight_lane_of_exit_road", "not_applicable", "unknown"
]
RoadClassOnApproach = Literal["main_road", "side_road", "service_road", "roundabout", "car_park_or_access", "unknown"]
RoadClassBasis = Literal[
    "more_than_two_lanes_and_long",
    "two_lanes_or_fewer",
    "short_or_ends_at_junction",
    "service_road_beside_main_road",
    "access_or_car_park",
    "roundabout_carriageway",
    "other",
    "unknown",
]
CameraRole = Literal[
    "fixed_camera", "witness_vehicle_not_involved", "camera_vehicle_involved", "interior_view", "unknown"
]
ContactState = Literal["contact_observed", "contact_possible_obscured", "no_contact_observed", "cannot_assess"]

# Checklist codes: P1, P2, ... (consecutive from P1) and at most one CAM per view.
PARTY_CODE_PATTERN = re.compile(r"(?:CAM|P[1-9][0-9]*)")
CAMERA_CODE = "CAM"
# The four fields code derives from the checklist; the overview-10 model never returns them.
DERIVED_STRUCTURED_FIELDS = frozenset(("party_type", "movement", "roundabout_entry_state", "entry_origin"))


class PartyChecklistRow(BaseModel):
    """One road user as the model returns it (all fields required, in the schema's order)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    party_code: str
    first_seen_seconds: float = Field(ge=0)
    attributes_seen_at_seconds: float = Field(ge=0)
    movement_seen_at_seconds: float = Field(ge=0)
    party_type: ChecklistPartyType
    party_type_other: str
    marking: ChecklistMarking
    colour_family: ColourFamily
    colour_other: str
    manoeuvre: ChecklistManoeuvre
    manoeuvre_other: str
    turn_direction: TurnDirection
    roundabout_exit_position: RoundaboutExitPosition
    road_class_on_approach: RoadClassOnApproach
    road_class_basis: RoadClassBasis
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not PARTY_CODE_PATTERN.fullmatch(self.party_code):
            raise ValueError(f"party_code {self.party_code!r} must be P<n> or CAM")
        for value, text, name in (
            (self.party_type, self.party_type_other, "party_type_other"),
            (self.colour_family, self.colour_other, "colour_other"),
            (self.manoeuvre, self.manoeuvre_other, "manoeuvre_other"),
        ):
            if (value == "other") != bool(text.strip()):
                raise ValueError(f"{name} must be non-empty if and only if the value is other")
        return self


class ChecklistParty(PartyChecklistRow):
    """A stored checklist row: the model's answers plus the view and the display name code built."""

    view_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class ObservationNoteRow(BaseModel):
    """A short cited model observation about one road user (or the scene); never a finding."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    party_code: str
    timestamp_seconds: float = Field(ge=0)
    note: str = Field(min_length=1, max_length=300)


class ObservationNote(ObservationNoteRow):
    """A stored note. Codes in the text were replaced by display names, so the 300 limit no longer applies."""

    note: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)  # the display name, or scene


class IncidentAssessmentRow(BaseModel):
    """Once per view: what the camera is and whether a touch was seen (spec 003 section 1b)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    assessment_note: str = Field(min_length=1, max_length=200)
    camera_role: CameraRole
    contact_state: ContactState
    contact_interval_start_seconds: float | None = Field(ge=0)
    contact_interval_end_seconds: float | None = Field(ge=0)

    @model_validator(mode="after")
    def _interval(self) -> Self:
        start, end = self.contact_interval_start_seconds, self.contact_interval_end_seconds
        has_contact = self.contact_state in {"contact_observed", "contact_possible_obscured"}
        if has_contact != (start is not None and end is not None) or (start is None) != (end is None):
            raise ValueError(
                "The contact interval is null if and only if no contact was observed or it cannot be assessed"
            )
        if start is not None and end is not None and end < start:
            raise ValueError("The contact interval ends before it starts")
        return self


# overview-11 recovery (spec 003 ticket 8): why a second, higher-detail request ran for a view and how it ended.
RecoveryTrigger = Literal["negative_state", "no_typed_contact", "contact_outside_detail_window"]
RecoveryOutcome = Literal["agreed_no_contact", "replaced", "failed"]


# Project assumption, to be validated (ADR 0006): a contact further than this from the detail window was seen only at
# the whole-clip rate, so it gets a second look; if that fails its parties are Cannot Determine.
DETAIL_WINDOW_MARGIN_SECONDS = 1.0


def outside_detail_window(seconds: float, window: tuple[float, float]) -> bool:
    return not window[0] - DETAIL_WINDOW_MARGIN_SECONDS <= seconds <= window[1] + DETAIL_WINDOW_MARGIN_SECONDS


class IncidentAssessmentRow11(IncidentAssessmentRow):
    """overview-11: the model's assessment plus the part its interval times are on. Kept apart so the overview-10 row
    (and its schema order) stay as they were; the null-iff rule for the part is checked where the answer is built
    (ai.overview11)."""

    contact_interval_part: ContactPart | None = None  # default-ok: overview-10 answers have no video part


class IncidentAssessment(IncidentAssessmentRow):
    """A stored assessment, one per view."""

    assessment_note: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    # overview-11 only. The part the interval times are on, the detail window sent with the answer kept here (full-clip seconds), and the recovery record.
    contact_interval_part: ContactPart | None = None  # default-ok: records before overview-11 have no video part
    detail_window_start_seconds: float | None = None  # default-ok: records before overview-11 sent no detail window
    detail_window_end_seconds: float | None = None  # default-ok: records before overview-11 sent no detail window
    recovery_triggers: list[RecoveryTrigger] = Field(
        default_factory=list
    )  # default-ok: records before overview-11 ran no recovery
    recovery: RecoveryOutcome | None = None  # default-ok: None = no recovery ran (records before overview-11)


class OverviewFacts(BaseModel):
    """Full typed facts extracted per incident across all views (RPT-11 contract)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    parties: list[PartyFact]
    unmatched_dense_parties: list[PartyFact] = Field(
        default_factory=list
    )  # default-ok: legacy facts predate dense-only party evidence tracking
    events: list[CitedFact]
    candidate_collisions: list[CitedFact]
    behaviour_observations: list[CitedFact]
    location_conditions: list[CitedFact]
    structured_facts: list[StructuredFact] = Field(
        default_factory=list
    )  # default-ok: legacy overview-1 has no structured facts; overview-2 requires them below

    text_readings: list[TextReading] = Field(
        default_factory=list
    )  # default-ok: legacy records predate overview-3; required in version context

    driver_observations: list[DriverObservation] = Field(
        default_factory=list
    )  # default-ok: records before overview-4 contain no driver observations

    collision_observations: list[CollisionObservation] = Field(
        default_factory=list
    )  # default-ok: records before overview-5 contain no collision observations

    drowsiness_observations: list[DrowsinessObservation] = Field(
        default_factory=list
    )  # default-ok: records before overview-7 contain no drowsiness observations

    party_checklist: list[ChecklistParty] = Field(
        default_factory=list
    )  # default-ok: records before overview-10 have no party checklist

    observation_notes: list[ObservationNote] = Field(
        default_factory=list
    )  # default-ok: records before overview-10 have no observation notes

    incident_assessments: list[IncidentAssessment] = Field(
        default_factory=list
    )  # default-ok: records before overview-10 have no incident assessment

    @model_validator(mode="before")
    @classmethod
    def _legacy_overview(cls, data: Any, info: ValidationInfo) -> Any:
        if (
            isinstance(data, dict)
            and info.context
            and info.context.get("prompt_version")
            in {"overview-7", "overview-8", "overview-9", "overview-10", "overview-11"}
        ):
            if "drowsiness_observations" not in data:
                raise ValueError("overview-7 requires drowsiness_observations")
        if (
            isinstance(data, dict)
            and info.context
            and info.context.get("prompt_version")
            in {"overview-5", "overview-6", "overview-7", "overview-8", "overview-9", "overview-10", "overview-11"}
        ):
            if "collision_observations" not in data:
                raise ValueError("overview-5 requires collision_observations")
        if (
            isinstance(data, dict)
            and info.context
            and info.context.get("prompt_version")
            in {
                "overview-4",
                "overview-5",
                "overview-6",
                "overview-7",
                "overview-8",
                "overview-9",
                "overview-10",
                "overview-11",
            }
        ):
            if "driver_observations" not in data:
                raise ValueError("overview-4 requires driver_observations")
        if (
            isinstance(data, dict)
            and info.context
            and info.context.get("prompt_version")
            in {
                "overview-3",
                "overview-4",
                "overview-5",
                "overview-6",
                "overview-7",
                "overview-8",
                "overview-9",
                "overview-10",
                "overview-11",
            }
        ):
            if "text_readings" not in data:
                raise ValueError("overview-3 requires text_readings")
        if (
            isinstance(data, dict)
            and info.context
            and info.context.get("prompt_version")
            in {
                "overview-2",
                "overview-3",
                "overview-4",
                "overview-5",
                "overview-6",
                "overview-7",
                "overview-8",
                "overview-9",
                "overview-10",
                "overview-11",
            }
            and "structured_facts" not in data
        ):
            raise ValueError("overview-2 requires structured_facts")
        # overview-1/dense-1 persisted records predate these required fields.
        # An overview-2 parser supplies explicit context, so omissions still fail.
        if (
            isinstance(data, dict)
            and "structured_facts" not in data
            and not (
                info.context
                and info.context.get("prompt_version")
                in {
                    "overview-2",
                    "overview-3",
                    "overview-4",
                    "overview-5",
                    "overview-6",
                    "overview-7",
                    "overview-8",
                    "overview-9",
                    "overview-10",
                    "overview-11",
                }
            )
        ):
            data = {"behaviour_observations": [], "location_conditions": [], "structured_facts": [], **data}
        return data

    @model_validator(mode="after")
    def _subjects_exist(self) -> Self:
        names = {p.description for p in self.parties}
        for observation in getattr(self, "drowsiness_observations", ()):
            if observation.subject not in names or is_raw_id(observation.subject):
                raise ValueError("Drowsiness observation subject must match a descriptive party")
        for observation in getattr(self, "driver_observations", ()):
            if observation.subject not in names or is_raw_id(observation.subject):
                raise ValueError("Driver observation subject must match a descriptive party")
        for collision_obs in getattr(self, "collision_observations", ()):
            if collision_obs.subject not in names:
                raise ValueError("Collision observation subject must match a descriptive party")
            if collision_obs.kind in _PAIR_KINDS and collision_obs.other_party not in names:
                raise ValueError("Collision observation other_party must match a descriptive party")
        for fact in self.structured_facts:
            if fact.subject != "scene" and fact.subject not in names:
                raise ValueError("Structured subject does not match a party")
            if fact.other_party and fact.other_party not in names:
                raise ValueError("Structured other_party does not match a party")
        return self

    @property
    def collision_candidates(self) -> list[CitedFact]:
        """Alias matching RPT-11 card vocabulary ('collision candidates')."""
        return self.candidate_collisions

    @property
    def location_and_conditions(self) -> list[CitedFact]:
        """Alias matching RPT-11 prompt vocabulary ('location/conditions')."""
        return self.location_conditions


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of L0 citation validation."""

    is_valid: bool
    errors: tuple[str, ...] = ()  # default-ok: validator output paired with is_valid, not evidence


def validate_citations(
    facts: OverviewFacts,
    views: Sequence[Any] | Collection[str],
    *,
    view_durations: Mapping[str, float] | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> ValidationResult:
    """L0 citation validator (ADR 0005, ADR 0006).

    Rejects:
    - Out-of-range timestamps (negative or exceeding view duration)
    - Unknown views (view_id not in known views)
    - Uncited facts (missing description, view_id, or non-finite timestamp)
    - Party descriptions using raw IDs
    - Low confidence (below min_confidence, labelled project assumption)
    """
    errors: list[str] = []

    # Extract known view IDs and durations
    known_view_ids: set[str] = set()
    durations: dict[str, float] = dict(view_durations or {})

    for v in views:
        if isinstance(v, str):
            known_view_ids.add(v)
        elif hasattr(v, "sha256"):
            known_view_ids.add(v.sha256)
            if hasattr(v, "duration_seconds") and v.duration_seconds is not None:
                durations.setdefault(v.sha256, float(v.duration_seconds))

    groups: list[tuple[str, Sequence[CitedFact]]] = [
        ("party", facts.parties),
        ("unmatched_dense_party", getattr(facts, "unmatched_dense_parties", ())),
        ("event", facts.events),
        ("candidate_collision", facts.candidate_collisions),
        ("behaviour_observation", facts.behaviour_observations),
        ("location_condition", facts.location_conditions),
    ]

    groups.append(("text_reading", [f.citation() for f in facts.text_readings]))
    groups.append(("structured_fact", [f.citation() for f in facts.structured_facts]))
    interior_ids = {
        v.sha256
        for v in views
        if not isinstance(v, str) and hasattr(v, "role") and getattr(v.role, "value", v.role) == "interior"
    }
    groups.append(
        (
            "driver_observation",
            [c for f in getattr(facts, "driver_observations", ()) for c in (f.citation(), f.citation(end=True))],
        )
    )
    drowsiness_observations: Sequence[DrowsinessObservation] = getattr(facts, "drowsiness_observations", ())
    groups.append(("drowsiness_observation", [c for o in drowsiness_observations for c in o.citations()]))
    exterior_ids = {
        v.sha256
        for v in views
        if not isinstance(v, str) and hasattr(v, "role") and getattr(v.role, "value", v.role) == "exterior"
    }
    for drowsiness in drowsiness_observations:
        allowed = exterior_ids if drowsiness.kind == "lane_drift" else interior_ids
        if drowsiness.view_id not in allowed:
            errors.append("drowsiness_observation: requires exterior lane drift or an interior driver cue")
    collision_observations: Sequence[CollisionObservation] = getattr(facts, "collision_observations", ())
    groups.append(("collision_observation", [f.citation() for f in collision_observations]))
    collision_views = {c.view_id for c in facts.candidate_collisions}
    # overview-10: code builds the candidates from the typed contacts, so a view with an incident assessment
    # needs no separate candidate (a stray aftermath item is a consistency issue instead, spec 003 1b).
    assessed_views = {a.view_id for a in getattr(facts, "incident_assessments", ())}
    for collision_obs in collision_observations:
        if collision_obs.view_id not in collision_views and collision_obs.view_id not in assessed_views:
            errors.append(
                f"collision_observation: no candidate collision on the cited view for {collision_obs.description!r}"
            )
    for observation in getattr(facts, "driver_observations", ()):
        if observation.view_id not in interior_ids:
            errors.append("driver_observation: requires a known interior view")
    for reading in facts.text_readings:
        if reading.view_id in interior_ids and reading.kind in {"signboard", "road_name"}:
            errors.append(f"text_reading: interior view cannot provide road text: {reading.description}")
    for structured in facts.structured_facts:
        if structured.view_id in interior_ids:
            errors.append(f"structured_fact: interior view cannot provide road facts: {structured.description}")

    for group_name, fact_list in groups:
        for idx, fact in enumerate(fact_list):
            prefix = f"{group_name}[{idx}]"
            # 1. Empty description check
            if not fact.description or not fact.description.strip():
                errors.append(f"{prefix}: fact description is empty")

            # 2. View citation check
            if not fact.view_id or not fact.view_id.strip():
                errors.append(f"{prefix}: missing view_id citation for {fact.description!r}")
            elif known_view_ids and fact.view_id not in known_view_ids:
                errors.append(f"{prefix}: cited view_id {fact.view_id!r} not in known views ({sorted(known_view_ids)})")

            # 3. Timestamp range check
            if fact.timestamp_seconds is None or fact.timestamp_seconds < 0:
                errors.append(
                    f"{prefix}: out-of-range timestamp {fact.timestamp_seconds}s (cannot be negative) for {fact.description!r}"
                )
            elif fact.view_id in durations:
                max_duration = durations[fact.view_id]
                if fact.timestamp_seconds > max_duration:
                    errors.append(
                        f"{prefix}: out-of-range timestamp {fact.timestamp_seconds}s exceeds view duration {max_duration}s for {fact.description!r}"
                    )

            # 4. Confidence check
            if fact.confidence is None or fact.confidence < min_confidence:
                conf_val = fact.confidence if fact.confidence is not None else 0.0
                errors.append(
                    f"{prefix}: confidence {conf_val:.2f} is below minimum threshold {min_confidence:.2f} for {fact.description!r}"
                )

            # 5. Raw ID check for parties
            if group_name == "party" and is_raw_id(fact.description):
                errors.append(
                    f"{prefix}: party description {fact.description!r} is a raw ID; must use colour + make/model + body type"
                )

    return ValidationResult(is_valid=len(errors) == 0, errors=tuple(errors))


# Deliberately small supported OpenAPI subset; local validation is stricter.
# Gemini structured output (https://ai.google.dev/gemini-api/docs/structured-output, page dated 2026-10-13):
# the docs state only that the schema is a supported subset of JSON Schema and that very large or deeply
# nested schemas may be rejected. The three rules below are stricter, conservative PROJECT rules kept from
# earlier Gemini tuning (the docs page does not itself forbid them), enforced by tests/test_facts_schema.py:
# - Legacy prose items have no enums; overview-2 adds only the small field-name enum (RPT-55)
# - No maxItems on nested arrays
# - Flat schema structure
_CITED_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "description": {"type": "STRING"},
        "timestamp_seconds": {"type": "NUMBER"},
        "confidence": {"type": "NUMBER"},
    },
    # No view_id: the code attaches the id of the view it sent (ai.gemini.parse_overview). A model asked to
    # copy a 64-hex fingerprint can mis-copy one character and invalidate a report (RPT-07 review).
    "required": ["description", "timestamp_seconds", "confidence"],
}

OVERVIEW_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        name: {"type": "ARRAY", "items": _CITED_SCHEMA}
        for name in (
            "parties",
            "events",
            "candidate_collisions",
            "behaviour_observations",
            "location_conditions",
        )
    },
    "required": ["parties", "events", "candidate_collisions"],
}

# Preserve the exact overview-1 request schema for persisted request hashes.
LEGACY_OVERVIEW_SCHEMA = OVERVIEW_SCHEMA
OVERVIEW_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **LEGACY_OVERVIEW_SCHEMA["properties"],
        "structured_facts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "subject": {"type": "STRING"},
                    "field": {"type": "STRING", "enum": list(get_args(StructuredField))},
                    "value": {"type": "STRING"},
                    "other_party": {"type": "STRING"},
                    "timestamp_seconds": {"type": "NUMBER"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["subject", "field", "value", "other_party", "timestamp_seconds", "confidence"],
            },
        },
    },
    "required": [*LEGACY_OVERVIEW_SCHEMA["properties"], "structured_facts"],
}


# Keep overview-2 and its request hashes byte-for-byte stable.

OVERVIEW_3_SCHEMA = deepcopy(OVERVIEW_SCHEMA)
OVERVIEW_3_SCHEMA["properties"]["text_readings"] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING"},
            "text": {"type": "STRING"},
            "language": {"type": "STRING"},
            "timestamp_seconds": {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["kind", "text", "language", "timestamp_seconds", "confidence"],
    },
}
OVERVIEW_3_SCHEMA["required"].append("text_readings")


# Deep copy preserves every previous schema and request hash.
OVERVIEW_4_SCHEMA = deepcopy(OVERVIEW_3_SCHEMA)
OVERVIEW_4_SCHEMA["properties"]["driver_observations"] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING"},
            "subject": {"type": "STRING"},
            "start_seconds": {"type": "NUMBER"},
            "end_seconds": {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["kind", "subject", "start_seconds", "end_seconds", "confidence"],
    },
}
OVERVIEW_4_SCHEMA["required"].append("driver_observations")


# Deep copy preserves every previous schema and request hash (RPT-25).
OVERVIEW_5_SCHEMA = deepcopy(OVERVIEW_4_SCHEMA)
OVERVIEW_5_SCHEMA["properties"]["collision_observations"] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING"},
            "subject": {"type": "STRING"},
            "other_party": {"type": "STRING"},
            "injury_grade": {"type": "STRING"},
            "timestamp_seconds": {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["kind", "subject", "other_party", "injury_grade", "timestamp_seconds", "confidence"],
    },
}
OVERVIEW_5_SCHEMA["required"].append("collision_observations")


# RPT-27 additive contract: flat observations, no nested array limits.
OVERVIEW_6_SCHEMA = deepcopy(OVERVIEW_5_SCHEMA)


# RPT-32 additive schema; old versions and default remain unchanged.
OVERVIEW_7_SCHEMA = deepcopy(OVERVIEW_6_SCHEMA)
OVERVIEW_7_SCHEMA["properties"]["drowsiness_observations"] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING"},
            "subject": {"type": "STRING"},
            "timestamp_seconds": {"type": "NUMBER"},
            "start_seconds": {"type": "NUMBER", "nullable": True},
            "end_seconds": {"type": "NUMBER", "nullable": True},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["kind", "subject", "timestamp_seconds", "start_seconds", "end_seconds", "confidence"],
    },
}
OVERVIEW_7_SCHEMA["required"].append("drowsiness_observations")


# overview-10 response schema (spec 003 ticket 3). Built from the same Literal types as the Pydantic models so
# the enums cannot drift. No parties array and no candidate_collisions: code builds both. Deliberately not
# derived from the older schemas, which stay byte-for-byte unchanged.
def _string(values: Sequence[str] | None = None, *, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "STRING", "enum": list(values)} if values is not None else {"type": "STRING"}
    if description is not None:
        node["description"] = description
    return node


def _item_array(properties: dict[str, dict[str, Any]], *, required: Sequence[str] | None = None) -> dict[str, Any]:
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": properties,
            "required": list(required if required is not None else properties),
            "propertyOrdering": list(properties),
        },
    }


_NUMBER: dict[str, Any] = {"type": "NUMBER"}
_NULLABLE_NUMBER: dict[str, Any] = {"type": "NUMBER", "nullable": True}
_CITED_PROPERTIES: dict[str, dict[str, Any]] = {
    "description": _string(),
    "timestamp_seconds": _NUMBER,
    "confidence": _NUMBER,
}
# The structured_facts.field enum omits the four fields code derives from the checklist (review S9: prevent).
OVERVIEW_10_STRUCTURED_FIELDS: tuple[str, ...] = tuple(
    f for f in get_args(StructuredField) if f not in DERIVED_STRUCTURED_FIELDS
)
OVERVIEW_10_TOP_LEVEL_ORDER: tuple[str, ...] = (
    "observation_notes",
    "incident_assessment",
    "party_checklist",
    "collision_observations",
    "structured_facts",
    "events",
    "behaviour_observations",
    "location_conditions",
    "text_readings",
    "driver_observations",
    "drowsiness_observations",
)
# Open-ended string codes (no enum: the number of road users is not fixed); the descriptions state the CAM policy
# and the note/checklist agreement the guard in build_overview_10_data enforces.
_NOTE_CODE_DESCRIPTION = "A party_code that is also a row of party_checklist (P1, P2, ... or CAM), or scene."
_ROW_CODE_DESCRIPTION = (
    "P1, P2, ... by first appearance; CAM for the vehicle carrying the camera (never on a fixed camera). "
    "Every code used in observation_notes must have a row."
)
OVERVIEW_10_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "observation_notes": _item_array(
            {
                "party_code": _string(description=_NOTE_CODE_DESCRIPTION),
                "timestamp_seconds": _NUMBER,
                "note": _string(),
            }
        ),
        "incident_assessment": {
            "type": "OBJECT",
            "properties": {
                "assessment_note": _string(),
                "camera_role": _string(get_args(CameraRole)),
                "contact_state": _string(get_args(ContactState)),
                "contact_interval_start_seconds": _NULLABLE_NUMBER,
                "contact_interval_end_seconds": _NULLABLE_NUMBER,
            },
            "required": [
                "assessment_note",
                "camera_role",
                "contact_state",
                "contact_interval_start_seconds",
                "contact_interval_end_seconds",
            ],
            "propertyOrdering": [
                "assessment_note",
                "camera_role",
                "contact_state",
                "contact_interval_start_seconds",
                "contact_interval_end_seconds",
            ],
        },
        "party_checklist": _item_array(
            {
                "party_code": _string(description=_ROW_CODE_DESCRIPTION),
                "first_seen_seconds": _NUMBER,
                "attributes_seen_at_seconds": _NUMBER,
                "movement_seen_at_seconds": _NUMBER,
                "party_type": _string(get_args(ChecklistPartyType)),
                "party_type_other": _string(),
                "marking": _string(get_args(ChecklistMarking)),
                "colour_family": _string(get_args(ColourFamily)),
                "colour_other": _string(),
                "manoeuvre": _string(get_args(ChecklistManoeuvre)),
                "manoeuvre_other": _string(),
                "turn_direction": _string(get_args(TurnDirection)),
                "roundabout_exit_position": _string(get_args(RoundaboutExitPosition)),
                "road_class_on_approach": _string(get_args(RoadClassOnApproach)),
                "road_class_basis": _string(get_args(RoadClassBasis)),
                "confidence": _NUMBER,
            }
        ),
        "collision_observations": _item_array(
            {
                "kind": _string(get_args(CollisionObservationKind)),
                "subject": _string(),
                "other_party": _string(),
                "injury_grade": _string(get_args(InjuryGradeValue)),
                "visibility": _string(get_args(ContactVisibility)),
                "timestamp_seconds": _NUMBER,
                "confidence": _NUMBER,
            }
        ),
        "structured_facts": _item_array(
            {
                "subject": _string(),
                "field": _string(OVERVIEW_10_STRUCTURED_FIELDS),
                "value": _string(),
                "other_party": _string(),
                "timestamp_seconds": _NUMBER,
                "confidence": _NUMBER,
            }
        ),
        "events": _item_array(_CITED_PROPERTIES),
        "behaviour_observations": _item_array(_CITED_PROPERTIES),
        "location_conditions": _item_array(_CITED_PROPERTIES),
        "text_readings": _item_array(
            {
                "kind": _string(get_args(TextReading.model_fields["kind"].annotation)),
                "text": _string(),
                "language": _string(get_args(TextReading.model_fields["language"].annotation)),
                "timestamp_seconds": _NUMBER,
                "confidence": _NUMBER,
            }
        ),
        "driver_observations": _item_array(
            {
                "kind": _string(get_args(DriverObservationKind)),
                "subject": _string(),
                "start_seconds": _NUMBER,
                "end_seconds": _NUMBER,
                "confidence": _NUMBER,
            }
        ),
        "drowsiness_observations": _item_array(
            {
                "kind": _string(get_args(DrowsinessObservation.model_fields["kind"].annotation)),
                "subject": _string(),
                "timestamp_seconds": _NUMBER,
                "start_seconds": _NULLABLE_NUMBER,
                "end_seconds": _NULLABLE_NUMBER,
                "confidence": _NUMBER,
            }
        ),
    },
    "required": list(OVERVIEW_10_TOP_LEVEL_ORDER),
    "propertyOrdering": list(OVERVIEW_10_TOP_LEVEL_ORDER),
}


# overview-11 schema (spec 003 ticket 8): the overview-10 schema plus the video part each contact and the contact
# interval were seen in, and a description on structured_facts.other_party. Built from a copy, so OVERVIEW_10_SCHEMA
# stays byte for byte as it was. The enums come from ContactPart. No maxItems, no nested arrays.
_PART_ENUM = list(get_args(ContactPart))


def _with_key_before(node: dict[str, Any], before: str, key: str, value: dict[str, Any]) -> None:
    """Insert `key` into an OBJECT node's properties, required and propertyOrdering just before `before`."""
    node["properties"] = {
        name: item
        for pair in node["properties"].items()
        for name, item in (((key, value), pair) if pair[0] == before else (pair,))
    }
    for listing in ("required", "propertyOrdering"):
        node[listing] = [name for entry in node[listing] for name in ((key, entry) if entry == before else (entry,))]


def _build_overview_11_schema() -> dict[str, Any]:
    schema = deepcopy(OVERVIEW_10_SCHEMA)
    _with_key_before(
        schema["properties"]["collision_observations"]["items"],
        "timestamp_seconds",
        "source_part",
        _string(_PART_ENUM),
    )
    _with_key_before(
        schema["properties"]["incident_assessment"],
        "contact_interval_start_seconds",
        "contact_interval_part",
        {**_string(_PART_ENUM), "nullable": True},
    )
    schema["properties"]["structured_facts"]["items"]["properties"]["other_party"]["description"] = (
        "Filled only on relative_position_of_other rows; empty on every other row."
    )
    return schema


OVERVIEW_11_SCHEMA: dict[str, Any] = _build_overview_11_schema()
