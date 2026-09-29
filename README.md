# 产品碳足迹核算与证据 API（Product Carbon Footprint Accounting & Evidence API）

基于 **Python + FastAPI + SQLite + Decimal** 的过程级碳足迹核算服务。客户端登记含产量、单位、排放因子版本的**过程批次（process batch）**、**物料投入**和**多产品产出有向无环图（DAG）**，并选择 **质量 / 能量 / 经济价值** 分摊法。服务：

- 按拓扑顺序以 `数量 × 排放因子` 计算直接排放；
- 将上游产品足迹按转移数量比例传递到下游，再按所选产出权重在联产品间分摊；
- 返回每个产品的 **单位 kg CO₂e**、**总 kg CO₂e**、精确的**贡献树**与扁平化的**贡献链（origin → sink）**、以及冻结了因子值的**证据（evidence）**；
- 拒绝**环、负量/零量、未知单位/量纲不匹配、缺失因子版本/因子、多产品缺分摊法、分摊分母为零**等输入，一次返回**稳定排序**的错误明细；
- 核算结果按**输入摘要 + 因子版本**的 SHA-256 内容哈希**幂等**，并以 **INSERT-only 不可变快照**保存；因子更新只能创建新版本，**永不改写历史**；
- 提供快照/产品查询，以及**用新因子版本重算并返回逐产品差异**的 HTTP API。

全部金额、数量、因子、份额均使用 Python `Decimal`（50 位精度上下文，响应按 9 位小数定点输出），不经过任何二进制浮点。

---

## 1. 一键启动（Docker Compose）

需要 Docker + Docker Compose v2。

```bash
docker compose up --build -d
docker compose ps          # 等待 healthy
curl -s http://localhost:8000/health
```

```json
{"status":"ok","service":"pcf-api","algo_version":"1.0.0"}
```

SQLite 数据库保存在命名卷 `pcf-data`（容器内 `/data/pcf.db`），容器重启数据不丢。交互式 API 文档：

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

停止 / 清空数据：

```bash
docker compose down            # 停止，保留数据卷
docker compose down -v         # 停止并删除数据卷
```

### 不用 Docker，本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PCF_DB_PATH=./data/pcf.db uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

首次启动会自动建表并播种内置因子版本 **`v2024`**（已冻结）。运行测试：

```bash
PYTHONPATH=. python tests/test_scenarios.py     # 40 项端到端断言
```

---

## 2. 数据模型与规则速览

| 概念 | 说明 |
| --- | --- |
| `process` | 过程批次，包含 `materials`（物料投入）、`inputs`（上游产品投入）、`outputs`（联产品产出） |
| `factor_version` | 排放因子版本（如 `v2024`）。因子按 `(version, factor_key)` 寻址；版本可冻结，冻结后拒绝写入 |
| `materials[].factor_key` | 物料使用的因子键，`kg_co2e_per_unit` 按因子 `reference_unit` 计 |
| `inputs[]` | DAG 边：`from_process + product_id` 指向上游某个产出，`quantity/unit` 决定转移份额（折算到基准单位之比） |
| `allocation` | `mass` / `energy` / `economic`；单产品过程无需分摊（份额=1） |
| 产出权重 | mass：质量单位产出可自动按产量计（自动换算 kg），否则给 `weight`；energy：必须显式给能量 `weight`；economic：给 `revenue`（或 `weight`）+ 可选币种 |

内置单位（可自动同量纲换算）：`kg,g,t,tonne,mg`（质量）、`MJ,kWh,GJ,J`（能量）、`L,m3,mL`（体积）、`item,piece,unit`（数量）。

**核算顺序**：Kahn 拓扑排序（确定性 id 次序）→ 逐过程 `Σ 物料量(换算到因子单位)×因子 + Σ 上游产品足迹×转移份额` → 按权重分摊到联产品 → 输出产品单位足迹 `分摊总量 / 产出数量`。

