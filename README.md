# 港口危险品配载预审 API

纯后端 Python 服务：接收一个舱位与 2–20 个货项，枚举全部无向货项对并依据
固定隔离规则给出同舱结论。裁决完全由本仓库的规则引擎（`app/rules.py`）
计算，不依赖任何外部服务；相同货项以任意次序提交，结论与依据字节级一致。

## 受理的类别子集

本服务只受理以下 6 个危险品类别（规则表对该子集闭合），其他类别一律整体拒绝：

| 类别   | 含义                   |
| ------ | ---------------------- |
| `FLAM` | 易燃品                 |
| `OXID` | 氧化剂                 |
| `TOX`  | 有毒品                 |
| `CORR` | 腐蚀品                 |
| `WET`  | 遇湿反应品             |
| `GAS`  | 压缩/液化气体          |

## 裁决规则（无向配对）

规则两端不区分顺序，同类之间允许：

| 配对                | 裁决        |
| ------------------- | ----------- |
| FLAM–OXID           | 禁止（FORBID） |
| FLAM–GAS            | 禁止（FORBID） |
| WET–CORR            | 禁止（FORBID） |
| TOX–FLAM            | 隔板（PARTITION） |
| TOX–OXID            | 隔板（PARTITION） |
| CORR–GAS            | 隔板（PARTITION） |
| 其余配对（含同类）  | 允许（ALLOW）  |

舱位级结论按优先级归并：任一配对命中禁止规则 → `FORBID`；否则任一配对命中
隔板规则 → `PARTITION`；否则 → `ALLOW`。

## 依据（evidence）排序

- 枚举所有不同货项构成的无向对，仅命中规则（FORBID / PARTITION）的对进入依据；
- 每对内部按货项编号先小后大排列（字符串按字典序）；
- 依据列表按（首编号, 次编号）升序；
- 因此录入顺序不影响输出，响应体字节级一致。

## 接口

### `POST /api/v1/stowage/assess`

请求体：

```json
{
  "hold": "HOLD-3",
  "items": [
    {"id": "C330", "category": "WET"},
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"}
  ]
}
```

- `hold`：非空字符串，空舱位（缺失、空串、纯空白）整体拒绝；
- `items`：2–20 个货项，`id` 为非空字符串且全舱唯一，`category` 须属于上述类别子集。

成功响应 `200`：

```json
{"hold":"HOLD-3","conclusion":"FORBID","evidence":[{"first":"C101","second":"C205","firstCategory":"FLAM","secondCategory":"OXID","rule":"FORBID"}]}
```

错误响应 `400`（代码稳定，可用于程序化处理）：

```json
{"error":{"code":"UNKNOWN_CATEGORY","message":"Item 'B2' has unknown category 'RADIO'; expected one of ['CORR', 'FLAM', 'GAS', 'OXID', 'TOX', 'WET']."}}
```

| 错误代码                  | 含义                                   |
| ------------------------- | -------------------------------------- |
| `INVALID_JSON`            | 请求体不是合法 JSON                    |
| `INVALID_REQUEST`         | 顶层结构或 `items` 结构非法            |
| `EMPTY_HOLD`              | 舱位缺失、为空或纯空白                 |
| `ITEM_COUNT_OUT_OF_RANGE` | 货项数量不在 2–20 之间                 |
| `INVALID_ITEM_ID`         | 货项编号缺失、为空或非字符串           |
| `DUPLICATE_ITEM_ID`       | 货项编号重复                           |
| `UNKNOWN_CATEGORY`        | 类别不在受理子集内                     |

### `POST /api/v1/stowage/removal-impact`

单件移除影响分析：当预审结论为禁止或需隔板时，快速判断卸下哪一件货最能
改善结果。请求体与 `assess` 完全相同（`hold` + `items`），校验规则与错误
代码也完全相同；任何非法输入整体拒绝，不产生部分分析。

