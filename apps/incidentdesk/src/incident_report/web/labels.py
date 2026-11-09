"""The bilingual label catalogue (RPT-04): one data file holds every Arabic string in the UI.

ADR 0004/0006: Arabic for official terms comes from GLOSSARY.md or the spec; it is never invented.
Each entry therefore carries where its Arabic came from:
- "sourced": the Arabic is copied from the named source (e.g. GLOSSARY.md).
- "translation pending owner review": the Arabic is a draft nobody has approved. None ship today.
- "none": no official Arabic term is known yet. The label renders in English only. Do not fill the gap.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ARABIC_SOURCED = "sourced"
ARABIC_PENDING = "translation pending owner review"
ARABIC_NONE = "none"
_STATUSES = (ARABIC_SOURCED, ARABIC_PENDING, ARABIC_NONE)

_KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
DEFAULT_LABELS_FILE = Path(__file__).parent / "labels.json"


class UnknownLabelError(KeyError):
    """A template asked for a label key that is not in the catalogue. Nothing is made up instead."""


@dataclass(frozen=True)
class Label:
    key: str
    en: str
    ar: str | None
    ar_status: str
    source: str

    def __post_init__(self) -> None:
        if not _KEY.match(self.key):
            raise ValueError(f"label key {self.key!r} must look like 'group.name' in lower case")
        if not self.en.strip():
            raise ValueError(f"{self.key}: a label needs an English text.")
        if self.ar_status not in _STATUSES:
            raise ValueError(f"{self.key}: unknown Arabic status {self.ar_status!r}; use one of {_STATUSES}.")
        if self.ar_status == ARABIC_NONE and self.ar is not None:
            raise ValueError(f"{self.key}: status 'none' means no Arabic; remove the Arabic text or change the status.")
        if self.ar_status != ARABIC_NONE and not (self.ar and self.ar.strip()):
            raise ValueError(f"{self.key}: status {self.ar_status!r} needs the Arabic text.")
        if self.ar_status == ARABIC_SOURCED and not self.source.strip():
            raise ValueError(f"{self.key}: a sourced Arabic string must name its source.")


class LabelCatalog:
    def __init__(self, labels: Iterable[Label]) -> None:
        self.labels: tuple[Label, ...] = tuple(labels)
        self._by_key: dict[str, Label] = {}
        for label in self.labels:
            if label.key in self._by_key:
                raise ValueError(f"duplicate label key {label.key!r}")
            self._by_key[label.key] = label

    def get(self, key: str) -> Label:
        try:
            return self._by_key[key]
        except KeyError:
            raise UnknownLabelError(key) from None

    @classmethod
    def from_file(cls, path: Path) -> LabelCatalog:
        raw = json.loads(path.read_text(encoding="utf-8"))
        labels = [
            Label(
                key=item["key"],
                en=item["en"],
                ar=item.get("ar"),
                ar_status=item["ar_status"],
                source=item.get("source", ""),
            )
            for item in raw["labels"]
        ]
        return cls(labels)

    @classmethod
    def default(cls) -> LabelCatalog:
        return cls.from_file(DEFAULT_LABELS_FILE)