**幂等**：对请求做规范化摘要（Decimal 归一化、列表排序）后与所用因子版本一起取 SHA-256；相同摘要命中已有快照，返回 `200 {"reused": true}`，不产生新行。

**不可变**：快照表只有 INSERT 路径；证据行内冻结当次因子的精确值。重算只会生成 `prior_snapshot_id` 链接的新快照。

---

## 3. 实际请求 / 响应示例

### 3.1 查看内置因子版本与因子

```bash
curl -s http://localhost:8000/factor-versions
curl -s http://localhost:8000/factor-versions/v2024/factors
```

```json
[
  {"version":"v2024","description":"Built-in reference factors (illustrative values)","frozen":true,"created_at":"2026-09-29T10:00:00Z"}
]
```

```json
[
  {"version":"v2024","factor_key":"crude_oil","name":"Crude oil (upstream supply)",
   "kg_co2e_per_unit":"0.45","reference_unit":"kg","dimension":"mass", ...},
  {"version":"v2024","factor_key":"electricity.grid","name":"Grid electricity",
   "kg_co2e_per_unit":"0.42","reference_unit":"kWh","dimension":"energy", ...}
]
```

### 3.2 提交核算（两过程 DAG + 经济分摊）

示例文件：[`examples/submission.json`](examples/submission.json)。
`refining` 投入 10 t 原油 + 5,000 kWh 电 + 10,000 MJ 天然气，按收入 6000:2000 分摊给柴油/石脑油；`blending` 接收其中 1,500 L 柴油继续掺混。

```bash
curl -s -X POST http://localhost:8000/accounting/submissions \
  -H 'Content-Type: application/json' \
  --data @examples/submission.json
```