成功响应 `200`：先给出原始裁决，再按货项编号升序列出卸下每件货后的结论
与依据，最后给出推荐编号：

```json
{
  "hold": "HOLD-3",
  "original": {
    "conclusion": "FORBID",
    "evidence": [
      {"first": "A1", "second": "B2", "firstCategory": "FLAM", "secondCategory": "GAS", "rule": "FORBID"},
      {"first": "B2", "second": "C3", "firstCategory": "GAS", "secondCategory": "CORR", "rule": "PARTITION"}
    ]
  },
  "removals": [
    {"removed": "A1", "conclusion": "PARTITION", "evidence": [
      {"first": "B2", "second": "C3", "firstCategory": "GAS", "secondCategory": "CORR", "rule": "PARTITION"}
    ]},
    {"removed": "B2", "conclusion": "ALLOW", "evidence": []},
    {"removed": "C3", "conclusion": "FORBID", "evidence": [
      {"first": "A1", "second": "B2", "firstCategory": "FLAM", "secondCategory": "GAS", "rule": "FORBID"}
    ]}
  ],
  "recommendations": ["A1", "B2"]
}
```

- `original`：未移除任何货项时的裁决，结构与 `assess` 响应一致；
- `removals`：按货项编号升序，每条记录卸下该货后的结论与依据；移除后
  只剩一件货时没有配对，按 `ALLOW` 处理；
- `recommendations`：能把严重程度降级的货项编号（禁止→隔板或允许、
  隔板→允许），按字典序排列；无改善时为空列表。

分析复用与预审相同的校验与裁决函数；相同货项以任意次序提交，完整响应
字节级一致。

### `POST /api/v1/stowage/reviews`

交接审核的第一步：建立 `revision=1` 的草稿（DRAFT）。请求体在 `assess`
的基础上增加一个幂等键 `commandId`：

```json
{
  "hold": "HOLD-3",
  "commandId": "handover-2026-09-17-0001",
  "items": [
    {"id": "C330", "category": "WET"},
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"}
  ]
}
```

成功响应 `201`：保存规范化请求（货项按编号排序）与复用规则引擎得到的
裁决，后续重试原样回放这些字节：

```json
{
  "reviewId": "9f1c…",
  "revision": 1,
  "status": "DRAFT",
  "commandId": "handover-2026-09-17-0001",
  "hold": "HOLD-3",
  "items": [
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"},
    {"id": "C330", "category": "WET"}
  ],
  "conclusion": "FORBID",
  "evidence": [
    {"first": "C101", "second": "C205", "firstCategory": "FLAM",
     "secondCategory": "OXID", "rule": "FORBID"}
  ]
}
```

- 货项、舱位的校验规则与错误代码与 `assess` 完全相同，非法输入整体
  拒绝，不产生草稿；
- `commandId` 必须为非空字符串（否则 `INVALID_COMMAND_ID`）。

### `POST /api/v1/stowage/reviews/{reviewId}/commands`

在草稿上下达命令，把审核推进到已确认记录。命令有两种：

- `REPLACE_ITEMS`：替换整舱货项，复用与 `assess` 完全相同的校验与裁决，
  成功后 `revision` 加一；
- `CONFIRM`：确认当前版本，`status` 变为 `CONFIRMED` 并冻结快照
  （不推进版本号）。

两种命令都必须携带幂等键 `commandId` 与乐观锁 `expectedRevision`：

```json
{
  "commandId": "handover-2026-09-17-0002",
  "action": "REPLACE_ITEMS",
  "expectedRevision": 1,
  "items": [
    {"id": "A1", "category": "TOX"},
    {"id": "B2", "category": "FLAM"}
  ]
}
```

成功响应 `200`，结构与建草稿响应一致（反映命令后的最新版本）。

#### 幂等与判重（commandId）

每个成功命令都按 `commandId` 全局判重，且失败的命令不占用标识：

