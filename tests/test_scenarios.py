"""End-to-end scenarios exercised against the ASGI app (httpx in-process)."""
import os
import sys
import tempfile
from decimal import Decimal

os.environ["PCF_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "pcf_test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def expect(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("ok  -", msg)


with TestClient(app) as client:
    # ------------------------------------------------------------------ #
    # 1. Two-process DAG, economic allocation, unit conversion + upstream
    # ------------------------------------------------------------------ #
    submission = {
        "id": "case-refinery",
        "name": "Refinery + blending demo",
        "default_factor_version": "v2024",
        "processes": [
            {
                "id": "refining",
                "name": "Crude refining",
                "allocation": "economic",
                "materials": [
                    # 10 t crude = 10,000 kg * 0.45 = 4,500 kg CO2e
                    {"id": "m_crude", "quantity": "10", "unit": "t",
                     "factor_key": "crude_oil"},
                    # 5,000 kWh * 0.42 = 2,100 kg CO2e
                    {"id": "m_elec", "quantity": "5000", "unit": "kWh",
                     "factor_key": "electricity.grid"},
                    # 10,000 MJ gas * 0.056 = 560 kg CO2e
                    {"id": "m_gas", "quantity": "10000", "unit": "MJ",
                     "factor_key": "natural_gas"},
                ],
                "outputs": [
                    {"id": "o_diesel", "product_id": "diesel",
                     "quantity": "3000", "unit": "L",
                     "revenue": "6000"},
                    {"id": "o_naphtha", "product_id": "naphtha",
                     "quantity": "2000", "unit": "L",
                     "revenue": "2000"},
                ],
            },
            {
                "id": "blending",
                "name": "Diesel blending",
                "materials": [
                    {"id": "m_elec", "quantity": "1000", "unit": "kWh",
                     "factor_key": "electricity.grid"},  # 420
                ],
                "inputs": [
                    # receive half of upstream diesel output (1,500 of 3,000 L)
                    {"id": "i_diesel", "from_process": "refining",
                     "product_id": "diesel", "quantity": "1500", "unit": "L"},
                ],
                "outputs": [
                    {"id": "o_blend", "product_id": "blended-diesel",
                     "quantity": "1500", "unit": "L"},
                ],
            },
        ],
    }

    r = client.post("/accounting/submissions", json=submission)
    expect(r.status_code == 201,
           f"submit returns 201, got {r.status_code}: {r.text[:300]}")
    body = r.json()
    snp = body["snapshot_id"]
    expect(body["reused"] is False, "new snapshot flagged reused=false")

    procs = {p["process_id"]: p for p in body["result"]["processes"]}
    ref = procs["refining"]
    expect(Decimal(ref["direct_kg_co2e"]) == Decimal("7160.000000000"),
           f"refinery direct = 7160, got {ref['direct_kg_co2e']}")
    diesel = next(p for p in ref["products"] if p["product_id"] == "diesel")
    expect(diesel["allocation_share"] == "0.750000000000",
           f"diesel economic share 6000/8000 = .75, got {diesel['allocation_share']}")
    expect(Decimal(diesel["allocated_kg_co2e"]) == Decimal("5370.000000000"),
           f"diesel allocated 5370, got {diesel['allocated_kg_co2e']}")
    expect(Decimal(diesel["unit_kg_co2e"]) == Decimal("1.790000000"),
           f"diesel unit 1.79/L, got {diesel['unit_kg_co2e']}")
    naphtha = next(p for p in ref["products"] if p["product_id"] == "naphtha")
    expect(Decimal(naphtha["allocated_kg_co2e"]) == Decimal("1790.000000000"),
           f"naphtha allocated 1790, got {naphtha['allocated_kg_co2e']}")

    blend = procs["blending"]
    expect(Decimal(blend["embedded_kg_co2e"]) == Decimal("2685.000000000"),
           f"blending embedded = 5370*0.5 = 2685, got {blend['embedded_kg_co2e']}")
    bd = blend["products"][0]
    expect(Decimal(bd["allocated_kg_co2e"]) == Decimal("3105.000000000"),
           f"blended total 2685+420=3105, got {bd['allocated_kg_co2e']}")
    expect(Decimal(bd["unit_kg_co2e"]) == Decimal("2.070000000"),
           f"blended unit 2.07, got {bd['unit_kg_co2e']}")

    # contribution chain: leaves must sum to product total
    leaf_sum = sum(Decimal(l["kg_co2e"]) for l in bd["contribution_chain"])
    expect(leaf_sum == Decimal("3105.000000000"),
           f"flat chain leaves sum 3105, got {leaf_sum}")
    # chain shape: upstream leaves traverse transfer then blend exit
    upstream_leaf = next(l for l in bd["contribution_chain"]
                         if l["source_process_id"] == "refining")
    kinds = [s["kind"] for s in upstream_leaf["chain"]]
    expect(kinds == ["transfer", "process"],
           f"upstream chain is transfer->process, got {kinds}")
    # evidence captured exact factor value/version
    ev = next(e for e in ref["evidence"] if e["material_id"] == "m_crude")
    expect(ev["factor_version"] == "v2024" and ev["kg_co2e_per_unit"] == "0.45",
           "evidence freezes factor value 0.45 @ v2024")

    # ------------------------------------------------------------------ #
    # 2. Idempotency
    # ------------------------------------------------------------------ #
    r2 = client.post("/accounting/submissions", json=submission)
    expect(r2.status_code == 200 and r2.json()["reused"] is True
           and r2.json()["snapshot_id"] == snp,
           "identical submission is idempotent (200, same snapshot)")

    # ------------------------------------------------------------------ #
    # 3. Immutability: adding a factor to v2024 is refused (frozen)
    # ------------------------------------------------------------------ #
    r3 = client.post("/factor-versions/v2024/factors", json={
        "key": "electricity.grid", "kg_co2e_per_unit": "9.99",
        "reference_unit": "kWh"})
    expect(r3.status_code == 409 and
           r3.json()["error"]["details"][0]["code"] == "FACTOR_VERSION_FROZEN",
           "rewriting frozen v2024 factors is rejected")
    # historical snapshot unchanged
    r3b = client.get(f"/accounting/snapshots/{snp}")
    ref2 = next(p for p in r3b.json()["result"]["processes"]
                if p["process_id"] == "refining")
    expect(Decimal(ref2["direct_kg_co2e"]) == Decimal("7160.000000000"),
           "history not rewritten after factor update attempt")

    # ------------------------------------------------------------------ #
    # 4. New factor version + recompute + per-product diff
    # ------------------------------------------------------------------ #
    r4 = client.post("/factor-versions/v2024/clone", json={
        "new_version": "v2025",
        "description": "Lower-carbon grid; heavier crude",
        "overrides": [
            {"key": "electricity.grid", "kg_co2e_per_unit": "0.30",
             "reference_unit": "kWh"},
            {"key": "crude_oil", "kg_co2e_per_unit": "0.50",
             "reference_unit": "kg"},
        ]})
    expect(r4.status_code == 201, f"clone v2025 -> 201, got {r4.status_code}:{r4.text}")

    r5 = client.post(f"/accounting/snapshots/{snp}/recompute", json={
        "default_factor_version": "v2025"})
    expect(r5.status_code == 201, f"recompute 201, got {r5.status_code}:{r5.text}")
    nb = r5.json()
    expect(nb["prior_snapshot_id"] == snp and nb["snapshot_id"] != snp,
           "new snapshot linked to prior")
    new_diesel = next(
        p for p in next(x for x in nb["result"]["processes"]
                        if x["process_id"] == "refining")["products"]
        if p["product_id"] == "diesel")
    # refinery: crude 10000*0.5=5000; elec 5000*.3=1500; gas 560 => 7060
    # diesel .75 => 5295; naphtha => 1765
    expect(Decimal(new_diesel["allocated_kg_co2e"]) == Decimal("5295.000000000"),
           f"new diesel 5295, got {new_diesel['allocated_kg_co2e']}")
    diffs = {d["product_key"]: d for d in nb["diff_vs_previous"]["products"]}
    dd = diffs["refining|diesel"]
    expect(dd["unit_delta_kg_co2e"] == "-0.025000000",
           f"diesel unit delta (1.765-1.79)=-0.025, got {dd['unit_delta_kg_co2e']}")
    expect(dd["unit_delta_percent"] == "-1.396648044693",
           f"diesel pct -1.3966..., got {dd['unit_delta_percent']}")
    # recompute again -> reused
    r5b = client.post(f"/accounting/snapshots/{snp}/recompute", json={
        "default_factor_version": "v2025"})
    expect(r5b.status_code == 200 and r5b.json()["reused"] is True,
           "recompute same versions is idempotent")

    # per-product endpoint
    r6 = client.get(f"/accounting/snapshots/{snp}/products/blending|blended-diesel")
    expect(r6.status_code == 200 and
           r6.json()["product"]["unit_kg_co2e"] == "2.070000000",
           "per-product lookup returns blended-diesel footprint")

    # ------------------------------------------------------------------ #
    # 5. Validation errors: cycle, negatives, missing factor, zero denom
    # ------------------------------------------------------------------ #
    def bad_case(name, payload, code, status=422):
        rr = client.post("/accounting/submissions", json=payload)
        ok = rr.status_code == status and any(
            d["code"] == code for d in rr.json()["error"]["details"])
        expect(ok, f"{name}: {code} ({rr.status_code} {rr.text[:200]})")
        return rr.json()

    base_proc = {
        "id": "A", "materials": [],
        "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                     "unit": "kg"}]}

    # cycle: A.input <- B, B.input <- A
    cyc = {"default_factor_version": "v2024", "processes": [
        {"id": "A", "inputs": [{"id": "i", "from_process": "B",
                                 "product_id": "y", "quantity": "1",
                                 "unit": "kg"}],
         "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                      "unit": "kg"}]},
        {"id": "B", "inputs": [{"id": "i", "from_process": "A",
                                 "product_id": "x", "quantity": "1",
                                 "unit": "kg"}],
         "outputs": [{"id": "o", "product_id": "y", "quantity": "1",
                      "unit": "kg"}]}]}
    err = bad_case("cycle", cyc, "CYCLE_DETECTED")
    expect("B -> A -> B" in err["error"]["details"][0]["message"] or
           "A -> B -> A" in err["error"]["details"][0]["message"],
           "cycle message shows the loop")

    neg = {"default_factor_version": "v2024", "processes": [{
        "id": "A",
        "materials": [{"id": "m", "quantity": "-3", "unit": "kWh",
                       "factor_key": "electricity.grid"}],
        "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                     "unit": "kg"}]}]}
    bad_case("negative quantity", neg, "NEGATIVE_AMOUNT")

    missing = {"processes": [{
        "id": "A",
        "materials": [{"id": "m", "quantity": "10", "unit": "kWh",
                       "factor_key": "nope"}],
        "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                     "unit": "kg"}]}]}
    bad_case("missing factor/version", missing, "MISSING_FACTOR_VERSION")
    missing2 = {"default_factor_version": "v2024", "processes": [{
        "id": "A",
        "materials": [{"id": "m", "quantity": "10", "unit": "kWh",
                       "factor_key": "nope"}],
        "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                     "unit": "kg"}]}]}
    bad_case("missing factor key", missing2, "FACTOR_NOT_FOUND")

    zero_denom = {"default_factor_version": "v2024", "processes": [{
        "id": "A", "allocation": "economic",
        "outputs": [
            {"id": "o1", "product_id": "x", "quantity": "1", "unit": "kg",
             "revenue": "0"},
            {"id": "o2", "product_id": "y", "quantity": "1", "unit": "kg",
             "revenue": "0"}]}]}
    bad_case("zero allocation denominator", zero_denom,
             "ALLOCATION_DENOMINATOR_ZERO")

    dim = {"default_factor_version": "v2024", "processes": [{
        "id": "A",
        "materials": [{"id": "m", "quantity": "10", "unit": "L",
                       "factor_key": "electricity.grid"}],
        "outputs": [{"id": "o", "product_id": "x", "quantity": "1",
                     "unit": "kg"}]}]}
    bad_case("dimension mismatch", dim, "MATERIAL_FACTOR_DIMENSION_MISMATCH")

    # multi-product without allocation method
    noalloc = {"default_factor_version": "v2024", "processes": [{
        "id": "A",
        "outputs": [
            {"id": "o1", "product_id": "x", "quantity": "1", "unit": "kg"},
            {"id": "o2", "product_id": "y", "quantity": "1", "unit": "kg"}]}]}
    bad_case("missing allocation method", noalloc,
             "ALLOCATION_METHOD_MISSING")

    # malformed decimal -> stable schema error
    rr = client.post("/accounting/submissions", json={
        "processes": [{"id": "A",
                       "materials": [{"id": "m", "quantity": "abc",
                                      "unit": "kWh",
                                      "factor_key": "electricity.grid"}],
                       "outputs": [{"id": "o", "product_id": "x",
                                    "quantity": "1", "unit": "kg"}]}]})
    expect(rr.status_code == 422 and
           rr.json()["error"]["type"] == "request_schema_error",
           "malformed decimal yields request_schema_error")

    # error details are deterministic (stable) across identical requests
    err_a = client.post("/accounting/submissions",
                        json=zero_denom).json()["error"]["details"]
    err_b = client.post("/accounting/submissions",
                        json=zero_denom).json()["error"]["details"]
    expect(err_a == err_b, "error details are stable between calls")

    # mass allocation by quantity, energy requiring explicit weight
    energy_missing = {"default_factor_version": "v2024", "processes": [{
        "id": "A", "allocation": "energy",
        "outputs": [
            {"id": "o1", "product_id": "x", "quantity": "10", "unit": "L"},
            {"id": "o2", "product_id": "y", "quantity": "5", "unit": "L"}]}]}
    bad_case("energy needs explicit weights", energy_missing,
             "ALLOCATION_WEIGHT_MISSING")

    mass_ok = {"default_factor_version": "v2024", "processes": [{
        "id": "A", "allocation": "mass",
        "materials": [{"id": "m", "quantity": "1000", "unit": "kWh",
                       "factor_key": "electricity.grid"}],
        "outputs": [
            {"id": "o1", "product_id": "x", "quantity": "3", "unit": "t"},
            {"id": "o2", "product_id": "y", "quantity": "1000", "unit": "kg"}]}]}
    rr = client.post("/accounting/submissions", json=mass_ok)
    expect(rr.status_code == 201, f"mass alloc accepts t+kg: {rr.text[:200]}")
    prods = {(p["process_id"], q["product_id"]): q
             for p in rr.json()["result"]["processes"]
             for q in p["products"]}
    expect(prods[("A", "x")]["allocation_share"] == "0.750000000000",
           "mass allocation converts t->kg (3000/4000)")

    # recompute validation: unknown version
    rr = client.post(f"/accounting/snapshots/{snp}/recompute", json={
        "default_factor_version": "v2099"})
    expect(rr.status_code == 422 and
           rr.json()["error"]["details"][0]["code"] ==
           "FACTOR_VERSION_NOT_FOUND",
           "recompute with missing version yields stable 422")

    # not founds
    expect(client.get("/accounting/snapshots/does-not-exist").status_code == 404,
           "missing snapshot -> 404")
    expect(client.get("/factor-versions/nope/factors").status_code == 404,
           "missing factor version -> 404")

print("\nALL SCENARIOS PASSED")
