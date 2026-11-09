"""Versioned MR 178/2017 catalogue (RPT-08); no rule evaluation or database access.

FinesTable.default() loads the packaged research snapshot; from_file() validates a
replacement before returning anything. get() matches exact keys or raises KeyError.
A null AED amount means court-decided, not zero. Null points/days mean not stated
in the note, never an inferred zero. ar=None explicitly means Arabic is missing.
Provisional rows must retain that flag downstream. Referrals have no AED field.
The snapshot is not a claim that the legal schedule is current beyond its cited note.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_FILE = Path(__file__).with_name("mr178-2017.v1.json")


@dataclass(frozen=True)
class Fine:
    key: str
    en: str
    ar: str | None
    ar_source: str | None
    aed: int | None
    black_points: int | None
    impound_days: int | None
    item_number: str
    source_url: str
    status: str
    research_reference: str
    notes: str

    def __post_init__(self) -> None:
        _validate_metadata(self)
        for field in ("aed", "black_points", "impound_days"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field} must be a nonnegative integer or null")


@dataclass(frozen=True)
class Referral:
    key: str
    en: str
    ar: str | None
    ar_source: str | None
    item_number: str
    source_url: str
    status: str
    research_reference: str
    notes: str
    disposition: str

    def __post_init__(self) -> None:
        _validate_metadata(self)
        if self.disposition != "Referral to court / prosecution":
            raise ValueError("Referral disposition must be Referral to court / prosecution")


@dataclass(frozen=True)
class FinesTable:
    version: str
    fines: tuple[Fine, ...]
    referrals: tuple[Referral, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a nonempty string")
        rows: tuple[Fine | Referral, ...] = (*self.fines, *self.referrals)
        keys = [row.key for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate violation/Referral key")

    def get(self, key: str) -> Fine:
        for row in self.fines:
            if row.key == key:
                return row
        raise KeyError(key)

    @classmethod
    def from_file(cls, path: Path) -> FinesTable:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "fines", "referrals"}:
                raise ValueError("config requires version, fines and referrals")
            if not isinstance(raw["fines"], list) or not isinstance(raw["referrals"], list):
                raise ValueError("fines and referrals must be arrays")
            return cls(
                raw["version"],
                tuple(Fine(**row) for row in raw["fines"]),
                tuple(Referral(**row) for row in raw["referrals"]),
            )
        except (TypeError, KeyError) as exc:
            raise ValueError(f"Invalid fines configuration: {exc}") from exc

    @classmethod
    def default(cls) -> FinesTable:
        return cls.from_file(DEFAULT_FILE)


def _validate_metadata(row: Fine | Referral) -> None:
    for field in ("key", "en", "item_number", "source_url", "status", "research_reference", "notes"):
        value = getattr(row, field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", row.key):
        raise ValueError("key must use lower case letters, digits and underscores")
    if not re.fullmatch(r"[0-9]+[a-z]?(, [0-9]+[a-z]?)*", row.item_number):
        raise ValueError("item_number must identify schedule items or decree articles")
    url = urlsplit(row.source_url)
    if url.scheme not in {"https", "http"} or not url.netloc:
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    if row.status not in {"verified", "provisional"}:
        raise ValueError("status must be verified or provisional")
    if row.ar is None:
        if row.ar_source is not None:
            raise ValueError("ar_source must be null when Arabic is missing")
    elif (
        not isinstance(row.ar, str)
        or not row.ar.strip()
        or not isinstance(row.ar_source, str)
        or not row.ar_source.strip()
    ):
        raise ValueError("Arabic requires nonempty ar and ar_source")
