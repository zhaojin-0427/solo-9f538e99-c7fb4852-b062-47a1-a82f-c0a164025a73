# 每日雨量可信汇总 API

面向气象数据员的雨量站断网补传 / 更正服务。批量接收累计雨量观测，并维护
可追溯的每日雨量。使用 FastAPI + SQLite，提供 Docker Compose 启动方式。

## 快速开始

### Docker Compose

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，SQLite 数据存放在命名卷 `rainfall-data`
（容器内 `/data/rainfall.db`），重建容器不会丢数据。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
DATABASE_PATH=./data/rainfall.db uvicorn app.main:app --reload
```

交互式文档：`http://localhost:8000/docs`。

## 数据模型

每条观测包含：

| 字段 | 说明 |
| --- | --- |
| `observation_id` | 观测 ID，在站点内唯一 |
| `station_id` | 站点 ID |
| `revision` | 修订号，从 1 开始，越高越新 |
| `observed_at` | 观测时刻，必须带时区偏移（如 `2026-11-01T12:00:00+08:00` 或 `...Z`） |
| `cumulative_mm` | 累计雨量（mm），非负 |
| `is_reset` | 是否复位。复位读数成为新的累计基准，本身不产生增量 |
| `is_retracted` | 是否撤回。撤回后该观测不参与任何计算 |

同时接受 snake_case、camelCase 和若干常见简写（`id` / `station` / `rev` /
`timestamp` / `cumulative` 等）。请求体可以是 `{"observations": [...]}`，
也可以直接是数组。

## 接口

### `POST /observations` — 批量上传

```bash
curl -sS -X POST http://localhost:8000/observations \
  -H 'content-type: application/json' \
  -d '{
        "observations": [
          {"observation_id": "O1", "station_id": "S1", "revision": 1,
           "observed_at": "2026-11-01T00:00:00Z", "cumulative_mm": 0.0},
          {"observation_id": "O2", "station_id": "S1", "revision": 1,
           "observed_at": "2026-11-01T12:00:00Z", "cumulative_mm": 4.2}
        ]
      }'
```

成功返回（HTTP 200）：

```json
{
  "accepted": 2,
  "created": 2,
  "updated": 0,
  "unchanged": 0,
  "results": [
    {"observation_id": "O1", "revision": 1, "status": "created"},
    {"observation_id": "O2", "revision": 1, "status": "created"}
  ],
  "affected_dates": ["2026-11-01"]
}
```

- `status` 为 `created` / `updated` / `unchanged`。
- `affected_dates` 列出因本批而重算的日期（含结果被删除的日期）。

批次被拒绝时返回 HTTP 409，`error.details` 给出逐条原因：

```json
{
  "error": {
    "code": "batch_rejected",
    "message": "The batch was rejected and no observations or daily results were changed.",
    "details": [
      {
        "code": "cumulative_regression_without_reset",
        "message": "cumulative rainfall decreased without a reset flag",
        "station_id": "S1",
        "observation_id": "O3",
        "cumulative_mm": "11",
        "previous_cumulative_mm": "12",
        "observed_at": "2026-11-01T12:00:00.000000+00:00"
      }
    ]
  }
}
```

载荷本身不合法（缺字段、时间无时区、负雨量等）返回 HTTP 422
（`request_validation_error`）。

### `GET /stations/{station_id}/daily/{YYYY-MM-DD}` — 日雨量

```bash
curl -sS http://localhost:8000/stations/S1/daily/2026-11-01
```

```json
{
  "station_id": "S1",
  "date": "2026-11-01",
  "rainfall_mm": 4.2,
  "observation_ids": ["O2"]
}
```

`observation_ids` 是参与该日计算、即贡献了差额的后一个读数。没有贡献读数的
日期返回 `0.0` 和空列表，而不是 404——"当天无雨"本身是可信结论。

等价的查询参数形式：`GET /daily?station_id=S1&date=2026-11-01`。

### `GET /stations/{station_id}/observations` — 观测当前状态

返回该站点每个观测的当前有效修订，用于追查来源。

### `POST /stations/merge` — 合并换编号的站点

雨量站更换编号后，旧编号的断网补报仍会到达。把旧站（`source`）并入新站
（`target`）后，两个编号共享同一份观测和日报：

```bash
curl -sS -X POST http://localhost:8000/stations/merge \
  -H 'content-type: application/json' \
  -d '{"source_station_id": "OLD1", "target_station_id": "NEW1"}'
```

成功返回（HTTP 200）：

```json
{
  "source_station_id": "OLD1",
  "target_station_id": "NEW1",
  "canonical_station_id": "NEW1",
  "already_merged": false,
  "source_observations": 2,
  "merged_observations": 4,
  "affected_dates": ["2026-11-01"]
}
```

