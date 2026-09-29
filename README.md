# JSON Schema 反例最小化 API

基于 **Python + FastAPI** 的确定性反例（counterexample）工具服务，提供四个接口：

- **`POST /minimize`**：给定 JSON Schema 和一个**未通过校验**的实例，做确定性
  delta debugging 最小化，锁定首个校验错误的关键字，只保留仍触发同一关键字的候选；
- **`POST /mutations`**：给定 JSON Schema 和一个**合法样例**，确定性生成覆盖
  `required` / `type` / `minItems` / `minLength` / 数值边界 / `enum` 等约束的
  **最小变异实例**，每个变异携带 JSON Pointer、预期触发关键字与可复现 seed；
- **`POST /schema-diff`**：给定**旧版、新版**两个 JSON Schema 与一个 JSON 实例，
  返回两版各自的通过/拒绝状态与首个错误；结果相反时沿用 `/minimize` 的确定性
  缩减步骤找出更小的**差异见证**（每个候选都必须保持两版通过/拒绝组合）；
- **`POST /import-preview`**：面向**数据导入预览**，给定 JSON Schema、待导入
  实例与校验次数上限，在严格受限的修复算子下做有界搜索，返回**修复状态**、
  **最佳候选实例**、按 **JSON Pointer** 标识的净改动，以及候选的**剩余错误**。

`/minimize` 的工作流程：

1. **锁定首个校验错误的关键字**（keyword，如 `required` / `type` / `minItems`）；
2. 用**确定性 delta debugging** 反复尝试：
   - 坍缩：同类型空值（`{}` / `[]` / `""`）与 `null`；
   - 简化标量：`false`、`0`、`±1`、按 2 次幂减半、字符串截到单字符；
   - 删除对象字段块（经典 Zeller ddmin，作用于键的有序序列）；
   - 删除数组连续区间块、删除字符串字符块（同一个 ddmin 内核）；
   - 递归处理剩余子节点，外层迭代到整棵树到达不动点；
3. **只保留仍触发同一关键字的候选**；如果候选变成合法实例、或首个错误换成了
   别的关键字，则丢弃该候选；
4. 返回最小实例、完整评估轨迹，以及是否因评估预算提前停止。

`/mutations` 的工作流程：

1. 校验样例必须**合法**（否则 422），随后沿 schema 的 applicator
   （`properties` / `patternProperties` / `additionalProperties` / `items` /
   `prefixItems` / `allOf` / 本地 `$ref`）与样例结构同步遍历；
2. 为每个适用的约束关键字生成确定性的候选变异（每个变异只改动**一个位置**，
   且取边界外最近值，例如 `minItems: 3` 截断到 2 个元素、`minimum: 5` 改为 4）；
3. 每个候选都用真实校验器验证：**只有首个校验错误的关键字与预期一致才被接受**
   （与 `/minimize` 的锁定语义一致），无法构造的关键字列入 `skipped`；
4. 同一变异实例去重，每个变异返回 JSON Pointer、预期触发关键字和可复现 seed。

`/schema-diff` 的工作流程：

1. 分别用旧版、新版 schema 对原实例各校验一次，返回两版的**通过/拒绝状态**与
   **首个错误**（关键字、JSON Pointer、错误信息）；
2. 两版结果相同（都通过或都拒绝）时**不缩减**，`minimal_instance` 为 `null`；
3. 两版结果相反（一通过、一拒绝）时，沿用 `/minimize` 的确定性缩减步骤
   （坍缩 → 标量阶梯 → 字段/区间/字符块 ddmin → 递归不动点），但“有趣性”
   判据换成 **候选必须保持原实例的 `(旧版状态, 新版状态)` 组合**——任一版的
   通过/拒绝发生翻转（包括两边都拒绝/都通过）即丢弃候选；
4. 返回最小差异见证、差异方向（`new_rejects` / `new_accepts`）、双版评估
   次数与预算中止标记。

`/import-preview` 的工作流程：

1. 先对待导入实例做一次完整校验：**原实例合法时不作任何修改**，状态为
   `valid`；schema 自身不合法或无法用于校验（坏 `$ref`、非法正则等）返回 422；
