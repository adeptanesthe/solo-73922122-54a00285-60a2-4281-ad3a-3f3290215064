# 古籍 OCR 单行校对 API

FastAPI + SQLite 实现的单行 OCR 校对服务。OCR 对同一行给出若干候选词（带编号、
起止位置、文字、整数分数），服务负责：

1. 求**总分最高的合法读法**——从位置 0 出发，词与词首尾相接，恰好到行尾；
2. 按校对员给的**分差**标出**待核词**——只要存在一条距最高分不超过分差的合法
   读法不含某个词，该词就 `needs_review=true`，需要回看原图；
3. 支持逐词**确认**，确认后重算读法与待核标记；拼不成整行则判冲突并保留已确认项。

## 目录结构

```
app/            FastAPI 应用
  engine.py     核心算法：候选归一化、分段位置 DP 最优读法、分差待核判定
  db.py         SQLite 持久化（WAL、IMMEDIATE 写事务）
  models.py     Pydantic 请求/响应模型
  main.py       路由与事务编排（解码在写事务外，短事务内复核裁决）
tests/          pytest 自测（38 个用例，含稀疏行、冲突与并发）
demo/           演示请求：demo.sh / demo.py / requests.http
Dockerfile
docker-compose.yml
requirements.txt
```

## 解码性能与并发设计

- **遍历量与坐标跨度无关**：位置 DP 用最小堆只访问“有词可达”的位置，
  同一组候选的坐标等比例放大多少倍，访问的位置数都不变（稀疏行
  `line_end=4×10⁹` 与 `line_end=4` 的解码耗时相同）。最高分读法、
  同分按编号元组字典序取舍、分差待核的规则不变。
- **重算不阻塞其他行**：确认请求先在不持写锁的情况下按快照试算，
  只在很短的 `BEGIN IMMEDIATE` 事务内复核最新确认集并落库；一行
  重算读法和待核词期间，其他行的确认照常提交。若同一行有新的确认
  先提交，本次请求在写事务内按最新已确认词重新判断：能拼满整行就
  接受，拼不满则冲突，重复确认幂等。
- **同请求不重复解码**：每次请求内按“候选 + 已生效确认集”记忆解码
  结果，相同输入的完整解码最多做一次（建行探测即响应、确认试算即
  响应，冲突/重复时当前结果也只算一次）。

## 启动（Docker Compose）

```bash
docker compose up --build -d
docker compose logs -f api          # 查看日志
curl http://localhost:8000/health   # {"status":"ok"}
```

SQLite 数据文件位于容器内 `/data/proofreading.db`，通过命名卷 `proof-data`
持久化；可用环境变量 `PROOF_DB_PATH` 覆盖路径。

停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据卷
```

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx
PROOF_DB_PATH=./proof.db uvicorn app.main:app --reload
pytest -q
```

## API

### `POST /tasks` 建任务

请求字段：

| 字段 | 约束 |
| --- | --- |
| `line_end` | 行尾位置，非负整数；合法读法须从 0 首尾相接到此位置 |
| `score_gap` | 非负整数分差 |
| `candidates` | 1–200 个候选，每项含 `id`（任务内唯一）、`start`、`end`、`text`（非空）、`score`（整数，可为负） |

位置约束 `0 <= start < end <= line_end`。**起止位置和文字相同即为同一个词**：
分数也相同的重复项合并去重；分数不同则返回 422。建任务时若不存在能拼满整行
的读法，同样返回 422，不产生任务。

### `GET /tasks/{task_id}` 查询当前结果

基于全部已生效确认项计算，含 `revision`（已生效确认版本号）、候选列表
（带 `confirmed` 标记）、最高 `reading`（每个词带 `needs_review`）。

### `POST /tasks/{task_id}/confirmations` 确认一个词

请求体 `{"candidate_id": 3}`，响应 `status`：

- `accepted`：与已生效确认项能拼成整行，写入确认、`revision + 1`、重算；
- `duplicate`：该词已确认，幂等返回当前结果，`revision` 不变；
- `conflict`：加入后拼不成整行（位置重叠 `overlap` 或无法铺满
  `no_full_tiling`），**保留全部已确认项**，响应里同时给出 `conflict` 详情和
  当前结果。

旧结果上的确认与并发确认等价：服务先在写事务外按快照试算，再在单个
`BEGIN IMMEDIATE` 事务内重新读取当前已生效确认项复核，由 SQLite 写锁
串行化，不会丢失更新。

## 待核判定规则（分差语义）

对最高读法中的每个非确认词，在其所属自由段内禁用该词重新求最优：

- 禁用后拼不成整行 → 不存在缺少它的合法读法 → 不标待核；
- 否则得到不含它的最优替代总分 `alt`，当 `最高总分 - alt <= score_gap` 时
  标为待核。

已确认词把整行切成若干自由段，替代读法只能在段内重选，不能跨越确认词边界。
同分时按候选编号元组字典序取最小，保证结果确定。

## 演示

```bash
bash demo/demo.sh                 # curl 版，逐步打印每步响应
python3 demo/demo.py              # 标准库版，无需第三方依赖
# 或在支持 REST Client 的编辑器里直接执行 demo/requests.http
```

演示覆盖：建任务看待核标记 → 确认后重算 → 冲突确认被拒且保留已确认项 →
重复确认幂等 → 非法输入 422 → 稀疏行（坐标放大 10⁶ 倍，耗时不变）→
并发校对（同一行互斥确认只生效一个，多行并行确认互不阻塞）。

## 测试

```bash
pytest -q
```

38 个用例覆盖：DP 最优读法、分差边界（gap=0/1）、同分确定性、负分、
候选归一化、确认后重算、重叠/不可铺满两类冲突、重复确认幂等、404、
跨确认词边界的替代读法、多自由段待核计分、稀疏坐标下读法/待核不变、
解码遍历量不随坐标跨度增长、超大稀疏行（line_end=10⁹）解码、
建行/确认/重复/冲突各路径同请求不重复解码、重算期间其他行可确认、
旧快照请求按最新确认集重判，以及 10 线程真实 HTTP 并发确认
（仅 1 个 accepted，其余 duplicate/conflict，revision 只增 1）和
真实服务下稀疏行与多行并发校对。