```json
{
  "snapshot_id": "snp_3e1a4a0a0aea4cc2",
  "content_hash": "sha256:8dcf5ade64724144b251d017f176e1796c4423f8219445644225e4cd31587",
  "created_at": "2026-09-29T10:03:09Z",
  "reused": false,
  "immutable": true,
  "prior_snapshot_id": null,
  "input_summary": { "algo_version": "1.0.0", "default_factor_version": "v2024", "...": "规范化输入摘要" },
  "factor_versions": {
    "blending.m_elec": "v2024",
    "refining.m_crude": "v2024",
    "refining.m_elec": "v2024",
    "refining.m_gas": "v2024"
  },
  "result": {
    "algo_version": "1.0.0",
    "co2e_unit": "kg CO2e",
    "processes": [
      {
        "process_id": "refining",
        "direct_kg_co2e": "7160.000000000",
        "embedded_kg_co2e": "0.000000000",
        "total_kg_co2e": "7160.000000000",
        "products": [
          {
            "process_id": "refining",
            "product_id": "diesel",
            "product_key": "refining|diesel",
            "quantity": "3000", "unit": "L",
            "allocation_method": "economic",
            "allocation_share": "0.750000000000",
            "allocation_weight": "6000", "allocation_weight_unit": "currency",
            "allocated_kg_co2e": "5370.000000000",
            "unit_kg_co2e": "1.790000000",
            "contribution_tree": {
              "kind": "product", "process_id": "refining", "product_id": "diesel",
              "allocation": "economic", "allocation_share": "0.750000000000",
              "kg_co2e": "5370.000000000",
              "children": [
                {"kind": "material", "material_id": "m_crude", "factor_key": "crude_oil",
                 "factor_version": "v2024", "reference_unit": "kg",
                 "kg_co2e_per_unit": "0.45", "input_quantity": "10", "input_unit": "t",
                 "kg_co2e": "3375.000000000", "children": []},
                {"kind": "material", "material_id": "m_elec",
                 "factor_key": "electricity.grid", "factor_version": "v2024",
                 "reference_unit": "kWh", "kg_co2e_per_unit": "0.42",
                 "input_quantity": "5000", "input_unit": "kWh",
                 "kg_co2e": "1575.000000000", "children": []},
                {"kind": "material", "material_id": "m_gas", "factor_key": "natural_gas",
                 "factor_version": "v2024", "reference_unit": "MJ",
                 "kg_co2e_per_unit": "0.056", "input_quantity": "10000", "input_unit": "MJ",
                 "kg_co2e": "420.000000000", "children": []}
              ]
            },
            "contribution_chain": [
              {"source_process_id": "refining", "material_id": "m_crude",
               "factor_key": "crude_oil", "factor_version": "v2024",
               "kg_co2e_per_unit": "0.45", "input_quantity": "10", "input_unit": "t",
               "kg_co2e": "3375.000000000",
               "chain": [{"kind": "process", "process_id": "refining",
                          "exit_product_id": "diesel", "share": "0.750000000000"}]}
            ]
          },
          {
            "product_id": "naphtha", "product_key": "refining|naphtha",
            "allocation_share": "0.250000000000",
            "allocated_kg_co2e": "1790.000000000",
            "unit_kg_co2e": "0.895000000"
          }
        ],
        "evidence": [
          {"type": "factor", "material_id": "m_crude", "factor_key": "crude_oil",
           "factor_version": "v2024", "reference_unit": "kg",
           "kg_co2e_per_unit": "0.45", "input_quantity": "10", "input_unit": "t",
           "kg_co2e": "4500.000000000"},
          {"type": "upstream_transfer", "...": "下游过程的转移证据见 blending"}
        ]
      },
      {
        "process_id": "blending",
        "direct_kg_co2e": "420.000000000",
        "embedded_kg_co2e": "2685.000000000",
        "total_kg_co2e": "3105.000000000",
        "products": [
          {
            "product_id": "blended-diesel",
            "product_key": "blending|blended-diesel",
            "quantity": "1500", "unit": "L",
            "allocation_share": "1.000000000000",
            "allocated_kg_co2e": "3105.000000000",
            "unit_kg_co2e": "2.070000000",
            "contribution_chain": [
              {"source_process_id": "refining", "material_id": "m_crude",
               "kg_co2e": "1687.500000000",
               "chain": [
                 {"kind": "transfer", "process_id": "refining", "to_process": "blending",
                  "input_id": "i_diesel", "product_id": "diesel",
                  "share": "0.500000000000"},
                 {"kind": "process", "process_id": "blending",
                  "exit_product_id": "blended-diesel", "share": "1.000000000000"}
               ]}
            ]
          }
        ]
      }
    ]
  }
}
```

核对：炼厂直接排放 = 10,000 kg × 0.45 + 5,000 × 0.42 + 10,000 × 0.056 = **7,160**；柴油分摊 75% = **5,370**（1.79 kg/L）；掺混工序继承 5,370 × (1,500/3,000) = 2,685，加自身 420，得 **3,105 / 1,500 L = 2.07 kg/L**。

再发一次完全相同的请求（幂等）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://localhost:8000/accounting/submissions \
  -H 'Content-Type: application/json' --data @examples/submission.json
# 200   （首次为 201），且 snapshot_id 不变、reused=true
```

### 3.3 查询快照与单产品

```bash
curl -s http://localhost:8000/accounting/snapshots | python -m json.tool
curl -s http://localhost:8000/accounting/snapshots/snp_3e1a4a0a0aea4cc2
curl -s "http://localhost:8000/accounting/snapshots/snp_3e1a4a0a0aea4cc2/products/blending|blended-diesel"
```

```json
{
  "snapshot_id": "snp_3e1a4a0a0aea4cc2",
  "process": "blending",
  "product": {
    "product_id": "blended-diesel",
    "product_key": "blending|blended-diesel",
    "unit_kg_co2e": "2.070000000",
    "allocated_kg_co2e": "3105.000000000",
    "contribution_tree": { "...": "完整证据树" },
    "contribution_chain": [ { "...": "扁平化 origin→sink 链" } ]
  }
}
```

### 3.4 因子更新：创建新版本（历史冻结，拒绝改写）

内置 `v2024` 已冻结。直接改写因子会被拒绝：

```bash
curl -s -X POST http://localhost:8000/factor-versions/v2024/factors \
  -H 'Content-Type: application/json' \
  -d '{"key":"electricity.grid","kg_co2e_per_unit":"9.99","reference_unit":"kWh"}'