2. 仅为以下约束构造**单点改动**（每个改动只触及一个 JSON Pointer）：

   | 关键字 | 修复方式 |
   | --- | --- |
   | `required` | 缺失属性在对应子 schema（跟随本地 `$ref` / `allOf`）上声明了 `default` 时补入默认值；**没有默认值一律不补**，不凭空捏造必填值 |
   | `enum` / `const` | 替换为枚举/常量取值；`enum` 优先选与现值同 JSON 类型的成员（至多额外尝试一个非同型成员） |
   | `minimum` / `maximum` | 取边界本身；整型现值遇非整型边界时取最近的可通过整数（`ceil` / `floor`） |
   | `minLength` / `maxLength` | 重复末字符填充（空串用 `x` 填充）/ 截断到边界长度 |

   其他约束（`type` / `pattern` / `format` / `multipleOf` / `oneOf` /
   `not` / `if` …）**不构造改动**，但仍参与每个候选的完整校验——它们无法
   被消除时，候选不能标记成功；
3. 对多处错误做确定性的**最佳优先有界搜索**（按剩余错误逐项单点扩展）：
   - 每个候选都**重新全量校验**，候选实例按规范 JSON 序列化**去重**；
   - 择优顺序固定为 **剩余错误数升序 → 改动位置数升序 → 规范 JSON**；
   - 仅当某个候选的**全部**错误清零才标记 `repaired`；
4. **预算耗尽**（`max_evaluations`，含对原实例的首次校验）或**不存在可行改动**
   时，保留搜索过程中见到的最佳候选，状态为 `unresolved` 并用
   `unresolved_reason` 明确区分 `budget_exhausted` / `no_fix_available`。

整个过程不使用任何随机源，相同输入必然得到相同结果（依赖版本已锁定）。

---

## 一条命令启动

需要 Docker（含 Compose 插件）：

```bash
docker compose up --build -d
```

启动后服务监听：

- **默认访问地址**：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/healthz`
- 交互式 API 文档（Swagger UI）：`http://localhost:8000/docs`

停止：`docker compose down`；查看日志：`docker compose logs -f`。

---

## 必要配置

所有配置均通过环境变量提供，均有默认值，**零配置即可启动**：

| 环境变量                   | 默认值    | 说明                                                       |
| -------------------------- | --------- | ---------------------------------------------------------- |
| `HOST`                     | `0.0.0.0` | 容器内监听地址（一般无需修改）                             |
| `PORT`                     | `8000`    | 容器内监听端口；宿主机映射端口由 Compose 的 `PORT` 控制    |
| `DEFAULT_MAX_EVALUATIONS`  | `1000`    | 请求未显式指定 `max_evaluations` 时使用的默认评估次数      |
| `HARD_MAX_EVALUATIONS`     | `100000`  | 单次请求允许的评估次数硬上限（超过返回 422）               |

可复制 `.env.example` 为 `.env` 后按需修改，例如改用宿主机 9000 端口：

```bash
cp .env.example .env
echo "PORT=9000" >> .env
docker compose up --build -d   # 然后访问 http://localhost:9000
```

---

## API

### `POST /minimize`

请求体：

| 字段              | 类型             | 必填 | 说明                                                                 |
| ----------------- | ---------------- | ---- | -------------------------------------------------------------------- |
| `schema`          | object / boolean | 是   | JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）    |
| `instance`        | 任意 JSON        | 是   | 未通过该校验的 JSON 实例                                             |
| `max_evaluations` | integer          | 否   | 最大校验评估次数（**含对原实例的首次校验**），默认/上限见上表，≥ 1   |

`schema` 中的 `$schema` 用于自动识别 draft；缺省按 Draft 2020-12 处理。

#### 响应 `200`

| 字段                 | 说明                                                                 |
| -------------------- | -------------------------------------------------------------------- |
| `minimal_instance`   | 最小化后的实例                                                       |
| `original_instance`  | 原始实例（原样回显）                                                 |
| `target_keyword`     | 锁定的首个错误关键字；布尔 `false` schema 下为 `null`                |
| `evaluations`        | 实际消耗的评估次数                                                   |
| `stopped_by_budget`  | `true` 表示在到达不动点前因预算耗尽提前停止                          |
| `trace[]`            | 每一次评估的轨迹（见下）                                             |

