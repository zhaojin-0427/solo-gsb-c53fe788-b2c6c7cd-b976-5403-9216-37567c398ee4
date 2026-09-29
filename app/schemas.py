"""Pydantic request models.

All quantities/amounts are declared as ``Decimal``: JSON strings (and ints)
are parsed as exact decimals so no binary float ever touches the accounting.
Model-level failures are translated into the same stable error schema used
everywhere else (see main.py exception handler).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from decimal import Decimal

AllocationMethod = Literal["mass", "energy", "economic"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialIn(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    quantity: Decimal
    unit: str
    factor_key: str
    # Optional explicit reference amount/unit when the factor's denominator
    # differs from the input unit but shares its dimension (e.g. factor per kg
    # while the input is in t). When omitted the factor unit is expected to
    # match directly.
    factor_unit: Optional[str] = None
    factor_version: Optional[str] = None


class ProductIn(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    from_process: str
    product_id: str
    quantity: Decimal
    unit: str


class ProductOut(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    product_id: str
    quantity: Decimal
    unit: str
    # Allocation weight in the allocation's native base unit:
    # mass -> kg, energy -> MJ, economic -> dimensionless currency amount.
    weight: Optional[Decimal] = None
    weight_unit: Optional[str] = None
    revenue: Optional[Decimal] = None  # convenience alias for economic weight


class Process(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: Optional[str] = None
    materials: list[MaterialIn] = Field(default_factory=list)
    inputs: list[ProductIn] = Field(default_factory=list)
    outputs: list[ProductOut] = Field(min_length=1)
    factor_version: Optional[str] = None
    allocation: Optional[AllocationMethod] = None


class Submission(StrictModel):
    id: Optional[str] = Field(default=None, max_length=128)
    name: Optional[str] = None
    default_factor_version: Optional[str] = None
    default_allocation: Optional[AllocationMethod] = None
    processes: list[Process] = Field(min_length=1)


class Factor(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    name: Optional[str] = None
    # kg CO2e per reference unit
    kg_co2e_per_unit: Decimal
    reference_unit: str
    description: Optional[str] = None


class FactorVersionCreate(StrictModel):
    version: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None


class FactorVersionClone(StrictModel):
    new_version: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None
    overrides: list[Factor] = Field(default_factory=list)


class RecomputeRequest(StrictModel):
    # Either re-point the submission default version, or override per process.
    default_factor_version: Optional[str] = None
    factor_versions: Optional[dict[str, str]] = None
