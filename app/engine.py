"""Carbon-footprint accounting engine.

Pipeline (all in :mod:`decimal`, never float):

1. *Validate* the declared process batch graph: duplicate ids, non-positive or
   negative amounts, unknown units, missing factor versions/keys, dimension
   mismatches, zero allocation denominators and cycles all raise a single
   :class:`ValidationError` carrying a stable, sorted error list.
2. *Topologically sort* the process batches.
3. For each process compute direct emissions ``quantity_in_factor_unit *
   factor``, attach upstream product footprints scaled by transferred
   quantity share, and split the process total across co-products by the
   chosen allocation method (mass / energy / economic).
4. Emit per-product kg CO2e/unit, an exact contribution tree, a flat
   origin->sink contribution chain and evidence rows capturing the exact
   factor values used.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext
from typing import Any, Optional, Protocol

from .errors import ErrorItem, ValidationError
from . import units
from .units import Dimension, to_base, to_unit
from .schemas import Submission

ALGO_VERSION = "1.0.0"
Q9 = Decimal("0.000000001")
Q12 = Decimal("0.000000000001")


def q9(d: Decimal) -> Decimal:
    return d.quantize(Q9)


def fixed9(d: Decimal) -> str:
    return format(q9(d), "f")


def q12(d: Decimal) -> Decimal:
    return d.quantize(Q12)


def canon_decimal(d: Decimal) -> str:
    return format(d.normalize(), "f")


class FactorProvider(Protocol):
    def version_exists(self, version: str) -> bool: ...
    def get_factor(self, version: str, key: str) -> Optional[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# Resolved graph
# --------------------------------------------------------------------------- #

class RMaterial:
    __slots__ = ("id", "quantity", "unit", "factor_key", "factor_version",
                 "factor_value", "factor_unit", "factor_name", "declared_unit")

    def __init__(self, id, quantity, unit, factor_key, factor_version,
                 factor_value, factor_unit, factor_name, declared_unit):
        self.id = id
        self.quantity = quantity
        self.unit = unit
        self.factor_key = factor_key
        self.factor_version = factor_version
        self.factor_value = factor_value  # Decimal kg CO2e / factor base unit
        self.factor_unit = factor_unit
        self.factor_name = factor_name
        self.declared_unit = declared_unit


class RInput:
    __slots__ = ("id", "from_process", "product_id", "quantity", "unit",
                 "edge_share")

    def __init__(self, id, from_process, product_id, quantity, unit):
        self.id = id
        self.from_process = from_process
        self.product_id = product_id
        self.quantity = quantity
        self.unit = unit
        self.edge_share: Decimal = Decimal(0)


class ROutput:
    __slots__ = ("id", "product_id", "quantity", "unit", "weight",
                 "weight_label", "share")

    def __init__(self, id, product_id, quantity, unit, weight, weight_label):
        self.id = id
        self.product_id = product_id
        self.quantity = quantity
        self.unit = unit
        self.weight = weight          # Decimal in allocation base unit
        self.weight_label = weight_label
        self.share: Decimal = Decimal(0)


class RProcess:
    __slots__ = ("id", "name", "materials", "inputs", "outputs",
                 "factor_version", "allocation", "direct_kg", "total_kg",
                 "product_trees")

    def __init__(self, id, name, materials, inputs, outputs, factor_version,
                 allocation):
        self.id = id
        self.name = name
        self.materials = materials
        self.inputs = inputs
        self.outputs = outputs
        self.factor_version = factor_version
        self.allocation = allocation
        self.direct_kg = Decimal(0)
        self.total_kg = Decimal(0)
        # product_id -> scaled contribution tree (exact Decimals)
        self.product_trees: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# Validation + resolution
# --------------------------------------------------------------------------- #

def _err(errs: list[ErrorItem], code: str, path: str, message: str) -> None:
    errs.append(ErrorItem(code, path, message))


def _check_amount(errs, value: Decimal, path: str, what: str) -> bool:
    if value < 0:
        _err(errs, "NEGATIVE_AMOUNT", path,
             f"{what} must not be negative, got {canon_decimal(value)}")
        return False
    if value == 0:
        _err(errs, "NON_POSITIVE_AMOUNT", path,
             f"{what} must be greater than zero, got 0")
        return False
    return True


def resolve(sub: Submission, factors: FactorProvider) -> dict[str, RProcess]:
    errs: list[ErrorItem] = []

    # --- process id uniqueness / basic shape -------------------------------
    procs: dict[str, RProcess] = {}
    for pi, p in enumerate(sub.processes):
        path = f".processes[{p.id}]" if p.id else f".processes[{pi}]"
        if not p.id:
            _err(errs, "MISSING_FIELD", f".processes[{pi}].id",
                 "process id is required")
            continue
        if p.id in procs:
            _err(errs, "DUPLICATE_ID", f".processes[{p.id}].id",
                 f"duplicate process id '{p.id}'")
            continue

        # effective factor version / allocation
        eff_version = p.factor_version or sub.default_factor_version
        eff_alloc = p.allocation or sub.default_allocation
        if len(p.outputs) > 1 and eff_alloc is None:
            _err(errs, "ALLOCATION_METHOD_MISSING",
                 f"{path}.allocation",
                 "process has multiple products but neither process-level nor "
                 "submission-default allocation method is set")

        rmats: list[RMaterial] = []
        seen_mat: set[str] = set()
        for m in p.materials:
            mpath = f"{path}.materials[{m.id}]"
            if m.id in seen_mat:
                _err(errs, "DUPLICATE_ID", f"{mpath}.id",
                     f"duplicate material input id '{m.id}'")
            seen_mat.add(m.id)
            _check_amount(errs, m.quantity, f"{mpath}.quantity",
                          "material quantity")
            # unit known?
            try:
                units.dimension_of(m.unit)
            except ValidationError as e:
                errs.append(ErrorItem("UNKNOWN_UNIT", f"{mpath}.unit",
                                      e.errors[0].message))
                continue

            mversion = m.factor_version or eff_version
            if not mversion:
                _err(errs, "MISSING_FACTOR_VERSION", f"{mpath}.factor_version",
                     "no factor version: set material.factor_version, "
                     "process.factor_version or default_factor_version")
                continue
            if not factors.version_exists(mversion):
                _err(errs, "FACTOR_VERSION_NOT_FOUND",
                      f"{mpath}.factor_version:{mversion}",
                      f"factor version '{mversion}' does not exist")
                continue
            fac = factors.get_factor(mversion, m.factor_key)
            if fac is None:
                _err(errs, "FACTOR_NOT_FOUND",
                      f"{mpath}.factor_key:{mversion}/{m.factor_key}",
                      f"emission factor '{m.factor_key}' is missing from "
                      f"version '{mversion}'")
                continue

            # unit dimensions: material unit vs factor reference unit
            try:
                m_dim = units.dimension_of(m.unit)
            except ValidationError:
                m_dim = None
            f_dim = None
            try:
                f_dim = units.dimension_of(fac["reference_unit"])
            except ValidationError:
                _err(errs, "UNKNOWN_UNIT",
                      f"{mpath}.factor_reference_unit:{fac['reference_unit']}",
                      f"factor reference unit '{fac['reference_unit']}' is "
                      "not a known unit")
            declared = m.factor_unit or fac["reference_unit"]
            if m_dim is not None and f_dim is not None:
                try:
                    units.ensure_same_dimension(
                        m.unit, declared,
                        code="MATERIAL_FACTOR_DIMENSION_MISMATCH",
                        path=f"{mpath}.unit",
                        message=(f"material unit '{m.unit}' is not of the same "
                                 f"dimension as factor '{m.factor_key}' "
                                 f"reference unit '{declared}'"))
                except ValidationError as e:
                    errs.extend(e.errors)
                if m.factor_unit:
                    try:
                        units.ensure_same_dimension(
                            m.factor_unit, fac["reference_unit"],
                            code="FACTOR_UNIT_DIMENSION_MISMATCH",
                            path=f"{mpath}.factor_unit")
                    except ValidationError as e:
                        errs.extend(e.errors)

            rmats.append(RMaterial(
                m.id, m.quantity, m.unit, m.factor_key, mversion,
                fac["kg_co2e_per_unit"], fac["reference_unit"],
                fac.get("name"), declared))

        rinputs: list[RInput] = []
        seen_in: set[str] = set()
        for i in p.inputs:
            ipath = f"{path}.inputs[{i.id}]"
            if i.id in seen_in:
                _err(errs, "DUPLICATE_ID", f"{ipath}.id",
                     f"duplicate product input id '{i.id}'")
            seen_in.add(i.id)
            _check_amount(errs, i.quantity, f"{ipath}.quantity",
                          "input quantity")
            try:
                units.dimension_of(i.unit)
            except ValidationError as e:
                errs.append(ErrorItem("UNKNOWN_UNIT", f"{ipath}.unit",
                                      e.errors[0].message))
            rinputs.append(RInput(i.id, i.from_process, i.product_id,
                                  i.quantity, i.unit))

        routputs: list[ROutput] = []
        seen_out: set[str] = set()
        for o in p.outputs:
            opath = f"{path}.outputs[{o.id}]"
            if o.id in seen_out:
                _err(errs, "DUPLICATE_ID", f"{opath}.id",
                     f"duplicate product output id '{o.id}'")
            seen_out.add(o.id)
            if o.product_id in {x.product_id for x in routputs}:
                _err(errs, "DUPLICATE_ID", f"{opath}.product_id",
                     f"duplicate product_id '{o.product_id}' in process "
                     f"'{p.id}'")
            amount_ok = _check_amount(errs, o.quantity, f"{opath}.quantity",
                                      "output quantity")
            try:
                odim = units.dimension_of(o.unit)
            except ValidationError as e:
                odim = None
                errs.append(ErrorItem("UNKNOWN_UNIT", f"{opath}.unit",
                                      e.errors[0].message))

            weight_value: Optional[Decimal] = None
            weight_label: Optional[str] = None
            if o.weight is not None and o.revenue is not None:
                _err(errs, "AMBIGUOUS_ECONOMIC_WEIGHT", f"{opath}",
                     "provide either weight or revenue, not both")
            raw_weight = o.weight if o.weight is not None else o.revenue
            if raw_weight is not None:
                if raw_weight < 0:
                    _err(errs, "NEGATIVE_AMOUNT",
                          f"{opath}.{'weight' if o.weight is not None else 'revenue'}",
                          "allocation weight must not be negative")
                weight_value = raw_weight
                weight_label = o.weight_unit

            # Method-specific weight resolution. Single-output processes need
            # no weights at all: the sole product receives share = 1.
            method = eff_alloc
            needs_method = len(p.outputs) > 1
            if amount_ok and odim is not None and needs_method and method is not None:
                m = method
                if m == "mass":
                    if raw_weight is not None:
                        wunit = o.weight_unit or "kg"
                        try:
                            wdim = units.dimension_of(wunit)
                            if wdim is not Dimension.MASS:
                                _err(errs, "ALLOCATION_UNIT_MISMATCH",
                                     f"{opath}.weight_unit",
                                     f"mass allocation requires a mass unit, "
                                     f"got '{wunit}' ({wdim.value})")
                            else:
                                weight_value = to_base(raw_weight, wunit)
                                weight_label = "kg"
                        except ValidationError as e:
                            errs.extend(e.errors)
                    else:
                        if odim is Dimension.MASS:
                            weight_value = to_base(o.quantity, o.unit)
                            weight_label = "kg"
                        else:
                            _err(errs, "ALLOCATION_WEIGHT_MISSING",
                                 f"{opath}.weight",
                                 "mass allocation requires an explicit mass "
                                 f"weight because output unit '{o.unit}' is "
                                 "not a mass unit")
                elif m == "energy":
                    if raw_weight is None:
                        _err(errs, "ALLOCATION_WEIGHT_MISSING",
                             f"{opath}.weight",
                             "energy allocation requires an explicit energy "
                             "weight per product")
                    else:
                        wunit = o.weight_unit or "MJ"
                        try:
                            wdim = units.dimension_of(wunit)
                            if wdim is not Dimension.ENERGY:
                                _err(errs, "ALLOCATION_UNIT_MISMATCH",
                                     f"{opath}.weight_unit",
                                     f"energy allocation requires an energy "
                                     f"unit, got '{wunit}' ({wdim.value})")
                            else:
                                weight_value = to_base(raw_weight, wunit)
                                weight_label = "MJ"
                        except ValidationError as e:
                            errs.extend(e.errors)
                elif m == "economic":
                    if raw_weight is None:
                        _err(errs, "ALLOCATION_WEIGHT_MISSING",
                             f"{opath}.revenue",
                             "economic allocation requires revenue (or weight) "
                             "per product")
                    else:
                        weight_label = o.weight_unit or "currency"

            routputs.append(ROutput(o.id, o.product_id, o.quantity, o.unit,
                                    weight_value, weight_label))

        # economic allocation: consistent currency labels
        if eff_alloc == "economic" and len(p.outputs) > 1:
            labels = {o.weight_label for o in routputs
                      if o.weight_label is not None}
            if len(labels) > 1:
                _err(errs, "ECONOMIC_UNIT_MISMATCH", f"{path}.outputs",
                     "economic allocation weights use inconsistent currencies: "
                     + ", ".join(sorted(labels)))

        procs[p.id] = RProcess(p.id, p.name, rmats, rinputs, routputs,
                               eff_version, eff_alloc)

    if errs:
        raise ValidationError(errs)

    # --- edges + allocation denominators + cycle ---------------------------
    _resolve_edges(procs, errs)
    _resolve_allocation(procs, errs)
    if errs:
        raise ValidationError(errs)

    order = _topo_order(procs, errs)
    if errs:
        raise ValidationError(errs)

    procs_ordered = {pid: procs[pid] for pid in order}
    return procs_ordered


def _resolve_edges(procs: dict[str, RProcess], errs: list[ErrorItem]) -> None:
    for p in procs.values():
        for i in p.inputs:
            ipath = f".processes[{p.id}].inputs[{i.id}]"
            src = procs.get(i.from_process)
            if src is None:
                _err(errs, "UPSTREAM_PROCESS_NOT_FOUND",
                     f"{ipath}.from_process",
                     f"input '{i.id}' references unknown upstream process "
                     f"'{i.from_process}'")
                continue
            out = next((o for o in src.outputs
                        if o.product_id == i.product_id), None)
            if out is None:
                _err(errs, "UPSTREAM_PRODUCT_NOT_FOUND",
                     f"{ipath}.product_id",
                     f"process '{i.from_process}' does not output product "
                     f"'{i.product_id}'")
                continue
            try:
                units.ensure_same_dimension(
                    i.unit, out.unit,
                    code="INPUT_UNIT_DIMENSION_MISMATCH", path=f"{ipath}.unit",
                    message=(f"input unit '{i.unit}' cannot receive product "
                             f"'{i.product_id}' declared in '{out.unit}' "
                             "(different dimensions)"))
            except ValidationError as e:
                errs.extend(e.errors)
                continue
            in_base = to_base(i.quantity, i.unit)
            out_base = to_base(out.quantity, out.unit)
            i.edge_share = in_base / out_base


def _resolve_allocation(procs: dict[str, RProcess],
                        errs: list[ErrorItem]) -> None:
    for p in procs.values():
        path = f".processes[{p.id}]"
        if len(p.outputs) == 1:
            p.outputs[0].share = Decimal(1)
            continue
        weights = [o.weight if o.weight is not None else None
                   for o in p.outputs]
        if any(w is None for w in weights):
            # missing-weight errors already emitted during resolution
            continue
        if any(w < 0 for w in weights):
            continue
        denom = sum(weights, Decimal(0))
        if denom == 0:
            _err(errs, "ALLOCATION_DENOMINATOR_ZERO",
                 f"{path}.outputs",
                 f"sum of {p.allocation} allocation weights is zero for "
                 f"process '{p.id}'; refusing to divide footprint by zero")
            continue
        for o, w in zip(p.outputs, weights):
            o.share = w / denom


def _topo_order(procs: dict[str, RProcess],
                errs: list[ErrorItem]) -> list[str]:
    indeg: dict[str, int] = {pid: 0 for pid in procs}
    adj: dict[str, list[str]] = {pid: [] for pid in procs}
    for p in procs.values():
        for i in p.inputs:
            if i.from_process in procs:
                adj[i.from_process].append(p.id)
                indeg[p.id] += 1
    # Deterministic Kahn: process ready set by id.
    ready = sorted(pid for pid, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        pid = ready.pop(0)
        order.append(pid)
        for nxt in sorted(adj[pid]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(order) != len(procs):
        cycle = _find_cycle(procs)
        _err(errs, "CYCLE_DETECTED", ".processes",
             "product flow graph contains a cycle: " + " -> ".join(cycle))
        return []
    return order


def _find_cycle(procs: dict[str, RProcess]) -> list[str]:
    color: dict[str, int] = {pid: 0 for pid in procs}  # 0 white 1 gray 2 black
    stack: list[str] = []

    def dfs(u: str) -> Optional[list[str]]:
        color[u] = 1
        stack.append(u)
        for i in procs[u].inputs:
            v = i.from_process
            if v not in procs:
                continue
            if color[v] == 1:
                start = stack.index(v)
                return stack[start:] + [v]
            if color[v] == 0:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = 2
        return None

    for pid in sorted(procs):
        if color[pid] == 0:
            found = dfs(pid)
            if found:
                return found
    return list(procs)[:1]


# --------------------------------------------------------------------------- #
# Computation
# --------------------------------------------------------------------------- #

def _scale_tree(node: dict, factor: Decimal) -> dict:
    return {
        **{k: v for k, v in node.items() if k not in ("kg", "children")},
        "kg": node["kg"] * factor,
        "children": [_scale_tree(c, factor) for c in node.get("children", ())],
    }


def _material_direct(m: RMaterial) -> Decimal:
    """kg CO2e = quantity expressed in factor reference unit * factor."""
    qty_base = to_base(m.quantity, m.unit)
    qty_in_ref = to_unit(qty_base, m.factor_unit)
    return qty_in_ref * m.factor_value


def compute(sub: Submission, factors: FactorProvider) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        procs = resolve(sub, factors)

        for pid, p in procs.items():
            children: list[dict] = []
            direct_total = Decimal(0)

            for m in p.materials:
                kg = _material_direct(m)
                direct_total += kg
                children.append({
                    "kind": "material",
                    "id": m.id,
                    "factor_key": m.factor_key,
                    "factor_version": m.factor_version,
                    "factor_name": m.factor_name,
                    "reference_unit": m.factor_unit,
                    "kg_co2e_per_unit": m.factor_value,
                    "input_quantity": m.quantity,
                    "input_unit": m.unit,
                    "kg": kg,
                    "children": [],
                })

            embedded_total = Decimal(0)
            for i in p.inputs:
                src = procs[i.from_process]
                upstream = src.product_trees[i.product_id]
                node = {
                    "kind": "input",
                    "id": i.id,
                    "from_process": i.from_process,
                    "product_id": i.product_id,
                    "quantity": i.quantity,
                    "unit": i.unit,
                    "transfer_share": i.edge_share,
                    "kg": upstream["kg"] * i.edge_share,
                    "children": [_scale_tree(c, i.edge_share)
                                 for c in upstream["children"]],
                }
                embedded_total += upstream["kg"] * i.edge_share
                children.append(node)

            p.direct_kg = direct_total
            p.total_kg = direct_total + embedded_total

            for o in p.outputs:
                subtree = {
                    "kind": "product",
                    "process_id": p.id,
                    "product_id": o.product_id,
                    "allocation": p.allocation or "mass",
                    "allocation_share": o.share,
                    "kg": p.total_kg * o.share,
                    "children": [_scale_tree(c, o.share) for c in children],
                }
                p.product_trees[o.product_id] = subtree

        return _build_result(sub, procs)


# --------------------------------------------------------------------------- #
# Result serialization (exact tree -> quantized API view)
# --------------------------------------------------------------------------- #

def _proc_seg(process_id: str, exit_product_id: str, share: Decimal) -> dict:
    return {"kind": "process", "process_id": process_id,
            "exit_product_id": exit_product_id,
            "share": str(q12(share))}


def _edge_seg(downstream_id: str, inp_node: dict) -> dict:
    return {"kind": "transfer", "process_id": inp_node["from_process"],
            "to_process": downstream_id, "input_id": inp_node["id"],
            "product_id": inp_node["product_id"],
            "share": str(q12(inp_node["transfer_share"]))}


def _chains(node: dict, suffix: list[dict]) -> list[dict]:
    """Walk an exact contribution tree and return material leaves, each with
    a forward-ordered chain origin -> ... -> target product exit.

    ``suffix`` is the path *below* the node (closer to the final product).
    """
    kind = node["kind"]
    if kind == "material":
        return [{
            "source_process_id": node["home_process"],
            "material_id": node["id"],
            "factor_key": node["factor_key"],
            "factor_version": node["factor_version"],
            "reference_unit": node["reference_unit"],
            "kg_co2e_per_unit": str(node["kg_co2e_per_unit"]),
            "input_quantity": canon_decimal(node["input_quantity"]),
            "input_unit": node["input_unit"],
            "kg_co2e": fixed9(node["kg"]),
            "chain": list(suffix),
        }]
    if kind == "input":
        seg = _edge_seg(node["downstream_process"], node)
        path = [seg] + suffix
        leaves: list[dict] = []
        for c in node["children"]:
            leaves.extend(_chains(c, path))
        return leaves
    # product root: every sub-node leaves through this product exit
    seg = _proc_seg(node["process_id"], node["product_id"],
                    node["allocation_share"])
    path = [seg] + suffix
    out: list[dict] = []
    for c in node["children"]:
        out.extend(_chains(c, path))
    return out


def _annotate(node: dict, home_process: str) -> None:
    """Stamp each node with context needed when emitting chains."""
    if node["kind"] == "input":
        node["downstream_process"] = home_process
        for c in node["children"]:
            _annotate(c, node["from_process"])
    elif node["kind"] == "product":
        for c in node["children"]:
            _annotate(c, node["process_id"])
    else:
        node["home_process"] = home_process


def _serialize_tree(node: dict) -> dict:
    out: dict[str, Any] = {
        "kind": node["kind"],
        "kg_co2e": fixed9(node["kg"]),
    }
    if node["kind"] == "product":
        out.update(process_id=node["process_id"],
                   product_id=node["product_id"],
                   allocation=node["allocation"],
                   allocation_share=str(q12(node["allocation_share"])))
    elif node["kind"] == "input":
        out.update(input_id=node["id"], from_process=node["from_process"],
                   product_id=node["product_id"],
                   quantity=canon_decimal(node["quantity"]), unit=node["unit"],
                   transfer_share=str(q12(node["transfer_share"])))
    else:
        out.update(material_id=node["id"], factor_key=node["factor_key"],
                   factor_version=node["factor_version"],
                   reference_unit=node["reference_unit"],
                   kg_co2e_per_unit=str(node["kg_co2e_per_unit"]),
                   input_quantity=canon_decimal(node["input_quantity"]),
                   input_unit=node["input_unit"])
    out["children"] = [_serialize_tree(c) for c in node.get("children", ())]
    return out


def _build_result(sub: Submission, procs: dict[str, RProcess]) -> dict[str, Any]:
    proc_dicts: list[dict] = []
    for p in procs.values():
        products: list[dict] = []
        evidence: list[dict] = []

        for m in p.materials:
            evidence.append({
                "type": "factor",
                "material_id": m.id,
                "factor_key": m.factor_key,
                "factor_version": m.factor_version,
                "reference_unit": m.factor_unit,
                "kg_co2e_per_unit": str(m.factor_value),
                "input_quantity": canon_decimal(m.quantity),
                "input_unit": m.unit,
                "kg_co2e": fixed9(_material_direct(m)),
            })
        for i in p.inputs:
            evidence.append({
                "type": "upstream_transfer",
                "input_id": i.id,
                "from_process": i.from_process,
                "product_id": i.product_id,
                "quantity": canon_decimal(i.quantity),
                "unit": i.unit,
                "transfer_share": str(q12(i.edge_share)),
            })

        for o in sorted(p.outputs, key=lambda x: x.product_id):
            tree = p.product_trees[o.product_id]
            alloc_kg = tree["kg"]
            unit_kg = alloc_kg / o.quantity

            # Flat origin->sink chains; annotation stamps each node with the
            # context needed while walking (which process owns an edge, where
            # a material was consumed).
            _annotate(tree, p.id)
            flat = _chains(tree, [])
            flat.sort(key=lambda l: (l["source_process_id"],
                                     l["material_id"],
                                     json.dumps(l["chain"], sort_keys=True)))

            products.append({
                "process_id": p.id,
                "product_id": o.product_id,
                "product_key": f"{p.id}|{o.product_id}",
                "quantity": canon_decimal(o.quantity),
                "unit": o.unit,
                "allocation_method": p.allocation
                                     or ("mass" if len(p.outputs) == 1
                                         else p.allocation),
                "allocation_share": str(q12(o.share)),
                "allocation_weight": (canon_decimal(o.weight)
                                      if o.weight is not None else None),
                "allocation_weight_unit": o.weight_label,
                "allocated_kg_co2e": fixed9(alloc_kg),
                "unit_kg_co2e": fixed9(unit_kg),
                "contribution_tree": _serialize_tree(tree),
                "contribution_chain": flat,
            })

        proc_dicts.append({
            "process_id": p.id,
            "name": p.name,
            "factor_version": p.factor_version,
            "allocation": p.allocation,
            "direct_kg_co2e": fixed9(p.direct_kg),
            "embedded_kg_co2e": fixed9(p.total_kg - p.direct_kg),
            "total_kg_co2e": fixed9(p.total_kg),
            "products": products,
            "evidence": evidence,
        })

    return {
        "algo_version": ALGO_VERSION,
        "co2e_unit": "kg CO2e",
        "processes": proc_dicts,
    }


# --------------------------------------------------------------------------- #
# Idempotency: canonical input summary + content hash
# --------------------------------------------------------------------------- #

def input_summary(sub: Submission) -> dict[str, Any]:
    """Canonical, serialization-stable summary of the *request*.

    Factor *values* are deliberately excluded: identity is "same inputs and
    same factor versions" per the idempotency contract. Captured values live
    in the immutable snapshot.
    """
    return {
        "algo_version": ALGO_VERSION,
        "submission_id": sub.id,
        "default_factor_version": sub.default_factor_version,
        "default_allocation": sub.default_allocation,
        "processes": [
            {
                "id": p.id,
                "name": p.name,
                "factor_version": p.factor_version,
                "allocation": p.allocation,
                "materials": sorted([
                    {"id": m.id,
                     "quantity": canon_decimal(m.quantity),
                     "unit": m.unit,
                     "factor_key": m.factor_key,
                     "factor_version": m.factor_version,
                     "factor_unit": m.factor_unit}
                    for m in p.materials
                ], key=lambda x: x["id"]),
                "inputs": sorted([
                    {"id": i.id, "from_process": i.from_process,
                     "product_id": i.product_id,
                     "quantity": canon_decimal(i.quantity), "unit": i.unit}
                    for i in p.inputs
                ], key=lambda x: x["id"]),
                "outputs": sorted([
                    {"id": o.id, "product_id": o.product_id,
                     "quantity": canon_decimal(o.quantity), "unit": o.unit,
                     "weight": (canon_decimal(o.weight)
                                if o.weight is not None else None),
                     "weight_unit": o.weight_unit,
                     "revenue": (canon_decimal(o.revenue)
                                 if o.revenue is not None else None)}
                    for o in p.outputs
                ], key=lambda x: x["id"]),
            }
            for p in sorted(sub.processes, key=lambda x: x.id)
        ],
    }


def content_hash(summary: dict[str, Any]) -> str:
    blob = json.dumps(summary, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def factor_versions_used(result: dict[str, Any]) -> dict[str, str]:
    used: dict[str, str] = {}
    for proc in result["processes"]:
        for ev in proc["evidence"]:
            if ev["type"] == "factor":
                used[f"{proc['process_id']}.{ev['material_id']}"] = \
                    ev["factor_version"]
    return dict(sorted(used.items()))