`trace[]` 条目字段：

| 字段                | 说明                                                                 |
| ------------------- | -------------------------------------------------------------------- |
| `index`             | 评估序号，从 1 开始（第 1 条恒为 `lock`）                            |
| `op`                | 操作类型：`lock` / `collapse` / `scalar` / `ddmin-object` / `ddmin-array` |
| `path`              | 被修改节点的 JSON Pointer（根为 `""`）                               |
| `detail`            | 人类可读的操作描述（删了哪些字段/区间、标量如何替换等）              |
| `kept`              | 该候选是否仍触发锁定关键字并被接受为新的当前实例                     |
| `triggered_keyword` | 候选触发的首个错误关键字；候选合法时为 `null`                        |
| `error_path`        | 错误对象所在位置的 JSON Pointer                                      |
| `message`           | 校验器给出的错误信息                                                 |

#### 错误响应（均为 `422`）

- 原实例**已经合法**：

  ```json
  { "error": "instance_valid", "message": "提供的实例已通过 schema 校验，无需最小化；本接口只接受反例。" }
  ```

- schema 自身不合法：`{ "error": "invalid_schema", "message": "...", "path": [...] }`
- 请求体缺少必填字段、`max_evaluations` 越界等：FastAPI/Pydantic 标准 422 结构。

---

### `POST /mutations`

给定 JSON Schema 与一个**合法样例**，确定性生成覆盖各类约束关键字的最小变异实例。

请求体：

| 字段              | 类型             | 必填 | 说明                                                                 |
| ----------------- | ---------------- | ---- | -------------------------------------------------------------------- |
| `schema`          | object / boolean | 是   | JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）    |
| `instance`        | 任意 JSON        | 是   | **合法**样例（必须通过 schema 校验），作为所有变异的起点             |
| `seed`            | 任意 JSON        | 否   | 复现种子；相同 `(schema, instance, seed)` 必然得到相同响应           |
| `max_evaluations` | integer          | 否   | 最大校验评估次数（**含对样例的首次校验**），默认/上限见配置表，≥ 1   |

#### 覆盖的约束关键字

| 关键字                                                | 变异方式                                                     |
| ----------------------------------------------------- | ------------------------------------------------------------ |
| `required`                                            | 删除单个必填属性（每个必填属性一个变异）                     |
| `type`                                                | 替换为确定性的异型哨兵值（`null` / `true` / `0` / …）        |
| `enum` / `const`                                      | 替换为取值集合外的确定性哨兵值（优先同类型）                 |
| `minimum` / `maximum`                                 | 取边界外最近值（整型样例优先保持整型）                       |
| `exclusiveMinimum` / `exclusiveMaximum`（draft 6+）   | 取边界值本身；draft 4 布尔形式由 `minimum`/`maximum` 覆盖    |
| `multipleOf`                                          | 破坏整除性的最近值                                           |
| `minLength` / `maxLength`                             | 截断 / 填充到边界外一个字符                                  |
| `minItems` / `maxItems`                               | 截断 / 填充到边界外一个元素                                  |
| `minProperties` / `maxProperties`                     | 删 / 增属性到边界外一个（删除时优先保留 `required` 键）      |

遍历会跟随 `properties`、`patternProperties`、`additionalProperties`、`items`
（含旧版 tuple form）、`prefixItems`、`allOf` 与本地 `$ref`（`#` / `#/...`，带环保护）。
`pattern` 与 `uniqueItems` 识别但暂不支持构造，会列入 `skipped`；
`anyOf` / `oneOf` / `not` / `if` / `format` 等不参与遍历。

#### 响应 `200`

| 字段                | 说明                                                                       |
| ------------------- | -------------------------------------------------------------------------- |
| `mutations[]`       | 变异列表（见下），按确定性遍历顺序排列                                     |
| `skipped[]`         | 无法构造变异的关键字及原因（如 `minItems: 0`、被其他约束抢先拦截）         |
| `seed`              | 本次请求的 base seed（由 `(schema, instance, seed)` 派生的 16 位十六进制） |
| `evaluations`       | 实际消耗的校验评估次数                                                     |
| `stopped_by_budget` | `true` 表示因评估预算耗尽提前停止，剩余目标已进入 `skipped`                |

