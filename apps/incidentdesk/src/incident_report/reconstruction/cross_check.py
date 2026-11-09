"""Read-only same-view comparisons; never a source of report facts or fault inputs."""

from dataclasses import dataclass, replace

from incident_report.evidence_import.models import ImportDecision, NormalizedRecord
from incident_report.evidence_import.payloads import EventFields, TrackFields
from incident_report.facts import OverviewFacts

from .timeline import MISSING_LABELS, TimelineRow, timeline_groups


def _uncertain(row: TimelineRow, decision: ImportDecision) -> bool:
    return row.needs_review or row.key in decision.quarantined_records


def _frame_at(time: float, records: tuple[NormalizedRecord, ...], decision: ImportDecision) -> int | None:
    table = sorted(
        (r.source_time, r.source_frame)
        for r in records
        if r.kind == "source_binding"
        and r.source_time is not None
        and r.source_frame is not None
        and not r.needs_review
        and r.record_id not in decision.quarantined_records
    )
    for (start, frame), (end, _) in zip(table, table[1:], strict=False):
        if start <= time < end:
            return frame
    return table[-1][1] if table and time == table[-1][0] else None


def _party_code(name: str, facts: OverviewFacts | None, view_sha: str) -> str | None:
    parties = [p for p in facts.parties if p.view_id == view_sha and p.description == name] if facts else []
    if len(parties) != 1 or not parties[0].party_code:
        return None
    value = parties[0].party_code
    return value if facts and sum(p.view_id == view_sha and p.party_code == value for p in facts.parties) == 1 else None


CONTACT_KINDS = {"vehicle_contact_vehicle", "vehicle_struck_pedestrian", "contact_other"}


@dataclass(frozen=True)
class Comparison:
    item: str
    state: str
    reason: str
    view_id: str
    producer_time: float | None
    producer_frame: int | None
    video_times: tuple[float, ...]
    review_status: str
    difference_seconds: float | None = None


