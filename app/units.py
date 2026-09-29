"""Unit registry with deterministic, dimension-checked conversions.

Only units sharing a dimension can be converted. Conversion factors map a
unit to its canonical base unit (e.g. kWh -> J, t -> kg). Carbon results are
always expressed in kg CO2e; factors always declare emissions per base unit
of their reference dimension (kg, J, MJ, L, item, ...).
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from .errors import ErrorItem, ValidationError


class Dimension(str, Enum):
    MASS = "mass"
    ENERGY = "energy"
    VOLUME = "volume"
    COUNT = "count"


# unit -> (dimension, factor: multiply quantity by factor to reach the base unit)
_TO_BASE: dict[str, tuple[Dimension, Decimal]] = {
    # mass (base: kg)
    "kg": (Dimension.MASS, Decimal("1")),
    "g": (Dimension.MASS, Decimal("0.001")),
    "t": (Dimension.MASS, Decimal("1000")),
    "tonne": (Dimension.MASS, Decimal("1000")),
    "mg": (Dimension.MASS, Decimal("0.000001")),
    # energy (base: MJ)
    "MJ": (Dimension.ENERGY, Decimal("1")),
    "kWh": (Dimension.ENERGY, Decimal("3.6")),
    "GJ": (Dimension.ENERGY, Decimal("1000")),
    "J": (Dimension.ENERGY, Decimal("0.000001")),
    # volume (base: L)
    "L": (Dimension.VOLUME, Decimal("1")),
    "m3": (Dimension.VOLUME, Decimal("1000")),
    "mL": (Dimension.VOLUME, Decimal("0.001")),
    # count (base: item)
    "item": (Dimension.COUNT, Decimal("1")),
    "piece": (Dimension.COUNT, Decimal("1")),
    "unit": (Dimension.COUNT, Decimal("1")),
}

KNOWN = set(_TO_BASE)


def dimension_of(unit: str) -> Dimension:
    try:
        return _TO_BASE[unit][0]
    except KeyError:
        raise ValidationError([
            ErrorItem(code="UNKNOWN_UNIT", path=f".unit:{unit}",
                      message=f"unknown unit '{unit}'. Known units: "
                              f"{', '.join(sorted(KNOWN))}")
        ])


def to_base(quantity: Decimal, unit: str) -> Decimal:
    dim, factor = _TO_BASE[unit]
    return quantity * factor


def to_unit(base_quantity: Decimal, unit: str) -> Decimal:
    factor = _TO_BASE[unit][1]
    return base_quantity / factor


def ensure_same_dimension(unit_a: str, unit_b: str, *, code: str, path: str,
                          message: str | None = None) -> None:
    dim_a = dimension_of(unit_a)
    dim_b = dimension_of(unit_b)
    if dim_a is not dim_b:
        raise ValidationError([
            ErrorItem(
                code=code, path=path,
                message=message or (
                    f"unit dimension mismatch: '{unit_a}' ({dim_a.value}) vs "
                    f"'{unit_b}' ({dim_b.value}); conversion is impossible"))
        ])


def convert(quantity: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Convert quantity between two units of the same dimension."""
    ensure_same_dimension(
        from_unit, to_unit, code="UNIT_DIMENSION_MISMATCH",
        path=f".unit:{from_unit}->{to_unit}")
    return to_unit(to_base(quantity, from_unit), to_unit)