`mutations[]` 条目字段：

| 字段               | 说明                                                                       |
| ------------------ | -------------------------------------------------------------------------- |
| `index`            | 变异序号，从 1 开始                                                        |
| `pointer`          | 变异位置的 JSON Pointer（相对**原始**样例；`required` 指向被删除的属性）   |
| `expected_keyword` | 预期触发关键字：保证该变异的**首个**校验错误关键字与此一致                 |
| `instance`         | 变异后的完整实例（单点最小改动）                                           |
| `seed`             | 该变异的可复现 seed（16 位十六进制）；重新发送相同请求即可复现             |
| `detail`           | 人类可读的变异描述                                                         |
| `error_pointer`    | 首个校验错误所在位置的 JSON Pointer                                        |

`skipped[]` 条目字段：`pointer` / `keyword` / `reason`。

#### 错误响应（均为 `422`）

- 样例**未通过** schema 校验：

  ```json
  { "error": "instance_invalid", "message": "...", "keyword": "type", "instance_path": "/a" }
  ```

- schema 自身不合法或无法用于校验（如坏 `$ref`）：
  `{ "error": "invalid_schema", "message": "...", "path": [...] }`
- 请求体缺少必填字段、`max_evaluations` 越界等：FastAPI/Pydantic 标准 422 结构。

---

### `POST /schema-diff`

给定**旧版、新版**两个 JSON Schema 与一个 JSON 实例，对比两版的通过/拒绝
结果；结果相反时确定性缩减出更小的差异见证。

请求体：

| 字段              | 类型             | 必填 | 说明                                                                 |
| ----------------- | ---------------- | ---- | -------------------------------------------------------------------- |
| `old_schema`      | object / boolean | 是   | 旧版 JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）|
| `new_schema`      | object / boolean | 是   | 新版 JSON Schema（同上）                                             |
| `instance`        | 任意 JSON        | 是   | 待对比的 JSON 实例                                                   |
| `max_evaluations` | integer          | 否   | 最大双版评估次数（**含对原实例的首次双版校验**），默认/上限见配置表，≥ 1 |

> 一次“评估”指对一个候选在旧版、新版上**各执行一次完整 JSON 校验**；
> 缩减步骤与 `/minimize` 完全相同，唯一区别是候选的接受条件为
> **保持两版通过/拒绝组合**。

#### 响应 `200`

| 字段                 | 说明                                                                       |
| -------------------- | -------------------------------------------------------------------------- |
| `old` / `new`        | 两版对**原实例**的结果侧结构（见下）                                       |
| `diverges`           | 两版结果是否相反（一通过、一拒绝）                                         |
| `direction`          | 差异方向：`new_rejects`（旧通过/新拒绝）、`new_accepts`（旧拒绝/新通过）；不相反时为 `null` |
| `minimal_instance`   | 最小差异见证；两版结果相同（不缩减）时为 `null`                            |
| `original_instance`  | 原始实例（原样回显）                                                       |
| `evaluations`        | 实际消耗的双版评估次数                                                     |
| `stopped_by_budget`  | `true` 表示在到达不动点前因预算耗尽提前停止                                |
| `trace[]`            | 每一次双版评估的轨迹（见下）                                               |

`old` / `new` 侧结构字段：

| 字段         | 说明                                                            |
| ------------ | --------------------------------------------------------------- |
| `status`     | `pass` / `reject`                                               |
| `keyword`    | 首个错误关键字；该版通过时为 `null`（布尔 `false` schema 下亦为 `null`） |
| `error_path` | 首个错误对象所在位置的 JSON Pointer（根为 `""`），通过时为 `null` |
| `message`    | 校验器给出的错误信息，通过时为 `null`                           |

`trace[]` 条目字段：