- **同标识同内容重放**：返回首次成功时的状态码与响应体，字节级一致
  （货项按编号规范化，录入次序不同也算同内容）；即使审核已被后续命令
  推进到更新版本，旧命令的重放仍返回它首次产生的那一版结果；
- **同标识不同内容**：返回 `409 COMMAND_ID_REUSED`。因此版本冲突的
  命令可以修正 `expectedRevision` 后用同一个 `commandId` 重试。

判重先于一切状态检查执行。

#### 新命令的错误顺序

对未见过的 `commandId`，按固定顺序报错，便于程序化处理：

| 顺序 | HTTP | 错误代码 | 含义 |
| ---- | ---- | -------- | ---- |
| 1 | 404 | `REVIEW_NOT_FOUND` | `reviewId` 不存在 |
| 2 | 409 | `REVISION_CONFLICT` | `expectedRevision` 与当前版本不一致 |
| 3 | 409 | `REVIEW_FINALIZED` | 审核已确认，快照冻结 |

确认后任何携带当前版本号的新命令（无论替换还是再确认）都得到
`REVIEW_FINALIZED`；携带错误版本号则先得到 `REVISION_CONFLICT`。

命令体自身的校验错误仍为 `400`，代码包括 `INVALID_COMMAND_ID`、
`INVALID_ACTION`（`action` 非 `REPLACE_ITEMS`/`CONFIRM`）、
`INVALID_REVISION`（`expectedRevision` 非正整数），以及替换货项时与
`assess` 相同的全部货项错误代码。

#### 并发语义

判重检查、版本检查与写入在服务端一次原子完成。多个命令争用同一版本时
只有一个成功：版本号随即递增（替换）或审核被冻结（确认），其余命令得到
`409` 且不留下任何部分状态。冲突方读取响应中的最新版本（或改用自己的
`commandId` 重试），以新版本号重新提交即可。

### `GET /health`

健康检查，返回 `{"status": "ok"}`。

### 示例

```bash
curl -s -X POST http://localhost:8000/api/v1/stowage/assess \
  -H 'Content-Type: application/json' \
  -d '{"hold":"HOLD-3","items":[{"id":"C101","category":"FLAM"},{"id":"C205","category":"OXID"}]}'
# {"hold":"HOLD-3","conclusion":"FORBID","evidence":[{"first":"C101","second":"C205","firstCategory":"FLAM","secondCategory":"OXID","rule":"FORBID"}]}
```

## 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（覆盖全部 21 种类别配对、规则优先级、依据排序与排列不变性）：

```bash
pytest
```

## Docker Compose

Compose 只运行 API 一个常驻服务；宿主端口由环境变量 `API_PORT` 覆盖（默认 8000）：

```bash
API_PORT=9000 docker compose up --build api
# 服务监听在宿主的 9000 端口
```

一次性验收服务 `verify`：等待 API 健康后，先跑完整 pytest 判据，再对运行中的
API 做 HTTP 级验收（三种结论、排列字节级一致、各类错误代码），随后退出：

```bash
docker compose up --build --exit-code-from verify verify
# 或：docker compose run --rm verify
```

`verify` 以退出码报告结果：全部通过为 0，任一检查失败为 1。

## 项目结构

```
app/
  main.py        # FastAPI 入口与错误处理
  rules.py       # 规则引擎：配对裁决、舱位级归并与单件移除影响分析
  validation.py  # 请求校验与稳定错误代码
  reviews.py     # 交接审核：草稿/命令/确认的版本化、判重与并发原子存储
tests/
  test_rules.py    # 全部配对、优先级、依据排序、移除分析与排列不变性
  test_api.py      # assess / removal-impact 的 HTTP 行为与字节级一致
  test_reviews.py  # 草稿/命令/确认、判重重放、冲突重试与并发原子性
verify/
  acceptance.py  # 对运行中实例的一次性 HTTP 验收
Dockerfile
docker-compose.yml
requirements.txt
pytest.ini
```
