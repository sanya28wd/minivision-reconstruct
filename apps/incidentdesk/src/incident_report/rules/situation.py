"""Typed Situation: the only input the fault rules read (RPT-15 round 2, ADR 0006, ADR 0007).

The rules never read prose. Every fact a rule needs is an explicit, typed field that carries the
cited facts (view + timestamp) it was observed in, and every field may be `unknown`. A rule that
needs an `unknown` field returns Cannot Determine and names the field.

The overview-2 adapter fills the visible fields from structured evidence. Legal road class
and legacy derived decision fields remain unknown; RPT-18 owns rule logic.
"""

from __future__ import annotations

import enum
import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_report.facts import (
    ApproachControl,
    CitedFact,
    CollisionObservation,
    ConflictZoneType,
    CrossingPlace,
    EntryOrigin,
    ImpactZone,
    IncidentAssessment,
    LaneLine,
    MotionState,
    Movement,
    PartyType,
    PedestrianSignal,
    RelativePosition,
    RoadClassBasis,
    RoadClassOnApproach,
    RoundaboutEntryState,
    RoundaboutExitPosition,
    SignalGiven,
    SignalState,
    StructuredFact,
    VisibilityConditions,
    YesNoUnknown,
)

UNKNOWN = "unknown"


class Tri(enum.StrEnum):
    """A yes/no fact that may not be known."""

    YES = "yes"
    NO = "no"
    UNKNOWN = UNKNOWN


class RoadClass(enum.StrEnum):
    """Class of the road a party was travelling on at the conflict point."""

    MAIN = "main"
    SECONDARY = "secondary"
    EQUAL = "equal"
    UNKNOWN = UNKNOWN


class JunctionControl(enum.StrEnum):
    """How traffic at the conflict point was controlled."""

    UNCONTROLLED = "uncontrolled"
    SIGNAL = "signal"
    ROUNDABOUT = "roundabout"
    UNKNOWN = UNKNOWN


class PartyKind(enum.StrEnum):
    """Kind of road user (GLOSSARY.md: Party)."""

    VEHICLE = "vehicle"
    PEDESTRIAN = "pedestrian"
    CYCLIST = "cyclist"
    ANIMAL = "animal"
    UNKNOWN = UNKNOWN