| 字段    | 说明                                                                       |
| ------- | -------------------------------------------------------------------------- |
| `index` | 评估序号，从 1 开始（第 1 条恒为 `lock`）                                  |
| `op`    | 操作类型：`lock` / `collapse` / `scalar` / `ddmin-object` / `ddmin-array` / `ddmin-string` |
| `path`  | 被修改节点的 JSON Pointer（根为 `""`）                                     |
| `detail`| 人类可读的操作描述                                                         |
| `kept`  | 候选是否保持了原实例的两版通过/拒绝组合并被接受为新的当前实例              |
| `old` / `new` | 候选在两版上各自的侧结构（同上；若校验器对该候选无法运行则为 `status: "error"`） |

#### 错误响应（均为 `422`）

- 任一 schema 自身不合法或无法用于校验（如坏 `$ref`）：

  ```json
  { "error": "invalid_schema", "which": "new_schema", "message": "...", "path": [] }
  ```

  `which` 取值为 `old_schema` 或 `new_schema`，指明是哪一版有问题。
- 请求体缺少必填字段、`max_evaluations` 越界等：FastAPI/Pydantic 标准 422 结构。

---

### `POST /import-preview`

面向**数据导入预览**：在严格受限的修复算子下做有界搜索，给出最佳候选实例、
按 JSON Pointer 标识的净改动与候选的剩余错误。原实例合法时**不作任何修改**。

请求体：

| 字段              | 类型             | 必填 | 说明                                                                 |
| ----------------- | ---------------- | ---- | -------------------------------------------------------------------- |
| `schema`          | object / boolean | 是   | JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）    |
| `instance`        | 任意 JSON        | 是   | 待导入的 JSON 实例                                                   |
| `max_evaluations` | integer          | 否   | 最大校验评估次数（**含对原实例的首次校验**），默认/上限见配置表，≥ 1 |

#### 修复算子

只有 `required`（缺失属性且 schema 声明了 `default`）、`enum` / `const`、
`minimum` / `maximum`、`minLength` / `maxLength` 会被构造单点改动；其余约束
不构造改动但仍参与完整校验。`default` 的查找跟随属性子 schema 的本地
`$ref`（`#` / `#/...`）与 `allOf`，实例侧遍历 `properties` /
`patternProperties` / `additionalProperties` / `items` / `prefixItems` /
`allOf`（带环保护）。

#### 响应 `200`

| 字段                  | 说明                                                                       |
| --------------------- | -------------------------------------------------------------------------- |
| `status`              | `valid`（原实例合法，未改动）/ `repaired`（候选全部错误清零）/ `unresolved`（保留最佳候选但仍有错误） |
| `candidate_instance`  | 最佳候选实例（`valid` 时即原实例）                                         |
| `original_instance`   | 原始实例（原样回显）                                                       |
| `changes[]`           | 候选相对原实例的净改动，按 JSON Pointer 排序（见下）                       |
| `remaining_errors[]`  | 候选经**完整校验**后的剩余错误（`repaired` / `valid` 时为空）              |
| `evaluations`         | 实际消耗的完整校验次数（第 1 次为对原实例的校验）                          |
| `stopped_by_budget`   | `true` 表示因评估预算耗尽提前停止搜索                                      |
| `unresolved_reason`   | `unresolved` 时为 `budget_exhausted` 或 `no_fix_available`；其余状态为 `null` |

`changes[]` 条目字段：

| 字段       | 说明                                                                       |
| ---------- | -------------------------------------------------------------------------- |
| `op`       | `add`（补必填属性）/ `replace`（替换既有值）                               |
| `pointer`  | 改动位置的 JSON Pointer（根为 `""`；`add` 指向新属性，如 `/role`）         |
| `keyword`  | 该改动所针对的约束关键字：`required` / `enum` / `const` / `minimum` / `maximum` / `minLength` / `maxLength` |
| `detail`   | 人类可读的改动描述                                                         |
| `old`      | 原值；`add` 时为 `null`                                                    |
| `new`      | 新值（`required` 修复时为 schema 声明的 `default`）                        |

`remaining_errors[]` 条目字段：

