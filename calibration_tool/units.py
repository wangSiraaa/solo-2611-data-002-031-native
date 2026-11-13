"""Unit declaration checks for plan.json.

The plan declares one reference unit and one response unit for the whole run.
Rows carry no per-row unit, so validation is at the declaration level: each
declared unit must be recognised and must belong to the expected dimension.
Values are *not* silently rescaled; the declared units are the reporting
units, and the report echoes them.
"""
from __future__ import annotations

from .issues import Issue

# dimension -> {symbol: (name, scale_to_base)}. Base: mg/L for concentration,
# mV for response. Scale is recorded for reproducibility only.
CONCENTRATION_UNITS = {
    "mg/L": ("milligram per litre", 1.0),
    "g/L": ("gram per litre", 1000.0),
    "ug/L": ("microgram per litre", 1e-3),
    "µg/L": ("microgram per litre", 1e-3),
    "mol/L": ("mole per litre", None),
    "mmol/L": ("millimole per litre", None),
}
RESPONSE_UNITS = {
    "mV": ("millivolt", 1.0),
    "V": ("volt", 1000.0),
    "uV": ("microvolt", 1e-3),
    "µV": ("microvolt", 1e-3),
}


def validate_units(plan: dict) -> list[Issue]:
    issues: list[Issue] = []
    ref = plan.get("reference_unit")
    resp = plan.get("response_unit")

    if not isinstance(ref, str) or not ref.strip():
        issues.append(Issue("error", "unit_missing", "plan.reference_unit",
                            "reference_unit is missing or empty"))
    elif ref not in CONCENTRATION_UNITS:
        issues.append(Issue("error", "unit_illegal", "plan.reference_unit",
                            f"reference_unit {ref!r} is not a recognised concentration unit "
                            f"(allowed: {sorted(CONCENTRATION_UNITS)})"))

    if not isinstance(resp, str) or not resp.strip():
        issues.append(Issue("error", "unit_missing", "plan.response_unit",
                            "response_unit is missing or empty"))
    elif resp not in RESPONSE_UNITS:
        issues.append(Issue("error", "unit_illegal", "plan.response_unit",
                            f"response_unit {resp!r} is not a recognised response unit "
                            f"(allowed: {sorted(RESPONSE_UNITS)})"))

    if (isinstance(ref, str) and isinstance(resp, str) and ref.strip()
            and resp.strip() and ref.strip() == resp.strip()):
        issues.append(Issue("error", "unit_conflict", "plan",
                            "reference_unit and response_unit are identical; a response "
                            "cannot share the reference dimension"))

    if (isinstance(ref, str) and ref in CONCENTRATION_UNITS
            and isinstance(resp, str) and resp in RESPONSE_UNITS):
        issues.append(Issue(
            "info", "unit_ok", "plan",
            f"reference_unit={ref} ({CONCENTRATION_UNITS[ref][0]}), "
            f"response_unit={resp} ({RESPONSE_UNITS[resp][0]}); values used as declared, "
            "no rescaling"))
    return issues
