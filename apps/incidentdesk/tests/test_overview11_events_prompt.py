"""REPORT-STORYLINE: the overview-11 system prompt is unchanged (owner decision 2026-10-26: no prompt change).

The storyline is built from observation notes, typed collision observations and the existing events, so no
extra output, latency or contact-finding risk is added. The hashes pin the file as it was on fresh-start.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

PROMPT = Path(__file__).resolve().parents[1] / "src/incident_report/ai/prompts/overview_11_system.md"
FINDING_CONTACTS_SHA256 = "a26b8933d32c526fbce2a9505efeea5c39b28978dd02127cca80e43bf9d95edb"
WHOLE_SYSTEM_PROMPT_SHA256 = "458f1adc40eb7fc33b766eec48d95c174db4c100efd51cac77b089d0130631f6"


def test_finding_contacts_block_is_byte_identical() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    match = re.search(r"<finding_contacts>.*?</finding_contacts>", text, re.S)
    assert match
    assert hashlib.sha256(match.group(0).encode()).hexdigest() == FINDING_CONTACTS_SHA256


def test_whole_system_prompt_is_unchanged_from_fresh_start() -> None:
    assert hashlib.sha256(PROMPT.read_bytes()).hexdigest() == WHOLE_SYSTEM_PROMPT_SHA256
