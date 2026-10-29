"""Case-wide ordering of proposed movement events with explicitly unresolved order."""

from minivision_reconstruct.movement.models import MovementEvent, MovementSequence, SequenceEntry


def order_events(case_id: str, events: tuple[MovementEvent, ...]) -> MovementSequence:
    """Rank by peak time; events whose uncertainty windows overlap are not claimed to be ordered."""
    ordered = sorted(events, key=lambda event: (event.peak_time, event.event_type, event.track_ids))
    entries: list[SequenceEntry] = []
    for rank, event in enumerate(ordered, 1):
        overlapping = tuple(
            other.event_id for other in ordered
            if other.event_id != event.event_id and other.time_window[0] <= event.time_window[1] and event.time_window[0] <= other.time_window[1]
        )
        entries.append(SequenceEntry(
            rank=rank, event_id=event.event_id, event_type=event.event_type, track_ids=event.track_ids,
            peak_time=event.peak_time, time_window=event.time_window, order_unresolved_with=overlapping,
        ))
    return MovementSequence(schema_version="1.0", case_id=case_id, entries=tuple(entries))
