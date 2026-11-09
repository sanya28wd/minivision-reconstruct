"""overview-10: the typed party checklist prompt, schema and model-output builder (spec 003 ticket 3).

The model returns closed checklist answers, typed contacts and short notes. Code builds the parties, the
display names, the rule-facing typed facts and the candidate collisions from them, then hands one ordinary
OverviewFacts payload to the existing validation. Prompt wording lives in the two .md files (Claude owns it);
this module only loads them, appends the allowed-values block and shapes the answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from typing import Any, get_args

from incident_report.checklist import (
    build_candidates,
    derive_party_type,
    derive_road_facts,
    display_name,
    party_body_type,
    party_color,
    rewrite_codes,
    validate_codes,
)
from incident_report.facts import (
    CAMERA_CODE,
    CONTACT_KINDS,
    DERIVED_STRUCTURED_FIELDS,
    FIELD_VALUES,
    OVERVIEW_10_SCHEMA,
    OVERVIEW_10_STRUCTURED_FIELDS,
    OVERVIEW_10_TOP_LEVEL_ORDER,
    PARTY_CODE_PATTERN,
    CollisionObservation,
    CollisionObservationKind,
    DriverObservationKind,
    DrowsinessObservation,
    IncidentAssessmentRow,
    IncidentAssessmentRow11,
    InjuryGradeValue,
    ObservationNoteRow,
    PartyChecklistRow,
    TextReading,
)

PROMPT_VERSION_10 = "overview-10"
_SYSTEM_FILE = "overview_10_system.md"
_USER_FILE = "overview_10_user.md"


def normalise_line_endings(text: str) -> str:
    """LF only, so the text sent (and the request hash) is identical on every machine."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _load(name: str) -> str:
    """Read a packaged prompt file (importlib.resources, never a working-directory path) with LF endings,
    whatever a Windows checkout did to the file."""
    return normalise_line_endings(
        resources.files("incident_report.ai").joinpath("prompts", name).read_text(encoding="utf-8")
    )


def allowed_values_block() -> str:
    """The closed values for the rows the model still writes as free STRING values, in the overview-2 form.

    structured_facts.value is a plain STRING in the schema, so its allowed values must reach the model as text.
    The four derived fields are left out: code derives them from the checklist.
    """
    lines = []
    for name in OVERVIEW_10_STRUCTURED_FIELDS:
        if name == "posted_speed_limit_kmh":
            lines.append(f"{name}: digits read from a visible sign, not_visible, unknown")
        else:
            lines.append(f"{name}: {', '.join(FIELD_VALUES[name])}")
    lines.append(f"text_readings.kind: {', '.join(get_args(TextReading.model_fields['kind'].annotation))}")
    lines.append(f"text_readings.language: {', '.join(get_args(TextReading.model_fields['language'].annotation))}")
    lines.append(f"driver_observations.kind: {', '.join(get_args(DriverObservationKind))}")
    lines.append(
        f"drowsiness_observations.kind: {', '.join(get_args(DrowsinessObservation.model_fields['kind'].annotation))}"
    )
    lines.append(f"collision_observations.kind: {', '.join(get_args(CollisionObservationKind))}")
    lines.append(f"collision_observations.injury_grade: {', '.join(get_args(InjuryGradeValue))}")
    return "<allowed_values>\n" + "\n".join(lines) + "\n</allowed_values>"


def system_instruction() -> str:
    """The system prompt file plus the generated allowed-values block (part of the hashed system text)."""
    return _load(_SYSTEM_FILE).rstrip("\n") + "\n\n" + allowed_values_block() + "\n"


def user_prompt() -> str:
    return _load(_USER_FILE)


class StructuredPrompt(str):
    """The user text of an overview-10 request, carrying what the transport must also send.

    A str, so every existing fake transport and the model-call log still see plain text. The system
    instruction and response schema are chosen by prompt version here, not guessed from the text.
    """

    system_instruction: str
    response_schema: dict[str, Any]

    def __new__(cls, text: str, *, system_instruction: str, response_schema: dict[str, Any]) -> StructuredPrompt:
        prompt = super().__new__(cls, text)
        prompt.system_instruction = system_instruction
        prompt.response_schema = response_schema
        return prompt


def build_prompt(text_suffix: str = "") -> StructuredPrompt:
    return StructuredPrompt(
        user_prompt() + text_suffix, system_instruction=system_instruction(), response_schema=OVERVIEW_10_SCHEMA
    )


