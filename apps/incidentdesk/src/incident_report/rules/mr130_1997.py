"""Research section 2.2 duties; our English renderings are unofficial translations."""

from incident_report.rules.models import Rule

_SOURCE = "MR 130/1997 (unofficial translation)"
_URL = "https://uaelegislation.gov.ae/ar/legislations/1020"

RULE_SIGNAL = Rule(
    "mr130-1997-art-25-red",
    _SOURCE,
    "MR 130/1997 Art. 25(1)",
    "Crossing on red",
    "Stop at a red signal; Item 33.",
    _URL,
)
RULE_STOP_GIVE_WAY = Rule(
    "mr130-1997-art-27-signs",
    _SOURCE,
    "MR 130/1997 Art. 1(3), 27",
    "Obey stop and give-way signs",
    "Obey approach signs and give way; Decree-Law Art. 4, Item 70.",
    _URL,
)
RULE_ENTRY = Rule(
    "mr130-1997-art-43-60-entry",
    _SOURCE,
    "MR 130/1997 Art. 43/60",
    "Enter a road only when clear",
    "Give way when entering from the side or roadside; Item 53.",
    _URL,
)
RULE_LANE_CHANGE = Rule(
    "mr130-1997-art-58-59-lane-change",
    _SOURCE,
    "MR 130/1997 Art. 58-59",
    "Unsafe lane change into another path",
    "Change lane only without danger; no fixed schedule item determined from video.",
    _URL,
)
RULE_REAR_END = Rule(
    "mr130-1997-art-50-following",
    _SOURCE,
    "MR 130/1997 Art. 50",
    "Following distance",
    "Leave distance to stop; leader exceptions remain relevant; Item 52.",
    _URL,
)

RULE_ONCOMING_LEFT_TURN = Rule(
    "mr130-1997-art-59-5-oncoming-left-turn",
    _SOURCE,
    "MR 130/1997 Art. 59(5), 59(2)",
    "Left turn across oncoming traffic",
    "Let oncoming traffic pass first; catalogue C1-C2; Item 99b mapped by owner decision 12.",
    _URL,
)

MR_RULES = (RULE_SIGNAL, RULE_STOP_GIVE_WAY, RULE_ENTRY, RULE_LANE_CHANGE, RULE_REAR_END)
RULE_ROUNDABOUT_EXIT_SIGNED = Rule(
    "mr130-1997-art-27-roundabout-exit",
    _SOURCE,
    "MR 130/1997 Art. 27, 44(1); Decree-Law 14/2024 Art. 6",
    "Yield at a signed roundabout entry to a vehicle on its exit curve",
    "Catalogue C11/C15-C17; boundary and direction are the owner's interpretation, not defined by law.",
    _URL,
)
RULE_ROUNDABOUT_EXIT = Rule(
    "mr130-1997-art-44-roundabout-exit",
    _SOURCE,
    "MR 130/1997 Art. 44(1); Decree-Law 14/2024 Art. 6",
    "Yield at a roundabout entry to a vehicle on its exit curve",
    "Catalogue C10/C15-C17; boundary and direction are the owner's interpretation, not defined by law.",
    _URL,
)
CATALOGUE_RULES = (RULE_ONCOMING_LEFT_TURN, RULE_ROUNDABOUT_EXIT, RULE_ROUNDABOUT_EXIT_SIGNED)