```

```json
{
  "error": {
    "type": "conflict",
    "details": [
      {"code": "FACTOR_VERSION_FROZEN",
       "path": ".version:v2024",
       "message": "factor version 'v2024' is frozen; register a new version instead of changing historical factors"}
    ]
  }
}
```

用克隆 + 覆盖的方式发布 **`v2025`**（电网变清洁、原油变高碳），见 [`examples/clone_v2025.json`](examples/clone_v2025.json)：

```bash
curl -s -X POST http://localhost:8000/factor-versions/v2024/clone \
  -H 'Content-Type: application/json' --data @examples/clone_v2025.json
```

```json
{"new_version":"v2025","cloned_from":"v2024","factors":8,"overridden":2}
```

也可以用 `POST /factor-versions` 建全新版本，再用 `POST /factor-versions/{version}/factors` 逐个登记因子（新版本未冻结时可增改）。

### 3.5 用新因子重算并获取逐产品差异

```bash
SNAP=snp_3e1a4a0a0aea4cc2
curl -s -X POST http://localhost:8000/accounting/snapshots/$SNAP/recompute \
  -H 'Content-Type: application/json' \
  -d '{"default_factor_version":"v2025"}'
```

```json
{
  "snapshot_id": "snp_c7557499f56e4864",
  "prior_snapshot_id": "snp_3e1a4a0a0aea4cc2",
  "reused": false,
  "immutable": true,
  "factor_versions": {"refining.m_crude": "v2025", "...": "全部切到 v2025"},
  "result": { "...": "按 v2025 重算的完整结果" },
  "diff_vs_previous": {
    "product_count_old": 3,
    "product_count_new": 3,
    "allocated_kg_co2e_old": "10265.000000000",
    "allocated_kg_co2e_new": "10007.500000000",
    "allocated_delta_kg_co2e": "-257.500000000",
    "products": [
      {"product_key": "blending|blended-diesel", "status": "changed",
       "unit_kg_co2e_old": "2.070000000", "unit_kg_co2e_new": "1.965000000",
       "unit_delta_kg_co2e": "-0.105000000", "unit_delta_percent": "-5.072463768116",
       "allocated_delta_kg_co2e": "-157.500000000"},
      {"product_key": "refining|diesel", "status": "changed",
       "unit_kg_co2e_old": "1.790000000", "unit_kg_co2e_new": "1.765000000",
       "unit_delta_kg_co2e": "-0.025000000", "unit_delta_percent": "-1.396648044693",
       "allocated_delta_kg_co2e": "-75.000000000"},
      {"product_key": "refining|naphtha", "status": "changed",
       "unit_kg_co2e_old": "0.895000000", "unit_kg_co2e_new": "0.882500000",
       "unit_delta_kg_co2e": "-0.012500000", "unit_delta_percent": "-1.396648044693",
       "allocated_delta_kg_co2e": "-25.000000000"}
    ]
  }
}
```

原快照仍然是 7,160 / 2.07，未被改写；重算同样幂等（再调一次返回 `200 reused=true`）。也可以只对个别过程换版本：

```bash
curl -s -X POST http://localhost:8000/accounting/snapshots/$SNAP/recompute \
  -H 'Content-Type: application/json' \
  -d '{"factor_versions":{"refining":"v2025"}}'
