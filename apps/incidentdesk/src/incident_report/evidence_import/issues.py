"""Structured importer diagnostics with legacy-compatible message text."""

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


class Issues:
    def __init__(self) -> None:
        self.items: list[Issue] = []

    def emit(self, code: str, message: str) -> None:
        self.items.append(Issue(code, message))

    def __iter__(self) -> Iterator[str]:
        return (issue.message for issue in self.items)

    def remove_message(self, message: str) -> None:
        self.items[:] = [issue for issue in self.items if issue.message != message]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.items)
