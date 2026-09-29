"""FastAPI application: Product Carbon Footprint Accounting & Evidence API."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db as database
from . import engine
from .errors import (ConflictError, NotFoundError, ValidationError,
                     error_body)
from .schemas import (Factor, FactorVersionClone, FactorVersionCreate,
                      RecomputeRequest, Submission)
from .seed import SEED_DESCRIPTION, SEED_FACTORS, SEED_VERSION
from .units import dimension_of

DB_PATH = os.environ.get("PCF_DB_PATH", "/data/pcf.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn_factory = lambda: database.connect(DB_PATH)
    conn = database.connect(DB_PATH)
    try:
        # Seed the built-in factor library exactly once.
        if database.get_factor_version(conn, SEED_VERSION) is None:
            database.create_factor_version(conn, SEED_VERSION, SEED_DESCRIPTION)
            for key, name, kg, unit, desc in SEED_FACTORS:
                database.put_factor(conn, SEED_VERSION, key, name, kg, unit,
                                    desc)
            database.freeze_factor_version(conn, SEED_VERSION)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Product Carbon Footprint Accounting & Evidence API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Register process batches with material inputs and multi-product "
        "output DAGs; the service validates, computes direct emissions, "
        "propagates upstream footprints, allocates across co-products and "
        "stores immutable, idempotent snapshots."),
)


def _conn(request: Request):
    return request.app.state.conn_factory()


class DBFactors:
    """FactorProvider backed by SQLite; returns Decimal-valued dicts."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def version_exists(self, version: str) -> bool:
        return database.get_factor_version(self.conn, version) is not None

    def get_factor(self, version: str, key: str) -> Optional[dict[str, Any]]:
        row = database.get_factor(self.conn, version, key)
        if row is None:
            return None
        return {"key": row["factor_key"], "name": row["name"],
                "kg_co2e_per_unit": Decimal(row["kg_co2e_per_unit"]),
                "reference_unit": row["reference_unit"],
                "description": row["description"]}


# --------------------------------------------------------------------------- #
# Exception handling — stable error schema for every 4xx
# --------------------------------------------------------------------------- #

@app.exception_handler(ValidationError)
async def _on_validation(_request: Request, exc: ValidationError):
    return JSONResponse(status_code=422,
                        content=error_body("validation_error", exc.errors))


@app.exception_handler(NotFoundError)
async def _on_not_found(_request: Request, exc: NotFoundError):
    return JSONResponse(status_code=404,
                        content=error_body("not_found", [exc.error_item()]))


@app.exception_handler(ConflictError)
async def _on_conflict(_request: Request, exc: ConflictError):
    return JSONResponse(status_code=409,
                        content=error_body("conflict", [exc.item]))


@app.exception_handler(RequestValidationError)
async def _on_pydantic(_request: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p not in ("body", ""))
        etype = err["type"]
        if "decimal" in etype or etype == "value_error":
            code = "INVALID_DECIMAL"
        elif "extra" in etype:
            code = "UNKNOWN_FIELD"
        elif "missing" in etype:
            code = "MISSING_FIELD"
        elif "enum" in etype:
            code = "INVALID_ENUM_VALUE"
        elif "string_too_short" in etype:
            code = "EMPTY_ID"
        elif "too_short" in etype:
            code = "MISSING_FIELD"
        elif "bool" in etype:
            code = "INVALID_BOOLEAN"
        else:
            code = "INVALID_REQUEST"
        details.append({"code": code, "path": f".{loc}",
                        "message": err["msg"]})
    return JSONResponse(status_code=422,
                        content=error_body("request_schema_error", details))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _factor_dict(row) -> dict[str, Any]:
    return {"version": row["version"], "factor_key": row["factor_key"],
            "name": row["name"],
            "kg_co2e_per_unit": row["kg_co2e_per_unit"],
            "reference_unit": row["reference_unit"],
            "description": row["description"],
            "dimension": dimension_of(row["reference_unit"]).value}


