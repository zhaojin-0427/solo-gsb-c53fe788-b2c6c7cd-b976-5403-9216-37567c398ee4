"""Built-in reference factor library.

v2024 ships out of the box so the service is useful immediately. It is seeded
only into an empty database; new factor data is created as a *new version*
(via POST /factor-versions/{v}/clone or a fresh version + factors), which is
what guarantees historical snapshots stay reproducible.
"""
from __future__ import annotations

from decimal import Decimal

SEED_VERSION = "v2024"
SEED_DESCRIPTION = "Built-in reference factors (illustrative values)"

# key -> (name, kg CO2e per reference unit, reference unit, description)
SEED_FACTORS: list[tuple[str, str, str, str, str]] = [
    ("electricity.grid", "Grid electricity", "0.42", "kWh",
     "Average grid electricity"),
    ("natural_gas", "Natural gas", "0.056", "MJ",
     "Natural gas combustion"),
    ("diesel", "Diesel", "2.68", "L",
     "Diesel fuel combustion"),
    ("crude_oil", "Crude oil (upstream supply)", "0.45", "kg",
     "Well-to-tank crude oil supply"),
    ("iron_ore", "Iron ore (concentrate)", "0.21", "kg",
     "Mined and beneficiated iron ore"),
    ("limestone", "Limestone", "0.032", "kg",
     "Quarried and crushed limestone"),
    ("fresh_water", "Process water", "0.0003", "L",
     "Treated process water"),
    ("steam", "Industrial steam", "0.075", "MJ",
     "Steam from natural-gas boiler"),
]