```

### 3.6 稳定错误明细

所有 4xx 使用统一结构，`details` 按 `(path, code)` 排序，相同请求字节级稳定，HTTP 状态码：`422` 校验/模式错误、`404` 未找到、`409` 冲突（冻结/重复版本）。

```bash
curl -s -X POST http://localhost:8000/accounting/submissions \
  -H 'Content-Type: application/json' -d '{"default_factor_version":"v2024","processes":[
    {"id":"A","inputs":[{"id":"i","from_process":"B","product_id":"y","quantity":"1","unit":"kg"}],
     "outputs":[{"id":"o","product_id":"x","quantity":"1","unit":"kg"}]},
    {"id":"B","inputs":[{"id":"i","from_process":"A","product_id":"x","quantity":"1","unit":"kg"}],
     "outputs":[{"id":"o","product_id":"y","quantity":"1","unit":"kg"}]}]}'
```

```json
{
  "error": {
    "type": "validation_error",
    "details": [
      {"code": "CYCLE_DETECTED", "path": ".processes",
       "message": "product flow graph contains a cycle: A -> B -> A"}
    ]
  }
}
```

其他错误码：`NEGATIVE_AMOUNT`、`NON_POSITIVE_AMOUNT`、`UNKNOWN_UNIT`、
`MATERIAL_FACTOR_DIMENSION_MISMATCH`、`INPUT_UNIT_DIMENSION_MISMATCH`、
`MISSING_FACTOR_VERSION`、`FACTOR_VERSION_NOT_FOUND`、`FACTOR_NOT_FOUND`、
`UPSTREAM_PROCESS_NOT_FOUND`、`UPSTREAM_PRODUCT_NOT_FOUND`、
`ALLOCATION_METHOD_MISSING`、`ALLOCATION_WEIGHT_MISSING`、
`ALLOCATION_UNIT_MISMATCH`、`ALLOCATION_DENOMINATOR_ZERO`、
`ECONOMIC_UNIT_MISMATCH`、`DUPLICATE_ID`、`INVALID_DECIMAL`、
`UNKNOWN_FIELD`、`MISSING_FIELD` 等。

---

## 4. HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/docs`, `/redoc`, `/openapi.json` | 交互式文档 |
| POST | `/factor-versions` | 新建（开放）因子版本 |
| GET | `/factor-versions` | 版本列表（含 frozen 标记） |
| POST | `/factor-versions/{v}/factors` | 在开放版本中登记/更新因子（冻结版本返回 409） |
| GET | `/factor-versions/{v}/factors` | 列出某版本全部因子 |
| POST | `/factor-versions/{v}/clone` | 克隆版本到新版本并覆盖部分因子（因子更新的推荐路径） |
| POST | `/accounting/submissions` | 校验 + 核算 + 存不可变快照（201 新建 / 200 幂等命中） |
| GET | `/accounting/snapshots` | 快照列表 |
| GET | `/accounting/snapshots/{id}` | 快照完整内容（输入摘要、因子版本、结果） |
| GET | `/accounting/snapshots/{id}/products/{process_id}\|{product_id}` | 单产品足迹切片 |
| POST | `/accounting/snapshots/{id}/recompute` | 用新因子版本生成新快照并返回逐产品 diff |

> 提示：产品键格式为 `过程id|产品id`，URL 中 `|` 建议编码为 `%7C` 或用引号包裹整个 URL。

## 5. 设计要点

- **Decimal 端到端**：Pydantic 以 `Decimal` 解析字符串入参，引擎在 50 位精度上下文运算，证据行保留因子精确值，响应定点 9 位（份额 12 位）。
- **确定性**：拓扑序、错误列表、叶子贡献链、JSON 快照均按稳定键排序；同一请求输出可重现。
- **快照即合同**：每个快照保存规范化输入摘要、SHA-256 内容哈希、原始请求、实际所用因子版本映射和完整结果；无任何 UPDATE 路径。
- **因子治理**：因子版本可冻结；更新因子 = 克隆新版本（`/clone`），再对历史快照 `/recompute`，新老结果通过 `prior_snapshot_id` 串联对比。
