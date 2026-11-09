"""Selected collision pairs require the Report's own close-up typed contacts."""

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIResponse
from incident_report.ai.dense import CandidateWindow, DenseSettings
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, Report
from incident_report.entry import analyse_incident
from incident_report.facts import OverviewFacts
from incident_report.hashing import sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.rules.models import FaultStatus
from incident_report.runlog import RunLog
from test_collision_pair_selection import A, B, C, collision, contact, signal_facts
from video_fixtures import synthetic_video


@pytest.mark.parametrize(
    ("confirmed", "n_runs", "two_party_majority"),
    [(False, 1, False), (True, 1, False), (False, 3, False), (False, 3, True), (True, 3, True)],
)
def test_report_uses_its_dense_pair_confirmation(
    run_log: RunLog, confirmed: bool, n_runs: int, two_party_majority: bool
) -> None:
    video = synthetic_video(b"pair-guard")
    view_id = sha256_hex(video)
    initial = signal_facts((A, B, C), [collision(0.5)], [contact(A, B, 0.5)])
    data = initial.model_dump()
    for values in data.values():
        if isinstance(values, list):
            for fact in values:
                fact.update(view_id=view_id, timestamp_seconds=0.5)
    overview = OverviewFacts.model_validate(data)
    close_up = overview.model_copy(
        update={
            "collision_observations": [
                overview.collision_observations[0].model_copy(update={"subject": B, "other_party": A})
            ]
            if confirmed
            else []
        }
    )

    class TwoPass:
        count = 0

        def analyse(self, request: AIRequest) -> AIResponse:
            current = overview
            if two_party_majority and self.count > 0:
                current = overview.model_copy(update={"parties": overview.parties[:2]})
            self.count += 1
            return AIResponse(summary="Synthetic collision", provenance="synthetic", facts=current, calls=())

        def analyse_window(
            self, request: AIRequest, window: CandidateWindow
        ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            return close_up, ()

    report = analyse_incident(
        [make_view(video)],
        Origin.BENCHMARK,
        ai=TwoPass(),
        run_log=run_log,
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        agreement_settings=AgreementSettings(n_runs=n_runs),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert report.is_valid
    assert report.dense_facts == (close_up,)
    faults = {f.party_description: f for f in report.parties}
    selection = faults[A].pair_selection
    assert selection is not None
    assert selection.corroborated is confirmed
    assert faults[A].status == (FaultStatus.AT_FAULT if confirmed else FaultStatus.CANNOT_DETERMINE)
    assert faults[B].status == (FaultStatus.NOT_AT_FAULT if confirmed else FaultStatus.CANNOT_DETERMINE)
    assert faults[C].status == FaultStatus.CANNOT_DETERMINE
    assert faults[C].reason.startswith("Not in the collision pair; not assessed")
    if not confirmed:
        assert faults[A].reason == "Collision pair not confirmed by the close-up check"
    assert run_log.get(report.run_id).report == Report.model_validate_json(report.model_dump_json())