- 两站的同一观测 ID 保留较高修订；同修订号且内容完全相同视为重复，只留
  一份。
- 合并成功后重算目标站日报（`affected_dates` 为发生变化的日期），旧编号
  成为**持久别名**：后续用任一编号上传、查询观测或日报，都作用于合并结果；
  响应中的 `station_id` 为合并后的目标编号。
- 同一对编号重试是幂等空操作：返回 `already_merged: true`，不重复迁移。
  反向提交同一对编号亦然。别名指向的站点再次并入其它站时，旧别名自动
  重定向到最新编号（别名不成链）。
- 也接受 `sourceStationId` / `targetStationId`、`old_station_id` /
  `new_station_id` 等写法。源与目标相同返回 422。

下列情况整次拒绝（HTTP 409，`error.code` 为 `merge_rejected`），两边观测、
日报和别名全部保持原样，修正后可直接重试：

- 同一观测 ID 在两站修订号相同但内容不同（`revision_conflict`）；
- 合并后同一时刻出现两条有效观测（`duplicate_active_observation`）；
- 合并后出现未标记复位的累计倒退
  （`cumulative_regression_without_reset`）。

### `GET /health`

健康检查。

## 业务规则

### 修订与幂等

- 更高修订号覆盖旧观测；更低修订号被忽略（返回 `unchanged`），不报错。
- 完全相同的重复上传不改变任何结果（`unchanged`，`affected_dates` 为空）。
  时间按 UTC 规范化后比较，因此 `08:00+08:00` 与 `00:00Z` 视为同一时刻。
- 同一批次内同一观测出现多次时，最高修订号生效；同修订号不同内容视为冲突。

### 整批拒绝（HTTP 409，数据库保持原样）

- **同修订号内容冲突**：已存修订与上传修订相同，但时间、累计值、复位或撤回
  标记不同。
- **同站同一时刻存在两条有效观测**：撤回的观测不计入。
- **未标记复位的累计值倒退**：在同一"复位到复位"区间内，后面的读数小于区间
  内此前任意有效读数（不只是紧邻前一条），即判为倒退。进入复位读数本身的
  下降是合法的。

批次内任一违规都会回滚整个事务，包括其它站点的正常数据：原观测和原日结果
全部保留。

### 日雨量计算

1. 取该站所有未撤回观测，按 UTC 时刻排序。
2. 相邻两个读数求差，差额计入**后一个**读数所在的 UTC 日。
3. `is_reset` 读数不产生差额，但成为下一段的基准；第一段的首个读数只建立
   基准。
4. 差额为 0 的读数不列入 `observation_ids`。
5. 累计值以十进制精确运算，避免 `0.1 + 0.2` 类浮点误差。

### 迟到 / 更正 / 撤回

任何一次成功上传都会重算受影响站点的全部日结果，因此在下列情况下，前后相邻
日期会一起更新：

- 补传迟到读数（拆分原有的一个差额）；
- 更正读数的时间（可能跨日移动）或累计值；
- 撤回读数（其前后两个读数直接相邻）。

重算后不再有任何贡献读数的日期会被删除，查询时表现为 0 mm。

### 站点合并与别名

- 合并与上传一样在单个 `BEGIN IMMEDIATE` 事务内完成：先取写锁再读两站
  状态，因此合并与上传并发时按提交顺序判定——先提交的上传会被一起迁移，
  后提交的上传自动落到别名指向的目标站。失败整体回滚，不会留下别名或
  部分日报。
- 上传在事务内解析别名，因此同一批次里混用新旧编号也会写到同一份数据上。
- 别名存于 `station_aliases` 表，随 SQLite 数据持久化，服务重启后仍然
  有效。

## 项目结构

```
app/
  main.py       FastAPI 应用、错误处理、路由（含站点合并）
  models.py     Pydantic 请求/响应模型与输入别名
  rainfall.py   校验、修订合并、站点合并、日雨量计算、事务
  db.py         SQLite 连接与 schema（观测、日报、站点别名）
  errors.py     批次拒绝与合并拒绝异常
tests/          端到端测试（FastAPI TestClient）
Dockerfile
docker-compose.yml
```

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

## 设计取舍

- **整批原子性**：先 `BEGIN IMMEDIATE` 再读校验，避免两个并发上传基于同一旧
  状态各自通过；任一违规即整体回滚。
- **按站点重算**：一次上传后重算该站所有日结果。数据量远小于查询量，换来
  迟到、更正、撤回、跨日移动、复位边界变化等情况的一致结果，无需维护增量
  依赖。
- **金额用十进制字符串存储**：重复上传的内容比对与日汇总都精确可复现。