| 字段            | 说明                                                          |
| --------------- | ------------------------------------------------------------- |
| `keyword`       | 错误关键字（布尔 `false` schema 下为 `null`）                 |
| `instance_path` | 错误对象所在位置的 JSON Pointer（根为 `""`）                  |
| `schema_path`   | 触发错误的 schema 关键字所在位置的 JSON Pointer               |
| `message`       | 校验器给出的错误信息                                          |

#### 搜索与择优

- 对每条当前错误尝试其单点改动，每个生成的候选都**重新执行一次完整 JSON 校验**；
- 候选实例按规范 JSON（键排序、无空白）**去重**；
- 状态比较顺序固定为 **剩余错误数 → 改动位置数 → 规范 JSON**，全程确定性；
- 同一 JSON Pointer 被多步修复连续触及时，`changes` 中合并为相对原实例的
  一条**净改动**（改动位置数按 Pointer 计，不按步骤数计）；
- 仅在某候选剩余错误为零时标记 `repaired`。

#### 错误响应（均为 `422`）

- schema 自身不合法或无法用于校验（如坏 `$ref`、非法正则）：
  `{ "error": "invalid_schema", "message": "...", "path": [] }`
- 请求体缺少必填字段、`max_evaluations` 越界等：FastAPI/Pydantic 标准 422 结构。

> 注意：原实例非法本身**不是**错误响应——无论能否修复都返回 `200`，
> 由 `status` / `unresolved_reason` 表达结果。

---

```bash
curl -s -X POST http://localhost:8000/minimize \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {"type":"object","required":["id"],"properties":{"id":{"type":"integer"}}},
    "instance": {"tags": ["aaaaaaaaaa", "bbbbbbbbbb"], "extra": {"deep": [1,2,3,4]}, "name": "hello-world"},
    "max_evaluations": 500
  }'
```

响应（节选）：空对象仍缺 `id`，故最小反例为 `{}`，全程仅 3 次评估：

```json
{
  "minimal_instance": {},
  "target_keyword": "required",
  "evaluations": 3,
  "stopped_by_budget": false,
  "trace": [
    {"index": 1, "op": "lock", "detail": "validate original instance", "kept": false, "triggered_keyword": "required"},
    {"index": 2, "op": "collapse", "detail": "collapse object(3) -> {}", "kept": true,  "triggered_keyword": "required"},
    {"index": 3, "op": "collapse", "detail": "collapse object(0) -> null", "kept": false, "triggered_keyword": "type"}
  ]
}
```

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/minimize",
    json={
        "schema": {"type": "array", "minItems": 3},
        "instance": [10, 20],          # 非法：只有 2 个元素
        "max_evaluations": 1000,
    },
)
print(r.status_code, r.json()["minimal_instance"])  # 200 []
```

### `/mutations` 示例

```bash
curl -s -X POST http://localhost:8000/mutations \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {"type":"object","required":["id"],"properties":{"id":{"type":"integer","minimum":1},"tags":{"type":"array","minItems":2}}},
    "instance": {"id": 10, "tags": ["a","b","c"]},
    "seed": "demo"
  }'
```

响应（节选）：`required` / `type` / `minimum` / `minItems` 各得到一个最小变异，
每个变异都保证首个校验错误关键字与 `expected_keyword` 一致：

```json
{
  "mutations": [
    {"index": 1, "pointer": "/id",   "expected_keyword": "required", "instance": {"tags": ["a","b","c"]},       "seed": "69810f73d39582b1", "detail": "remove required property 'id'", "error_pointer": ""},
    {"index": 3, "pointer": "/id",   "expected_keyword": "type",     "instance": {"id": null, "tags": ["a","b","c"]}, "seed": "0d42184afb4c5c15", "detail": "replace 10 with None",   "error_pointer": "/id"},
    {"index": 4, "pointer": "/id",   "expected_keyword": "minimum",  "instance": {"id": 0, "tags": ["a","b","c"]},    "seed": "5bc77c1122bf4513", "detail": "set to 0 (< minimum 1)", "error_pointer": "/id"},
    {"index": 6, "pointer": "/tags", "expected_keyword": "minItems", "instance": {"id": 10, "tags": ["a"]},           "seed": "890019db2adc303a", "detail": "truncate array 3 -> 1 items", "error_pointer": "/tags"}
  ],
  "skipped": [],
  "seed": "a264bd17a0ab56bd",
  "evaluations": 7,
  "stopped_by_budget": false
}
```

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/mutations",
    json={
        "schema": {"type": "string", "minLength": 3},
        "instance": "hello",           # 合法样例
        "seed": "my-run",
    },
)
for m in r.json()["mutations"]:
    print(m["pointer"], m["expected_keyword"], repr(m["instance"]), m["seed"])
#  type None <seed>
#  minLength 'he' <seed>
```

