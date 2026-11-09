"""Rules as data: Federal Decree-Law No. 14 of 2024 (RPT-15, ADR 0006).

Official source: https://uaelegislation.gov.ae/en/legislations/2598
Only the following rules are configured for the RPT-15 skeleton:
- Art. 6: Uncontrolled junction priority
  - Emergency and official vehicles come first
  - Main road has priority over a secondary road
  - On equal roads, priority goes to traffic from the left
  - At roundabouts, priority goes to circulating traffic from the left
- Art. 5(1)(f): Driver must stop for pedestrians crossing at a crosswalk
- Art. 7(4): Pedestrian crossing road with speed limit above 80 km/h bears liability
"""

from __future__ import annotations

from incident_report.rules.models import Rule
from incident_report.rules.mr130_1997 import CATALOGUE_RULES, MR_RULES

_SOURCE = "Federal Decree-Law No. 14 of 2024"
_URL = "https://uaelegislation.gov.ae/en/legislations/2598"

RULE_ART_6_EMERGENCY = Rule(
    rule_id="decree-law-14-2024-art-6-emergency",
    legal_source=_SOURCE,
    article="Art. 6",
    title="Emergency and official vehicle priority",
    description="Qualified emergency and official vehicles have priority in all cases, including signals.",
    source_url=_URL,
)

RULE_ART_6_MAIN_ROAD = Rule(
    rule_id="decree-law-14-2024-art-6-main-road",
    legal_source=_SOURCE,
    article="Art. 6; MR 130/1997 Art. 44(2) and Art. 43",
    title="Main road priority over secondary road at uncontrolled junctions",
    description="Traffic on the main road has priority over traffic on a secondary road at an uncontrolled junction.",
    source_url=_URL,
)

RULE_ART_6_PRIORITY_LEFT_EQUAL = Rule(
    rule_id="decree-law-14-2024-art-6-priority-left-equal",
    legal_source=_SOURCE,
    article="Art. 6",
    title="Priority to traffic from the left on equal roads at uncontrolled junctions",
    description="On equal roads at an uncontrolled junction, priority goes to traffic approaching from the left.",
    source_url=_URL,
)

RULE_ART_6_ROUNDABOUT = Rule(
    rule_id="decree-law-14-2024-art-6-roundabout",
    legal_source=_SOURCE,
    article="Art. 6",
    title="Priority to traffic from the left at roundabouts",
    description="At roundabouts, priority goes to circulating traffic approaching from the left.",
    source_url=_URL,
)

RULE_ART_5_1_F_CROSSWALK = Rule(
    rule_id="decree-law-14-2024-art-5-1-f-crosswalk",
    legal_source=_SOURCE,
    article="Art. 5(1)(f)",
    title="Driver duty to stop for pedestrians at a crosswalk",
    description="A driver must stop for pedestrians who have started crossing at a designated crosswalk.",
    source_url=_URL,
)

RULE_ART_7_4_PEDESTRIAN_SPEED = Rule(
    rule_id="decree-law-14-2024-art-7-4-pedestrian-over-80",
    legal_source=_SOURCE,
    article="Art. 7(4)",
    title="Pedestrian liability when crossing a road with speed limit above 80 km/h",
    description="A pedestrian crossing a road with a speed limit above 80 km/h bears liability.",
    source_url=_URL,
)

ALL_RULES: tuple[Rule, ...] = (
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_MAIN_ROAD,
    RULE_ART_6_PRIORITY_LEFT_EQUAL,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
    *MR_RULES,
    *CATALOGUE_RULES,
)