class Observed[T](BaseModel):
    """A value plus the cited facts it was observed in.

    A known value must carry at least one citation (ADR 0006: no uncited finding). `unknown`
    needs none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T
    # Unknown needs no citation; _known_values_are_cited rejects a known value without one.
    evidence: tuple[CitedFact, ...] = ()  # default-ok: only unknown may be uncited (validator below)

    value_evidence: tuple[CitedFact, ...] | None = (
        None  # default-ok: direct typed inputs cite their value in evidence; adapter explicitly separates abstentions
    )

    @property
    def supporting_evidence(self) -> tuple[CitedFact, ...]:
        return self.evidence if self.value_evidence is None else self.value_evidence

    @property
    def is_known(self) -> bool:
        return self.value is not None and self.value != UNKNOWN

    @model_validator(mode="after")
    def _known_values_are_cited(self) -> Self:
        if self.is_known and not self.evidence:
            raise ValueError(f"known value {self.value!r} must cite at least one fact (view and timestamp)")
        if self.is_known and not self.supporting_evidence:
            raise ValueError("known value must have value-supporting citations")
        return self


def _unknown_road_class() -> Observed[RoadClass]:
    return Observed[RoadClass](value=RoadClass.UNKNOWN)


def _unknown_kind() -> Observed[PartyKind]:
    return Observed[PartyKind](value=PartyKind.UNKNOWN)


def _unknown_tri() -> Observed[Tri]:
    return Observed[Tri](value=Tri.UNKNOWN)


def _unknown_control() -> Observed[JunctionControl]:
    return Observed[JunctionControl](value=JunctionControl.UNKNOWN)


def _unknown_speed() -> Observed[int | None]:
    return Observed[int | None](value=None)


# Research section 5.3 P12; pair record keyed by the following party description.
LeadingVehicleAction = Literal[
    "braked_visible",
    "stopped_in_traffic",
    "stopped_at_signal",
    "changed_lane_into_path_within_window",
    "reversed",
    "none_visible",
    "unknown",
]


# Visible markings only; the engine, never the model, applies Art. 6 qualifications.
VisibleVehicleMarkings = Literal[
    "police", "ambulance", "civil_defence", "other_official_marked", "none_visible", "unknown"
]


class PartySituation(BaseModel):
    """Everything the rules need to know about one Party. Every field defaults to unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)  # colour + make/model + body type, never a raw id
    source: CitedFact | None = None  # the fact that identified this party, cited on the result
    kind: Observed[PartyKind] = Field(
        default_factory=_unknown_kind
    )  # default-ok: explicit unknown, gives Cannot Determine
    road_class: Observed[RoadClass] = Field(
        default_factory=_unknown_road_class
    )  # default-ok: explicit unknown, gives Cannot Determine
    circulating_in_roundabout: Observed[Tri] = Field(
        default_factory=_unknown_tri
    )  # default-ok: explicit unknown, gives Cannot Determine
    # True when THIS party approached from the OTHER party's left (so this party has priority on equal roads).
    approaching_from_other_party_left: Observed[Tri] = Field(
        default_factory=_unknown_tri
    )  # default-ok: explicit unknown, gives Cannot Determine
    # Emergency or official vehicle with signals active.
    emergency_signals_active: Observed[Tri] = Field(
        default_factory=_unknown_tri
    )  # default-ok: explicit unknown, gives Cannot Determine
    # Pedestrian only: at a designated crosswalk and had already started crossing.
    started_crossing_at_crosswalk: Observed[Tri] = Field(
        default_factory=_unknown_tri
    )  # default-ok: explicit unknown, gives Cannot Determine

    party_type: Observed[PartyType] = Field(
        default_factory=lambda: Observed[PartyType](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    motion_state_before_contact: Observed[MotionState] = Field(
        default_factory=lambda: Observed[MotionState](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    movement: Observed[Movement] = Field(
        default_factory=lambda: Observed[Movement](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    approach_control_faced: Observed[ApproachControl] = Field(
        default_factory=lambda: Observed[ApproachControl](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    signal_state_when_crossing_stop_line: Observed[SignalState] = Field(
        default_factory=lambda: Observed[SignalState](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    roundabout_entry_state: Observed[RoundaboutEntryState] = Field(
        default_factory=lambda: Observed[RoundaboutEntryState](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    lane_line_crossed_before_contact: Observed[LaneLine] = Field(
        default_factory=lambda: Observed[LaneLine](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    signal_given: Observed[SignalGiven] = Field(
        default_factory=lambda: Observed[SignalGiven](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    impact_zone_on_party: Observed[ImpactZone] = Field(
        default_factory=lambda: Observed[ImpactZone](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    emergency_lights: Observed[YesNoUnknown] = Field(
        default_factory=lambda: Observed[YesNoUnknown](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    emergency_siren: Observed[YesNoUnknown] = Field(
        default_factory=lambda: Observed[YesNoUnknown](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    pedestrian_crossing_place: Observed[CrossingPlace] = Field(
        default_factory=lambda: Observed[CrossingPlace](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    pedestrian_signal_state: Observed[PedestrianSignal] = Field(
        default_factory=lambda: Observed[PedestrianSignal](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    pedestrian_started_before_vehicle_arrived: Observed[YesNoUnknown] = Field(
        default_factory=lambda: Observed[YesNoUnknown](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    entry_origin: Observed[EntryOrigin] = Field(
        default_factory=lambda: Observed[EntryOrigin](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    # overview-10 checklist answers, carried for later rules (ticket 9). No rule reads them in this ticket.
    road_class_on_approach: Observed[RoadClassOnApproach] = Field(
        default_factory=lambda: Observed[RoadClassOnApproach](value="unknown")
    )  # default-ok: explicit unknown for records before overview-10, gives Cannot Determine
    road_class_basis: Observed[RoadClassBasis] = Field(
        default_factory=lambda: Observed[RoadClassBasis](value="unknown")
    )  # default-ok: explicit unknown for records before overview-10, gives Cannot Determine
    roundabout_exit_position: Observed[RoundaboutExitPosition] = Field(
        default_factory=lambda: Observed[RoundaboutExitPosition](value="unknown")
    )  # default-ok: explicit unknown for records before overview-10, gives Cannot Determine
    relative_position_of_other: dict[str, Observed[RelativePosition]] = Field(
        default_factory=dict
    )  # default-ok: missing pair gives unknown; never infer a relation

    visible_vehicle_markings: Observed[VisibleVehicleMarkings] = Field(
        default_factory=lambda: Observed[VisibleVehicleMarkings](value="unknown")
    )  # default-ok: no approved overview-2 markings extraction; no legal category inferred

    # Research section 5.3 P11. No sudden flag or duration threshold.
    lateral_move_into_other_party_path: Observed[YesNoUnknown] = Field(
        default_factory=lambda: Observed[YesNoUnknown](value="unknown")
    )  # default-ok: explicit unknown until an approved facts-contract extension
    lateral_move_start_t: Observed[float | None] = Field(
        default_factory=lambda: Observed[float | None](value=None)
    )  # default-ok: no observed move-start timestamp
    contact_t: Observed[float | None] = Field(
        default_factory=lambda: Observed[float | None](value=None)
    )  # default-ok: no observed contact timestamp
    leading_vehicle_action: dict[str, Observed[LeadingVehicleAction]] = Field(
        default_factory=dict
    )  # default-ok: absent pair has unknown leader action, never an inferred exception

    @model_validator(mode="after")
    def _valid_move_timestamps(self) -> Self:
        for observation in (self.lateral_move_start_t, self.contact_t):
            if observation.value is not None and (not math.isfinite(observation.value) or observation.value < 0):
                raise ValueError("P11 timestamps must be finite and nonnegative")
        return self


class Contradiction(BaseModel):
    """Incompatible readings of one fact, retaining each view's own clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    field: str
    other_party: str
    readings: tuple[StructuredFact, ...]


class Situation(BaseModel):
    """Input of the fault rules: the scene plus one PartySituation per Party."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parties: tuple[PartySituation, ...]
    collision_observations: tuple[CollisionObservation, ...] = ()  # default-ok: legacy facts did not ask visibility
    incident_assessments: tuple[IncidentAssessment, ...] = ()  # default-ok: legacy facts did not ask contact state
    contradictions: tuple[Contradiction, ...] = ()  # default-ok: no incompatible readings
    # Candidate collisions, each cited. Empty means no collision candidate, which gives Cannot Determine.
    collisions: tuple[CitedFact, ...] = ()  # default-ok: empty gives Cannot Determine
    junction_control: Observed[JunctionControl] = Field(
        default_factory=_unknown_control
    )  # default-ok: explicit unknown, gives Cannot Determine
    # True when the collision came from the two parties' paths converging (not rear-end, lane change and so on).
    right_of_way_conflict: Observed[Tri] = Field(
        default_factory=_unknown_tri
    )  # default-ok: explicit unknown, gives Cannot Determine
    # Posted limit where it happened.
    speed_limit_kmh: Observed[int | None] = Field(
        default_factory=_unknown_speed
    )  # default-ok: explicit unknown, gives Cannot Determine

    conflict_zone_type: Observed[ConflictZoneType] = Field(
        default_factory=lambda: Observed[ConflictZoneType](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    officer_directing_traffic: Observed[YesNoUnknown] = Field(
        default_factory=lambda: Observed[YesNoUnknown](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    posted_speed_limit_kmh: Observed[int | Literal["not_visible", "unknown"]] = Field(
        default_factory=lambda: Observed[int | Literal["not_visible", "unknown"]](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
    visibility_conditions: Observed[VisibilityConditions] = Field(
        default_factory=lambda: Observed[VisibilityConditions](value="unknown")
    )  # default-ok: explicit unknown, gives Cannot Determine