### `/import-preview` 示例

一条待导入记录同时缺一个带默认值的必填字段、名称超长、枚举值非法：

```bash
curl -s -X POST http://localhost:8000/import-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {
      "type": "object",
      "required": ["id", "name", "role"],
      "properties": {
        "id":   {"type": "integer", "default": 0},
        "name": {"type": "string", "maxLength": 4},
        "role": {"enum": ["admin", "user"]}
      }
    },
    "instance": {"name": "alice", "role": "root"},
    "max_evaluations": 50
  }'
```

响应（节选）：三处错误全部可修，候选经全量校验零错误，状态为 `repaired`，
净改动按 JSON Pointer 排序：

```json
{
  "status": "repaired",
  "candidate_instance": {"id": 0, "name": "alic", "role": "admin"},
  "changes": [
    {"op": "add",     "pointer": "/id",   "keyword": "required",  "old": null,  "new": 0,       "detail": "add missing required property 'id' with default 0"},
    {"op": "replace", "pointer": "/name", "keyword": "maxLength", "old": "alice", "new": "alic", "detail": "truncate string 5 -> 4 chars"},
    {"op": "replace", "pointer": "/role", "keyword": "enum",      "old": "root", "new": "admin", "detail": "replace 'root' with 'admin' (enum member, same type)"}
  ],
  "remaining_errors": [],
  "evaluations": 10,
  "stopped_by_budget": false,
  "unresolved_reason": null
}
```

缺少的必填属性**没有 `default`** 时不会凭空补值；若其余错误可修，最佳候选仍
保留已完成的修复，并在 `remaining_errors` 中明示未消除的错误（此例还混入一个
不可构造修复的 `type` 错误）：

```bash
curl -s -X POST http://localhost:8000/import-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {
      "type": "object",
      "required": ["a"],
      "properties": {"a": {"type": "integer", "default": 1},
                     "b": {"type": "string"}}
    },
    "instance": {"b": 5}
  }'
```

```json
{
  "status": "unresolved",
  "candidate_instance": {"a": 1, "b": 5},
  "changes": [
    {"op": "add", "pointer": "/a", "keyword": "required", "old": null, "new": 1}
  ],
  "remaining_errors": [
    {"keyword": "type", "instance_path": "/b", "schema_path": "/properties/b/type", "message": "5 is not of type 'string'"}
  ],
  "unresolved_reason": "no_fix_available"
}
```

预算只允许探索部分候选时，`unresolved_reason` 为 `budget_exhausted`，
`candidate_instance` 仍是已探索候选中剩余错误最少、改动位置最少的那个；
原实例本身合法时直接返回 `"status": "valid"` 且 `changes` 为空。

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/import-preview",
    json={
        "schema": {
            "type": "object",
            "required": ["age"],
            "properties": {"age": {"type": "integer", "minimum": 18}},
        },
        "instance": {"age": 5},
        "max_evaluations": 100,
    },
)
data = r.json()
print(data["status"])                       # repaired
print(data["candidate_instance"])           # {'age': 18}
print([c["pointer"] for c in data["changes"]])  # ['/age']
```

### `/schema-diff` 示例

旧版要求对象必须有 `id`，新版取消了该约束——原实例在两版下结果相反
（旧拒绝 / 新通过，`new_accepts`），缩减时每个候选都必须保持这一组合：

```bash
curl -s -X POST http://localhost:8000/schema-diff \
  -H 'Content-Type: application/json' \
  -d '{
    "old_schema": {"type":"object","required":["id"],"properties":{"id":{"type":"integer"}}},
    "new_schema": {},
    "instance": {"tags": ["aaaaaaaaaa", "bbbbbbbbbb"], "extra": {"deep": [1,2,3,4]}},
    "max_evaluations": 100
  }'
