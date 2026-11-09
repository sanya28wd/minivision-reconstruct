"""Rules engine package (RPT-15, spec 001 Seam 3, ADR 0004, ADR 0006, ADR 0007)."""

from __future__ import annotations

from incident_report.rules.adapter import situation_from_overview
from incident_report.rules.decree_law_14_2024 import (
    ALL_RULES,
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_MAIN_ROAD,
    RULE_ART_6_PRIORITY_LEFT_EQUAL,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
)
from incident_report.rules.engine import evaluate_fault, evaluate_situation
from incident_report.rules.models import (
    FAULT_STATUS_COLORS,
    FaultStatus,
    IncidentViolationsSummary,
    PartyFault,
    PartyViolation,
    PartyViolationsSummary,
    Rule,
    Violation,
    ViolationTotals,
)
from incident_report.rules.situation import (
    JunctionControl,
    Observed,
    PartyKind,
    PartySituation,
    RoadClass,
    Situation,
    Tri,
)
from incident_report.rules.violations import (
    calculate_violation_totals,
    evaluate_violations,
    map_violation,
    summarize_violations,
    violations_from_faults,
)

__all__ = [
    "ALL_RULES",
    "FAULT_STATUS_COLORS",
    "FaultStatus",
    "IncidentViolationsSummary",
    "JunctionControl",
    "Observed",
    "PartyFault",
    "PartyKind",
    "PartySituation",
    "PartyViolation",
    "PartyViolationsSummary",
    "RULE_ART_5_1_F_CROSSWALK",
    "RULE_ART_6_EMERGENCY",
    "RULE_ART_6_MAIN_ROAD",
    "RULE_ART_6_PRIORITY_LEFT_EQUAL",
    "RULE_ART_6_ROUNDABOUT",
    "RULE_ART_7_4_PEDESTRIAN_SPEED",
    "RoadClass",
    "Rule",
    "Situation",
    "Tri",
    "Violation",
    "ViolationTotals",
    "calculate_violation_totals",
    "evaluate_fault",
    "evaluate_situation",
    "evaluate_violations",
    "map_violation",
    "situation_from_overview",
    "summarize_violations",
    "violations_from_faults",
]