def compare_view(
    decision: ImportDecision,
    source_view: str,
    view_sha: str,
    facts: OverviewFacts | None,
    mappings: dict[str, str],
) -> tuple[Comparison, ...]:
    """Mappings must be latest, current-import reviewer mappings in this view only."""
    records = tuple(r for r in decision.records if r.view_id == source_view)
    candidates = {
        r.record_id for r in records if isinstance(r.data, EventFields) and r.data.event_type == "collision_candidate"
    }
    assessments = [a for a in facts.incident_assessments if a.view_id == view_sha] if facts else []
    assessment = assessments[0] if assessments else None
    contacts = (
        [c for c in facts.collision_observations if c.view_id == view_sha and c.kind in CONTACT_KINDS] if facts else []
    )
    times = (
        (assessment.contact_interval_start_seconds, assessment.contact_interval_end_seconds)
        if assessment
        and assessment.contact_interval_start_seconds is not None
        and assessment.contact_interval_end_seconds is not None
        else tuple(c.timestamp_seconds for c in contacts)
    )
    rows = [
        row
        for group in timeline_groups(decision, source_view, view_sha, ())
        for row in group.rows
        if row.key in candidates
    ]

    def result(row: TimelineRow | None, state: str, reason: str, delta: float | None = None) -> Comparison:
        return Comparison(
            "Contact time",
            state,
            reason,
            view_sha,
            row.source_time if row else None,
            row.source_frame if row else None,
            times,
            row.review_status if row else "No candidate supplied",
            delta,
        )

    if decision.provenance_state != "verified":
        return (result(rows[0] if rows else None, "cannot_compare", "export provenance incomplete"),)
    if decision.missing_inputs:
        return (
            result(
                None,
                "reconstruction_unavailable",
                "; ".join(MISSING_LABELS.get(code, code) for code in decision.missing_inputs),
            ),
        ) + _motion_context(decision, source_view, view_sha, facts, mappings, rows)
    uncertain_contact = bool(assessment and assessment.contact_state in {"contact_possible_obscured", "cannot_assess"})
    observed = not uncertain_contact and (
        bool(assessment and assessment.contact_state == "contact_observed") or bool(contacts)
    )
    if not rows:
        return (
            result(
                None, "tracker_miss" if observed else "cannot_compare", "No reconstruction collision candidate supplied"
            ),
        ) + _motion_context(decision, source_view, view_sha, facts, mappings, rows)
    output = []
    for row in rows:
        if row.rejected or _uncertain(row, decision) or row.source_time is None or row.source_frame is None:
            output.append(result(row, "cannot_compare", "Candidate rejected or effective source time/frame unknown"))
        elif (
            len(row.track_refs) != 2
            or any(ref not in mappings for ref in row.track_refs)
            or len({mappings.get(ref) for ref in row.track_refs}) != 2
        ):
            output.append(result(row, "cannot_compare", "Tracks need same-view reviewer mappings"))
        elif assessment and assessment.contact_state in {"contact_possible_obscured", "cannot_assess"}:
            output.append(result(row, "cannot_compare", assessment.contact_state))
        elif assessment and assessment.contact_state == "no_contact_observed":
            output.append(result(row, "model_miss", "Video analysis did not report contact"))
        elif observed and times:
            if assessment and assessment.contact_interval_start_seconds is not None:
                low, high = times
                agrees = low <= row.source_time <= high
                other_times = [c.timestamp_seconds for c in contacts if not low <= c.timestamp_seconds <= high]
                distinct_contacts = {(c.timestamp_seconds, frozenset((c.subject, c.other_party))) for c in contacts}
                if len(distinct_contacts) > 1:
                    other_times = sorted({c.timestamp_seconds for c in contacts})
                other_times.extend(
                    a.contact_interval_start_seconds
                    for a in assessments[1:]
                    if a.contact_interval_start_seconds is not None
                    and a.contact_interval_end_seconds is not None
                    and (a.contact_interval_start_seconds, a.contact_interval_end_seconds) != (low, high)
                )
                if not agrees and other_times:
                    other_contacts = [c for c in contacts if c.timestamp_seconds in other_times]
                    same_pair = False
                    if len(assessments) == 1 and len(other_contacts) == 1:
                        other = other_contacts[0]
                        codes = (
                            _party_code(other.subject, facts, view_sha),
                            _party_code(other.other_party, facts, view_sha),
                        )
                        same_pair = all(codes) and set(codes) == {mappings[ref] for ref in row.track_refs}
                    reason = (
                        "video analysis contact times are inconsistent"
                        if same_pair
                        else "video analysis reports another contact at "
                        + ", ".join(f"{t:g} s" for t in other_times)
                        + "; which contact this is cannot be determined"
                    )
                    output.append(
                        replace(
                            result(
                                row,
                                "cannot_compare",
                                reason,
                            ),
                            video_times=times + tuple(other_times),
                        )
                    )
                    continue
                delta = row.source_time - (low if row.source_time < low else high)
            else:
                # Half-open intervals between consecutive saved presentation timestamps.
                # No inferred FPS, nearest-frame tolerance, or extrapolation past the table.
                frames = [_frame_at(t, records, decision) for t in times]
                if not any(frame is not None for frame in frames):
                    output.append(result(row, "cannot_compare", "Video timestamp has no saved source-frame interval"))
                    continue
                agrees = row.source_frame in frames
                delta = min((row.source_time - t for t in times), key=abs)
            output.append(
                result(
                    row,
                    "agree"
                    if agrees
                    else (
                        "disagree_time"
                        if assessment and assessment.contact_interval_start_seconds is not None
                        else "differs_no_interval"
                    ),
                    "Reconstruction peak and video analysis contact times",
                    None if agrees else delta,
                )
            )
        else:
            output.append(result(row, "cannot_compare", "No explicit video analysis contact timing"))
    if any(comparison.state == "agree" for comparison in output):
        for index, row in enumerate(rows):
            valid_candidate = (
                not row.rejected
                and not _uncertain(row, decision)
                and row.source_time is not None
                and row.source_frame is not None
                and len(row.track_refs) == 2
                and all(ref in mappings for ref in row.track_refs)
                and len({mappings.get(ref) for ref in row.track_refs}) == 2
            )
            if valid_candidate and output[index].state != "agree":
                output[index] = replace(
                    output[index],
                    state="cannot_compare",
                    reason="reconstruction reports an additional contact",
                    difference_seconds=None,
                )
    time_states = {row.key: comparison.state for row, comparison in zip(rows, output, strict=True)}
    for row in rows:
        base = result(row, "cannot_compare", "No unique matching contact or current party mapping")
        if time_states[row.key] != "agree":
            output.append(replace(base, item="Parties in contact", reason="contact time does not match"))
            continue
        if (
            assessment
            and assessment.contact_interval_start_seconds is not None
            and assessment.contact_interval_end_seconds is not None
        ):
            matching = [
                c
                for c in contacts
                if assessment.contact_interval_start_seconds
                <= c.timestamp_seconds
                <= assessment.contact_interval_end_seconds
            ]
        else:
            matching = [c for c in contacts if _frame_at(c.timestamp_seconds, records, decision) == row.source_frame]
        if len(matching) > 1:
            output.append(
                replace(
                    base,
                    item="Parties in contact",
                    reason="which contact cannot be determined",
                    video_times=tuple(c.timestamp_seconds for c in matching),
                )
            )
            continue
        pair = [mappings.get(ref) for ref in row.track_refs]
        if (
            not row.rejected
            and not _uncertain(row, decision)
            and row.source_time is not None
            and len(pair) == 2
            and all(pair)
            and len(set(pair)) == 2
            and len(matching) == 1
        ):
            contact = matching[0]

            video_pair = [
                _party_code(contact.subject, facts, view_sha),
                _party_code(contact.other_party, facts, view_sha),
            ]
            known = all(video_pair) and len(set(video_pair)) == 2
            base = replace(
                base,
                state=("agree" if set(pair) == set(video_pair) else "disagree_parties") if known else "cannot_compare",
                reason="Reconstruction: "
                + " / ".join(
                    next(
                        (p.description for p in facts.parties if p.view_id == view_sha and p.party_code == value),
                        "Party description unavailable",
                    )
                    if facts
                    else "Party description unavailable"
                    for value in pair
                )
                + f"; video analysis: {contact.subject} / {contact.other_party}",
                video_times=(contact.timestamp_seconds,),
            )
        output.append(replace(base, item="Parties in contact"))
    return tuple(output) + _motion_context(decision, source_view, view_sha, facts, mappings, rows)


