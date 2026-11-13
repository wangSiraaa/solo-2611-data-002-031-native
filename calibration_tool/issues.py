"""Issues collected during validation, parsing, fitting and application."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Issue:
    severity: str          # "error" or "warning"
    code: str              # short machine-readable code
    item: str              # named input item / row / level / sample
    message: str           # human-readable explanation
    line: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["line"] is None:
            del d["line"]
        return d


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