```

响应（节选）：空对象在旧版下仍缺 `id`、在新版下通过；进一步坍缩成 `null`
后旧版依旧拒绝、新版依旧通过，组合保持不变，故最小见证为 `null`，全程 3 次评估：

```json
{
  "old": {"status": "reject", "keyword": "required", "error_path": "", "message": "'id' is a required property"},
  "new": {"status": "pass", "keyword": null, "error_path": null, "message": null},
  "diverges": true,
  "direction": "new_accepts",
  "minimal_instance": null,
  "evaluations": 3,
  "stopped_by_budget": false
}
```

反方向的例子：旧版只要求数组，新版增加 `minItems: 3`。实例 `[10, 20]`
旧版通过、新版拒绝（`new_rejects`）；`[]` 仍保持该组合，而 `null` 会让
旧版也拒绝（组合被破坏），故最小见证是 `[]`：

```bash
curl -s -X POST http://localhost:8000/schema-diff \
  -H 'Content-Type: application/json' \
  -d '{
    "old_schema": {"type":"array"},
    "new_schema": {"type":"array","minItems":3},
    "instance": [10, 20]
  }'
```

两版结果相同（都通过或都拒绝）时不缩减：响应中 `diverges` 为 `false`、
`direction` 与 `minimal_instance` 均为 `null`，只消耗 1 次评估。

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/schema-diff",
    json={
        "old_schema": {"type": "object", "required": ["id"]},
        "new_schema": {"type": "object"},
        "instance": {"tags": ["a", "b"]},
    },
)
data = r.json()
print(data["old"]["status"], data["new"]["status"])  # reject pass
print(data["direction"], data["minimal_instance"])    # new_accepts None
```

---

## 算法说明（确定性保证）

1. **锁定**：首次校验取 `jsonschema` 校验器产出的第一个错误，记录其
   `validator`（即关键字）作为唯一"有趣性"判据。`/schema-diff` 不锁定
   关键字：首次在旧版、新版上各校验一次，以**两版通过/拒绝组合**为唯一
   判据，缩减过程中组合翻转的候选一律丢弃。
2. **遍历策略**：对每个节点依次执行「坍缩 → 标量阶梯 → 本层 ddmin → 递归子节点」。
   任何被接受的修改都严格减小实例的良基复杂度（容器元素数 / 标量量级），
   因此外层不动点迭代必然终止，不会出现 `1 ↔ -1` 之类的振荡。
3. **ddmin**：对象按键的插入序、数组/字符串按位置切成尽量等长的连续块，
   先逐块删除，再尝试只保留单个块，粒度 `n` 按经典算法
   `2 → … → 元素数` 自适应增减。
4. **预算**：每执行一次完整 JSON 校验计 1 次评估（`/schema-diff` 对一个候选
   做旧版、新版两次校验，合计为 1 次双版评估）。开始第
   `max_evaluations + 1` 次评估前立即停止并把 `stopped_by_budget` 置为 `true`，
   已接受的缩减全部保留。

注意：这里的"最小"是 **1-最小（1-minimal）** 意义上的最小——算法保证在其
操作算子下再删任何一个单元都会丢失目标关键字，但不保证是全局基数最小。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest httpx
python -m pytest -q
```

---

## 项目结构

```
.
├── app/
│   ├── main.py             # FastAPI 应用、请求/响应模型、配置
│   ├── minimizer.py        # 确定性 delta debugging 最小化内核
│   ├── differ.py           # 两版 schema 差异见证最小化（/schema-diff，复用 minimizer）
│   ├── mutator.py          # 确定性约束变异生成器（/mutations）
│   └── repairer.py         # 导入预览有界修复搜索（/import-preview）
├── tests/
│   ├── test_api.py         # /minimize 单元与端到端测试
│   ├── test_schema_diff.py # /schema-diff 单元与端到端测试
│   ├── test_mutations.py   # /mutations 单元与端到端测试
│   └── test_import_preview.py # /import-preview 单元与端到端测试
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── requirements.txt
```