def _envelope(row, result, *, reused: bool, diff: Optional[dict] = None) -> dict:
    body = {
        "snapshot_id": row["snapshot_id"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "reused": reused,
        "immutable": True,
        "prior_snapshot_id": row["prior_snapshot_id"],
        "input_summary": _loads(row["input_summary"]),
        "factor_versions": _loads(row["factor_versions"]),
        "result": result,
    }
    if diff is not None:
        body["diff_vs_previous"] = diff
    return body


def _loads(s: str) -> Any:
    import json
    return json.loads(s)


def _create_snapshot(conn, sub: Submission, *,
                     prior_snapshot_id: Optional[str] = None) -> tuple[Any, bool]:
    """Compute + persist, honoring content-hash idempotency.

    Returns ``(row, reused)``. On a hash collision the *existing* row is
    returned untouched — factor updates can therefore never mutate it.
    """
    factors = DBFactors(conn)
    summary = engine.input_summary(sub)
    chash = engine.content_hash(summary)

    existing = database.get_snapshot_by_hash(conn, chash)
    if existing is not None:
        return existing, True

    result = engine.compute(sub, factors)
    versions = engine.factor_versions_used(result)
    snapshot_id = "snp_" + uuid.uuid4().hex[:16]
    request_obj = sub.model_dump(mode="json")
    try:
        database.save_snapshot(
            conn, snapshot_id=snapshot_id, content_hash=chash,
            prior_snapshot_id=prior_snapshot_id, input_summary=summary,
            factor_versions=versions, request=request_obj, result=result)
    except sqlite3.IntegrityError:
        # Concurrent identical submission won the content_hash unique race.
        winner = database.get_snapshot_by_hash(conn, chash)
        if winner is not None:
            return winner, True
        raise
    return database.get_snapshot(conn, snapshot_id), False


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": "pcf-api", "algo_version": engine.ALGO_VERSION}


# --------------------------------------------------------------------------- #
# Factor versions (factor governance)
# --------------------------------------------------------------------------- #

@app.post("/factor-versions", status_code=201, tags=["factors"])
def create_factor_version(payload: FactorVersionCreate, request: Request):
    conn = _conn(request)
    try:
        if database.get_factor_version(conn, payload.version) is not None:
            raise ConflictError(
                "FACTOR_VERSION_EXISTS", f".version:{payload.version}",
                f"factor version '{payload.version}' already exists; factor "
                "updates require a new version, they never rewrite history")
        database.create_factor_version(conn, payload.version,
                                       payload.description)
        return {"version": payload.version, "description": payload.description,
                "frozen": False}
    finally:
        conn.close()


@app.get("/factor-versions", tags=["factors"])
def list_factor_versions(request: Request):
    conn = _conn(request)
    try:
        return [{"version": r["version"], "description": r["description"],
                 "frozen": bool(r["frozen"]), "created_at": r["created_at"]}
                for r in database.list_factor_versions(conn)]
    finally:
        conn.close()


@app.post("/factor-versions/{version}/factors", status_code=201,
          tags=["factors"])
def put_factor(version: str, payload: Factor, request: Request):
    conn = _conn(request)
    try:
        vrow = database.get_factor_version(conn, version)
        if vrow is None:
            raise NotFoundError("factor version", version, f".version:{version}")
        if vrow["frozen"]:
            raise ConflictError(
                "FACTOR_VERSION_FROZEN", f".version:{version}",
                f"factor version '{version}' is frozen; register a new "
                "version instead of changing historical factors")
        # Validate reference unit now for a stable error.
        dimension_of(payload.reference_unit)
        existing = database.get_factor(conn, version, payload.key)
        database.put_factor(
            conn, version, payload.key, payload.name,
            str(payload.kg_co2e_per_unit), payload.reference_unit,
            payload.description)
        status = "updated" if existing else "created"
        return {"status": status, "version": version,
                "factor_key": payload.key}
    finally:
        conn.close()


@app.get("/factor-versions/{version}/factors", tags=["factors"])
def list_factors(version: str, request: Request):
    conn = _conn(request)
    try:
        if database.get_factor_version(conn, version) is None:
            raise NotFoundError("factor version", version, f".version:{version}")
        return [_factor_dict(r)
                for r in database.list_factors(conn, version)]
    finally:
        conn.close()


@app.post("/factor-versions/{version}/clone", status_code=201, tags=["factors"])
def clone_factor_version(version: str, payload: FactorVersionClone,
                         request: Request):
    """Copy an existing version into a new (open) one with factor overrides.

    This is the supported "factor update" path: old snapshots keep pointing at
    the old, frozen version; new calculations use the new version.
    """
    conn = _conn(request)
    try:
        if database.get_factor_version(conn, version) is None:
            raise NotFoundError("factor version", version, f".version:{version}")
        if database.get_factor_version(conn, payload.new_version) is not None:
            raise ConflictError(
                "FACTOR_VERSION_EXISTS", f".new_version:{payload.new_version}",
                f"factor version '{payload.new_version}' already exists")
        overrides = {f.key: f for f in payload.overrides}
        database.create_factor_version(
            conn, payload.new_version,
            payload.description or f"Cloned from {version}")
        copied = 0
        for r in database.list_factors(conn, version):
            f = overrides.pop(r["factor_key"], None)
            kg = str(f.kg_co2e_per_unit) if f is not None else r["kg_co2e_per_unit"]
            ref = f.reference_unit if f is not None else r["reference_unit"]
            name = f.name if f is not None and f.name is not None else r["name"]
            desc = (f.description if f is not None and f.description is not None
                    else r["description"])
            if f is not None:
                dimension_of(f.reference_unit)
            database.put_factor(conn, payload.new_version, r["factor_key"],
                                name, kg, ref, desc)
            copied += 1
        for f in overrides.values():
            dimension_of(f.reference_unit)
            database.put_factor(conn, payload.new_version, f.key, f.name,
                                str(f.kg_co2e_per_unit), f.reference_unit,
                                f.description)
            copied += 1
        return {"new_version": payload.new_version, "cloned_from": version,
                "factors": copied, "overridden": len(payload.overrides)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #

@app.post("/accounting/submissions", status_code=201, tags=["accounting"])
def submit(payload: Submission, request: Request):
    """Validate + compute a graph and store an immutable snapshot.

    Idempotent: the same input summary with the same factor versions returns
    the previously stored snapshot (``reused: true``).
    """
    conn = _conn(request)
    try:
        row, reused = _create_snapshot(conn, payload)
        result = _loads(row["result_json"])
        status_code = 200 if reused else 201
        return JSONResponse(status_code=status_code,
                            content=_envelope(row, result, reused=reused))
    finally:
        conn.close()


@app.get("/accounting/snapshots", tags=["accounting"])
def list_snapshots(request: Request):
    conn = _conn(request)
    try:
        return [{"snapshot_id": r["snapshot_id"],
                 "content_hash": r["content_hash"],
                 "prior_snapshot_id": r["prior_snapshot_id"],
                 "factor_versions": _loads(r["factor_versions"]),
                 "created_at": r["created_at"]}
                for r in database.list_snapshots(conn)]
    finally:
        conn.close()


@app.get("/accounting/snapshots/{snapshot_id}", tags=["accounting"])
def get_snapshot(snapshot_id: str, request: Request):
    conn = _conn(request)
    try:
        row = database.get_snapshot(conn, snapshot_id)
        if row is None:
            raise NotFoundError("snapshot", snapshot_id,
                                f".snapshot_id:{snapshot_id}")
        return _envelope(row, _loads(row["result_json"]), reused=False)
    finally:
        conn.close()


@app.get("/accounting/snapshots/{snapshot_id}/products/{product_key:path}",
         tags=["accounting"])
def get_product(snapshot_id: str, product_key: str, request: Request):
    """Per-product footprint slice; product_key is 'process_id|product_id'."""
    conn = _conn(request)
    try:
        row = database.get_snapshot(conn, snapshot_id)
        if row is None:
            raise NotFoundError("snapshot", snapshot_id,
                                f".snapshot_id:{snapshot_id}")
        prow = database.get_product_row(conn, snapshot_id, product_key)
        if prow is None:
            raise NotFoundError("product", product_key,
                                f".product_key:{product_key}")
        result = _loads(row["result_json"])
        proc = next(p for p in result["processes"]
                    if p["process_id"] == prow["process_id"])
        product = next(p for p in proc["products"]
                       if p["product_id"] == prow["product_id"])
        return {"snapshot_id": snapshot_id,
                "factor_versions": _loads(row["factor_versions"]),
                "process": proc["process_id"],
                "product": product}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Recomputation with new factor versions + per-product diff
# --------------------------------------------------------------------------- #

def _diff_products(old_result: dict, new_result: dict) -> dict[str, Any]:
    def index(result):
        return {(p["process_id"], q["product_id"]): q
                for p in result["processes"] for q in p["products"]}

    old = index(old_result)
    new = index(new_result)
    per_product = []
    for key in sorted(new):
        n = new[key]
        pk = n["product_key"]
        if key not in old:
            per_product.append({"product_key": pk, "status": "added",
                                "unit_kg_co2e": n["unit_kg_co2e"]})
            continue
        o = old[key]
        old_unit = Decimal(o["unit_kg_co2e"])
        new_unit = Decimal(n["unit_kg_co2e"])
        old_total = Decimal(o["allocated_kg_co2e"])
        new_total = Decimal(n["allocated_kg_co2e"])
        delta = new_unit - old_unit
        pct = None
        if old_unit != 0:
            pct = str(engine.q12((delta / old_unit) * Decimal(100)))
        status = "unchanged" if delta == 0 else "changed"
        per_product.append({
            "product_key": pk, "status": status,
            "unit_kg_co2e_old": str(old_unit),
            "unit_kg_co2e_new": str(new_unit),
            "unit_delta_kg_co2e": engine.fixed9(delta),
            "unit_delta_percent": pct,
            "allocated_kg_co2e_old": str(old_total),
            "allocated_kg_co2e_new": str(new_total),
            "allocated_delta_kg_co2e": engine.fixed9(new_total - old_total),
        })
    for key in sorted(old):
        if key not in new:
            per_product.append({"product_key": old[key]["product_key"],
                                "status": "removed",
                                "unit_kg_co2e_old": old[key]["unit_kg_co2e"]})

    old_total = sum((Decimal(q["allocated_kg_co2e"])
                     for p in old_result["processes"]
                     for q in p["products"]), Decimal(0))
    new_total = sum((Decimal(q["allocated_kg_co2e"])
                     for p in new_result["processes"]
                     for q in p["products"]), Decimal(0))
    return {
        "product_count_old": len(old),
        "product_count_new": len(new),
        "allocated_kg_co2e_old": engine.fixed9(old_total),
        "allocated_kg_co2e_new": engine.fixed9(new_total),
        "allocated_delta_kg_co2e": engine.fixed9(new_total - old_total),
        "products": per_product,
    }


@app.post("/accounting/snapshots/{snapshot_id}/recompute", status_code=201,
          tags=["accounting"])
def recompute(snapshot_id: str, payload: RecomputeRequest, request: Request):
    """Re-run an immutable snapshot with new factor versions.

    The original snapshot is never modified; a brand-new snapshot linked via
    ``prior_snapshot_id`` is returned together with a per-product diff. If the
    requested factor versions resolve to the same inputs, the existing
    snapshot is returned with ``reused: true`` and an empty diff.
    """
    conn = _conn(request)
    try:
        row = database.get_snapshot(conn, snapshot_id)
        if row is None:
            raise NotFoundError("snapshot", snapshot_id,
                                f".snapshot_id:{snapshot_id}")
        if payload.default_factor_version is None and not payload.factor_versions:
            from .errors import ErrorItem
            raise ValidationError([ErrorItem(
                code="RECOMPUTE_TARGET_MISSING", path=".",
                message="provide default_factor_version and/or "
                        "factor_versions to recompute against")])

        original = Submission.model_validate(_loads(row["request_json"]))

        default_version = (payload.default_factor_version
                           if payload.default_factor_version is not None
                           else original.default_factor_version)
        per_process = payload.factor_versions or {}

        # Verify all referenced versions exist *before* rebuilding.
        wanted = {default_version} if default_version else set()
        wanted.update(per_process.values())
        missing = sorted(v for v in wanted
                         if v and not database.get_factor_version(conn, v))
        if missing:
            from .errors import ErrorItem
            raise ValidationError([
                ErrorItem("FACTOR_VERSION_NOT_FOUND",
                          f".factor_versions:{v}",
                          f"factor version '{v}' does not exist")
                for v in missing])
        bad_proc = sorted(pid for pid in per_process
                          if pid not in {p.id for p in original.processes})
        if bad_proc:
            from .errors import ErrorItem
            raise ValidationError([
                ErrorItem("PROCESS_NOT_FOUND", f".factor_versions:{pid}",
                          f"process '{pid}' is not part of this submission")
                for pid in bad_proc])

        # Rebuild: override default; per-process version wins; material-level
        # explicit versions (None) are left untouched so callers can re-point
        # just the default while pinning selected materials.
        new_default = default_version
        new_processes = []
        for p in original.processes:
            pdata = p.model_dump(mode="json")
            if p.id in per_process:
                pdata["factor_version"] = per_process[p.id]
            new_processes.append(pdata)
        rebuilt = Submission(
            id=original.id, name=original.name,
            default_factor_version=new_default,
            default_allocation=original.default_allocation,
            processes=new_processes)

        new_row, reused = _create_snapshot(
            conn, rebuilt, prior_snapshot_id=snapshot_id)
        old_result = _loads(row["result_json"])
        new_result = _loads(new_row["result_json"])
        diff = _diff_products(old_result, new_result)
        return JSONResponse(
            status_code=200 if reused else 201,
            content=_envelope(new_row, new_result, reused=reused, diff=diff))
    finally:
        conn.close()