# ---------------------------------------------------------------------------------------------------------
# Model output -> OverviewFacts payload
# ---------------------------------------------------------------------------------------------------------


_ECHO_LIMIT = 10  # well-formed codes named in the message; the rest are counted
_MAX_CODE_LENGTH = 6  # P99999: longer digit strings are not road-user codes


class UnlistedPartyCodes(ValueError):
    """Notes name road users the checklist does not list. Its message is safe to show the repair call: it holds
    only well-formed party codes (at most _ECHO_LIMIT of them), counts and fixed words, never model text."""


def _list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _dict(item: Any, what: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{what} must be an object")
    return item


def _unlisted_note_codes(data: Mapping[str, Any], names: Mapping[str, str]) -> UnlistedPartyCodes:
    """One error naming every note code with no checklist row, so a single repair can fix them all.
    The codes are never folded into scene and no row is invented (card section 1)."""
    codes = {
        ObservationNoteRow.model_validate(_dict(raw, "observation_notes item")).party_code
        for raw in _list(data, "observation_notes")
    }
    unknown = codes - set(names) - {"scene"}
    # Only short values that fully match the code pattern are echoed; anything else is model text and is only counted.
    wellformed = sorted(c for c in unknown if len(c) <= _MAX_CODE_LENGTH and PARTY_CODE_PATTERN.fullmatch(c))
    malformed = len(unknown) - len(wellformed)
    shown = ", ".join(wellformed[:_ECHO_LIMIT])
    if len(wellformed) > _ECHO_LIMIT:
        shown += f" and {len(wellformed) - _ECHO_LIMIT} more"
    if malformed:
        counted = f"{malformed} malformed code{'s' if malformed > 1 else ''}"
        shown = f"{shown} and {counted}" if shown else counted
    return UnlistedPartyCodes(
        f"observation_notes use party codes not in party_checklist: {shown} "
        f"(listed: {', '.join(names) or 'none'}). Add a checklist row for each, or remove those notes."
    )


def _code(names: Mapping[str, str], code: Any, what: str) -> str:
    """A party reference must be a code from this view's checklist (card section 3)."""
    if not isinstance(code, str) or code not in names:
        raise ValueError(f"{what} must be a party code from the checklist")
    return names[code]


def build_overview_10_data(
    data: Any, view_id: str, view_role: str | None, *, allow_parts: bool = False
) -> dict[str, Any]:
    """Turn the model's overview-10 answer into the payload OverviewFacts validates.

    allow_parts is for overview-11 (ai.overview11), whose answers carry the video part of each contact and of the
    interval; an overview-10 answer that carries one is refused, exactly as before the fields existed.

    Raises ValueError (or a Pydantic ValidationError) on any breach; the caller turns that into the existing
    repair path. Nothing is silently dropped or rewritten except codes to display names (ADR 0006).
    """
    if not isinstance(data, dict):
        raise ValueError("overview-10 answer must be an object")
    unexpected = set(data) - set(OVERVIEW_10_TOP_LEVEL_ORDER)
    missing = set(OVERVIEW_10_TOP_LEVEL_ORDER) - set(data)
    if unexpected or missing:
        raise ValueError(f"overview-10 keys wrong: unexpected {sorted(unexpected)}, missing {sorted(missing)}")

    rows = [PartyChecklistRow.model_validate(_dict(r, "party_checklist item")) for r in _list(data, "party_checklist")]
    validate_codes(rows)
    raw_assessment = _dict(data["incident_assessment"], "incident_assessment")
    assessment = (IncidentAssessmentRow11 if allow_parts else IncidentAssessmentRow).model_validate(raw_assessment)
    names = {row.party_code: display_name(row) for row in rows}
    exterior = view_role != "interior"

    def rewrite(text: Any) -> Any:
        return rewrite_codes(text, names) if isinstance(text, str) else text

    notes = []
    for raw in _list(data, "observation_notes"):
        note = ObservationNoteRow.model_validate(_dict(raw, "observation_notes item"))
        if note.party_code != "scene" and note.party_code not in names:
            raise _unlisted_note_codes(data, names)
        notes.append(
            {
                **note.model_dump(),
                "note": rewrite(note.note),
                "view_id": view_id,
                "subject": "scene" if note.party_code == "scene" else names[note.party_code],
            }
        )

    parties = []
    checklist = []
    for row in rows:
        name = names[row.party_code]
        parties.append(
            {
                "description": name,
                "view_id": view_id,
                "timestamp_seconds": row.first_seen_seconds,
                "confidence": row.confidence,
                "color": party_color(row),
                "body_type": party_body_type(row),
                "party_code": row.party_code,
            }
        )
        checklist.append({**row.model_dump(), "view_id": view_id, "display_name": name})

    structured: list[dict[str, Any]] = []
    for raw in _list(data, "structured_facts"):
        item = _dict(raw, "structured_facts item")
        if item.get("field") in DERIVED_STRUCTURED_FIELDS:
            raise ValueError(f"structured_facts field {item['field']!r} is derived from the checklist in overview-10")
        subject = item.get("subject")
        other = item.get("other_party")
        structured.append(
            {
                **item,
                "subject": "scene" if subject == "scene" else _code(names, subject, "structured_facts subject"),
                "other_party": "" if other == "" else _code(names, other, "structured_facts other_party"),
            }
        )
    if exterior:
        for row in rows:
            name = names[row.party_code]
            road = derive_road_facts(row)
            for field, value, seen_at in (
                ("party_type", derive_party_type(row), row.attributes_seen_at_seconds),
                ("movement", road.movement, row.movement_seen_at_seconds),
                ("roundabout_entry_state", road.roundabout_entry_state, row.movement_seen_at_seconds),
                ("entry_origin", road.entry_origin, row.movement_seen_at_seconds),
            ):
                structured.append(
                    {
                        "subject": name,
                        "field": field,
                        "value": value,
                        "other_party": "",
                        "view_id": view_id,
                        "timestamp_seconds": seen_at,
                        "confidence": row.confidence,
                    }
                )

    collision_items = []
    contacts: list[CollisionObservation] = []
    for raw in _list(data, "collision_observations"):
        item = _dict(raw, "collision_observations item")
        if not allow_parts and ("source_part" in item or "part_time_seconds" in item or "part_fps" in item):
            raise ValueError("source_part is an overview-11 field")
        kind = item.get("kind")
        other = item.get("other_party")
        if kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian"}:
            other = _code(names, other, "collision_observations other_party")
        elif kind == "contact_other":
            other = rewrite(other)  # plain words for the thing touched (review S3)
        collision = {**item, "subject": _code(names, item.get("subject"), "collision_observations subject")}
        collision["other_party"] = other
        collision["view_id"] = view_id
        collision_items.append(collision)
        contact = CollisionObservation.model_validate(collision)
        if kind in CONTACT_KINDS:
            if contact.visibility is None:
                raise ValueError("a contact needs visibility")
            contacts.append(contact)

    camera = names.get(CAMERA_CODE)
    candidates = [
        c.model_dump() for c in build_candidates(contacts, camera_name=camera, camera_role=assessment.camera_role)
    ]

    def subject_items(key: str) -> list[dict[str, Any]]:
        out = []
        for raw in _list(data, key):
            item = _dict(raw, f"{key} item")
            out.append({**item, "subject": _code(names, item.get("subject"), f"{key} subject")})
        return out

    def described(key: str) -> list[dict[str, Any]]:
        return [
            {**_dict(raw, f"{key} item"), "description": rewrite(_dict(raw, f"{key} item").get("description"))}
            for raw in _list(data, key)
        ]

    return {
        "parties": parties,
        "events": described("events"),
        "candidate_collisions": candidates,
        "behaviour_observations": described("behaviour_observations"),
        "location_conditions": _list(data, "location_conditions"),
        "structured_facts": structured,
        "text_readings": _list(data, "text_readings"),
        "driver_observations": subject_items("driver_observations"),
        "collision_observations": collision_items,
        "drowsiness_observations": subject_items("drowsiness_observations"),
        "party_checklist": checklist,
        "observation_notes": notes,
        "incident_assessments": [
            {
                **assessment.model_dump(),
                "assessment_note": rewrite(assessment.assessment_note),
                "view_id": view_id,
            }
        ],
    }


__all__ = [
    "PROMPT_VERSION_10",
    "StructuredPrompt",
    "UnlistedPartyCodes",
    "allowed_values_block",
    "build_overview_10_data",
    "build_prompt",
    "system_instruction",
    "user_prompt",
]