MOTION = {"moving": "moving", "stopped_in_lane": "stopped", "parked_at_kerb": "stopped"}


def _motion_context(
    decision: ImportDecision,
    source_view: str,
    view_sha: str,
    facts: OverviewFacts | None,
    mappings: dict[str, str],
    candidates: list[TimelineRow],
) -> tuple[Comparison, ...]:
    if facts is None:
        return ()
    kinds = {
        r.record_id: r.data.event_type
        for r in decision.records
        if r.view_id == source_view and isinstance(r.data, EventFields)
    }
    all_movements = [
        r
        for g in timeline_groups(decision, source_view, view_sha, ())
        for r in g.rows
        if kinds.get(r.key) in {"first_observed", "last_observed", "stop", "start"} and not r.rejected
    ]
    movements = [
        r
        for g in timeline_groups(decision, source_view, view_sha, ())
        for r in g.rows
        if kinds.get(r.key) in {"first_observed", "last_observed", "stop", "start"}
        and not r.rejected
        and not _uncertain(r, decision)
        and r.source_time is not None
        and r.source_frame is not None
    ]
    contact_refs = {ref for r in candidates if not r.rejected and not _uncertain(r, decision) for ref in r.track_refs}
    video_contacts = [c for c in facts.collision_observations if c.view_id == view_sha and c.kind in CONTACT_KINDS]
    video_times = tuple(c.timestamp_seconds for c in video_contacts)
    for a in facts.incident_assessments:
        if (
            a.view_id == view_sha
            and a.contact_interval_start_seconds is not None
            and a.contact_interval_end_seconds is not None
        ):
            video_times += (a.contact_interval_start_seconds, a.contact_interval_end_seconds)
    contact_names = {n for c in video_contacts for n in (c.subject, c.other_party)}
    output = []
    for party in facts.parties:
        if party.view_id != view_sha or not party.party_code:
            continue
        if sum(p.view_id == view_sha and p.party_code == party.party_code for p in facts.parties) != 1:
            continue
        refs = [
            ref
            for ref, code in mappings.items()
            if code == party.party_code and (ref in contact_refs or party.description in contact_names)
        ]
        events = [r for r in movements if len(refs) == 1 and refs[0] in r.track_refs]
        first = [r for r in events if kinds[r.key] == "first_observed"]
        last = [r for r in events if kinds[r.key] == "last_observed"]
        contacts = [
            r
            for r in candidates
            if not r.rejected
            and not _uncertain(r, decision)
            and any(ref in r.track_refs for ref in refs)
            and r.source_time is not None
        ]
        late = next(
            (
                r
                for r in first
                if any(
                    r.source_time is not None and c.source_time is not None and r.source_time >= c.source_time
                    for c in contacts
                )
            ),
            None,
        )
        party_video_times = tuple(
            c.timestamp_seconds for c in video_contacts if party.description in {c.subject, c.other_party}
        )
        if not video_contacts and len([r for r in candidates if not r.rejected and not _uncertain(r, decision)]) == 1:
            party_video_times = video_times
        video_late = next(
            (r for r in first if r.source_time is not None and any(r.source_time >= t for t in party_video_times)), None
        )
        late = late or video_late
        missing = any("calibration" in c or "timestamp" in c for c in decision.missing_inputs)
        if refs and (late or missing):
            citation = late or (contacts[0] if contacts else None)
            output.append(
                Comparison(
                    "Pre-contact context",
                    "cannot_compare",
                    f"Reconstruction lacks pre-contact context for {party.description}"
                    + (
                        ": " + "; ".join(MISSING_LABELS.get(code, code) for code in decision.missing_inputs)
                        if missing
                        else ": first seen at or after contact"
                    ),
                    view_sha,
                    citation.source_time if citation else None,
                    citation.source_frame if citation else None,
                    party_video_times or video_times,
                    citation.review_status if citation else "Review status unavailable",
                )
            )
        for fact in facts.structured_facts:
            if (
                fact.view_id != view_sha
                or fact.subject != party.description
                or fact.field != "motion_state_before_contact"
            ):
                continue
            time = fact.timestamp_seconds
            boundaries = (
                len(first) == 1
                and len(last) == 1
                and first[0].source_time is not None
                and last[0].source_time is not None
                and first[0].source_time <= time <= last[0].source_time
            )
            transitions = [
                r
                for r in events
                if kinds[r.key] in {"stop", "start"} and r.source_time is not None and r.source_time <= time
            ]
            transitions.sort(key=lambda r: r.source_time or 0)
            cited = transitions[-1] if transitions else None
            states = {kinds[r.key] for r in transitions if cited and r.source_time == cited.source_time}
            actual = ("stopped" if kinds[cited.key] == "stop" else "moving") if cited and len(states) == 1 else None
            barrier = any(
                any(ref in r.track_refs for ref in refs)
                and (r.source_time is None or r.source_frame is None or _uncertain(r, decision))
                for r in all_movements
            )
            records = tuple(r for r in decision.records if r.view_id == source_view)
            frame = _frame_at(time, records, decision)
            observations = [
                r for r in records if isinstance(r.data, TrackFields) and len(refs) == 1 and refs[0] in r.track_refs
            ]
            observed = frame is not None and any(
                r.source_frame == frame
                and isinstance(r.data, TrackFields)
                and r.data.state in {"observed", "recovered"}
                and not r.needs_review
                and r.record_id not in decision.quarantined_records
                for r in observations
            )
            gap = (
                cited is None
                or cited.source_frame is None
                or frame is None
                or any(
                    isinstance(r.data, TrackFields)
                    and r.data.state in {"lost", "interpolated"}
                    and (
                        (r.source_frame is not None and cited.source_frame <= r.source_frame <= frame)
                        or (
                            r.source_frame is None
                            and (
                                r.source_time is None
                                or cited.source_time is None
                                or cited.source_time <= r.source_time <= time
                            )
                        )
                    )
                    for r in observations
                )
            )
            coverage = observed and not gap
            valid = (
                coverage
                and not barrier
                and boundaries
                and not decision.missing_inputs
                and len(refs) == 1
                and actual is not None
                and fact.value in MOTION
            )
            state = ("agree" if actual == MOTION.get(fact.value) else "disagree_motion") if valid else "cannot_compare"
            output.append(
                Comparison(
                    "Motion",
                    state,
                    (
                        f"track is not mapped to {party.description}"
                        if party.party_code not in mappings.values()
                        else f"reconstruction has no observation of {party.description} at this time"
                        if not coverage
                        else f"{party.description}: reconstruction {actual or 'unknown'}; video analysis {fact.value}"
                    ),
                    view_sha,
                    cited.source_time if cited else None,
                    cited.source_frame if cited else None,
                    (time,),
                    cited.review_status if cited else "Review status unavailable",
                )
            )
    return tuple(output)
