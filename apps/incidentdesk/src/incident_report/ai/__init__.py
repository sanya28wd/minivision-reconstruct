"""Seam 2 (AI connection). See interface.py for the contract."""

from incident_report.ai.dense import (
    CandidateWindow,
    DensePassAIConnection,
    DenseSettings,
    calculate_overview_collision_recall,
    compute_windows,
    consolidate_facts,
)
from incident_report.ai.dense_replay import ReplayDenseAIConnection
from incident_report.ai.errors import AIConnectionError, RecordingInvalid, RecordingNotFound
from incident_report.ai.interface import AIConnection, AIRequest, AIResponse, AIViewRef
from incident_report.ai.replay import ReplayAIConnection

__all__ = [
    "AIConnection",
    "AIConnectionError",
    "AIRequest",
    "AIResponse",
    "AIViewRef",
    "CandidateWindow",
    "DensePassAIConnection",
    "DenseSettings",
    "RecordingInvalid",
    "RecordingNotFound",
    "ReplayAIConnection",
    "ReplayDenseAIConnection",
    "calculate_overview_collision_recall",
    "compute_windows",
    "consolidate_facts",
]
