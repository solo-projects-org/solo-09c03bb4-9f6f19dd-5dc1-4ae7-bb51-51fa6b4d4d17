# 岸站 AIS 可信航迹（离线分析）

FastAPI + SQLite 网页应用：海事数据员上传两处岸站收取的 AIS 定位 CSV 原档，
系统按船生成可信航迹、隔离不可信电文，并支持下载可信航迹与对应原档。
原档与判定**同一批次同时发布**；并发上传、写盘失败或服务重启后，页面与下载
只会看到完整版本，失败批次不会覆盖此前可用的航迹。

值班员另可在**站日对账**页按岸站和 UTC 日期录入上游交接清单的预期原档
SHA-256，与已发布批次逐档对账（缺档、清单外档、重复收档、内容合格性），
只有清单与实收**一一对应且每档内容合格**时才显示可交接。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://127.0.0.1:8000
```

数据保存在命名卷 `ais-data` 中（`/data/ais.sqlite3` 与 `/data/originals/`）。
`./samples` 以只读方式挂载进容器为 `/samples`，便于在容器内直接取样例文件：

```bash
# 宿主机上传样例（两个批次，验证重投、冲突、迟到点）
curl -F 'file=@samples/batch1_base.csv' http://127.0.0.1:8000/upload
curl -F 'file=@samples/batch2_late_arrivals.csv' http://127.0.0.1:8000/upload
```

停止并保留数据：`docker compose down`；连同卷一起删除：`docker compose down -v`。

> 若本机没有 Docker（本开发环境就没有），可直接用 venv 运行，见下节；
> 两种方式使用完全相同的代码、环境变量与数据布局。

### 本地 venv

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt      # 运行
pip install -r requirements-dev.txt  # 另加测试依赖
uvicorn app.main:app --reload --port 8000
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIS_DATA_DIR` | `./data` | SQLite 与 `originals/` 根目录 |
| `AIS_MAX_UPLOAD_BYTES` | 67108864 (64 MiB) | 单次上传上限，超限直接拒绝且不改动航迹 |
| `AIS_PORT` | 8000 | 仅 Compose 使用的宿主端口 |

## 输入格式

CSV 至少包含五列（表头大小写/别名均可，支持 `station/shore_station`、`utc/utc_seconds`、
`lat/latitude`、`lon/lng/longitude`），分隔符自动识别 `, ; \t |`：

```csv
station,mmsi,utc,lat,lon
SZ01,413000001,1767225600,22.500000,114.200000
```

* `utc` 为 UTC 秒（Unix epoch，2000–2100 年之间）；
* `mmsi` 为纯数字；
* 纬/经度有限且在合法范围内，且不是 (0,0)；
* 坏行不会中断整批，而是逐行记录在批次页的“解析拒绝行”中。

## 判定规则（`app/ais.py`）

按 MMSI 全量重算，与批次到达顺序无关：

1. **同秒同位置（重投）**：同一船同一 UTC 秒内所有候选位置一致时，只保留一个
   可信候选（先到者胜），其余标记 `duplicate` 隔离。重投不会制造伪轨迹。
2. **同秒异位（冲突）**：该秒只要出现两个不同位置，**该秒所有记录**都标记
   `conflict` 隔离。
3. **速度门限**：候选按 UTC 排序，与**上一可信点**计算的航速超过 **60 节**即
   标记 `speed_exceeded` 隔离；被隔离的点不参与后续“上一可信点”的计算。
4. **迟到电文**：后到的早期记录会触发该船**全部已发布行**的重算，旧点的
   可信/隔离结论及后续速度基准都可能改变；每次重算生成新的船舶 `version`。
5. 位置按 6 位小数（约 0.11 m）比较，恰好覆盖同一次定位的重投抖动。

## 发布与一致性（`app/store.py` / `app/db.py`）

* 原档先写入 `originals/.staging-*/` 并 `fsync`，再原子 `rename` 到
  `originals/<batch_id>/original.csv`，随后才在**单个 `BEGIN IMMEDIATE` 事务**
  中写入批次、原始行、拒绝行、每船判定与可信点并提交。
* 因此“已提交”即“原档已落盘且判定完整”；失败上传整体回滚并删除该目录，
  既有已发布航迹完全不变。
* 查询与下载在 `BEGIN`（WAL 快照）内固定船舶 `version`，并发重算不会让一次
  请求混入新旧两版数据；旧版本在事务内被裁剪。
* 启动时 `cleanup_staging` 清理崩溃遗留的 `.staging-*` 和无批次引用的目录，
  已发布批次永不删除。

## 站日原档交接对账（`app/reconcile.py` / `app/recon_store.py`）

值班员收到上游**按岸站和 UTC 日期**开具的原档交接清单后，在离线分析前核对
当天是否收齐。入口为导航栏 **站日对账**（`GET /reconcile`），选择岸站与 UTC
日期进入站日工作台 `/reconcile/{station}/{day}`：

1. 在文本框录入预期原档 SHA-256，每行一个；也接受 `sha256sum` 输出
   （`<hash>  文件名`，文件名部分忽略），大小写与空行自动归一。
2. 保存后系统与**已发布批次**逐档对账并给出结论；补传批次或更换清单后重新
   打开/提交即按最新已提交状态重算。

对账口径（纯函数，见 `app/reconcile.py`）：

* **实收范围**：已发布且**含有**该岸站当日定位行的批次原档（按 UTC 日历日
  00:00–次日 00:00 划界）。
* **缺档**：清单中有、实收中没有的 SHA-256。
* **清单外档**：实收 SHA-256 不在清单中（多出的副本同时计入重复收档）。
* **重复收档**：同一 SHA-256 实收超过一份；清单自身重复录入同一 SHA-256
  也单独列出，因为此时无法一一对应。
* **内容合格性**：匹配到的原档若含**解析拒绝行**，或含**其他岸站/他日的
  定位行**，即判内容不合格，不得交接。
* 只有清单与实收一一对应、无重复且每档内容合格，页面才显示 **✓ 可交接**。

并发与陈旧页面保护：

* 每个站日维护单调递增的 `station_day_state.revision`：批次发布触及该站日、
  或清单被替换时，在**同一事务**内 +1。页面隐藏域带上渲染时的修订号。
* 提交清单时若修订号已不匹配（页面打开后批次或清单发生过变化），返回
  **409 拒绝旧页面提交**，已保存的原清单不变；清单格式非法返回 400，同样
  不改动已保存清单，文本框保留刚才的录入内容便于修改。
* 对账页的批次清单、每档内容统计、清单与修订号全部在**一个 `BEGIN` 快照
  事务**内读出，并发查询不会把新旧两个已提交状态混在同一结论里。

## 页面与下载

| 路径 | 作用 |
| --- | --- |
| `GET /` | 批次列表与船舶列表 |
| `POST /upload` | 上传原档（multipart 字段名 `file`） |
| `GET /batches/{id}` | 本批原始行逐行判定 + 解析拒绝明细 |
| `GET /batches/{id}/original.csv` | 下载该批原档（响应头 `X-Content-SHA256`） |
| `GET /vessels/{mmsi}` | 可信点（含相对上一点航速）与隔离原因 |
| `GET /vessels/{mmsi}/track.csv` | 可信航迹 CSV（按 UTC 排序） |
| `GET /vessels/{mmsi}/isolated.csv` | 隔离记录与原因 CSV |
| `GET /reconcile` | 站日选择与已有站日列表 |
| `GET /reconcile/{station}/{day}` | 站日对账工作台（清单、修订号、对账结论与明细） |
| `POST /reconcile/{station}/{day}` | 保存/替换清单并触发对账（修订号过期返回 409） |
| `GET /healthz` | 健康检查 |

## 测试与样例

```bash
. .venv/bin/activate
pytest -q            # 航迹规则 + 对账规则 + HTTP/并发/重启/失败回滚/陈旧页面
python tests/smoke.py  # 用 samples/ 跑一遍并打印每船判定与站日对账过程
```

`samples/README.md` 详细说明了三个样例场景。手工验证顺序：

1. 上传 `samples/batch1_base.csv`：413000001 出现 `duplicate`，413000002 整秒
   `conflict`，413000003 出现 `speed_exceeded`，413000004 的坏行进入拒绝明细。
2. 上传 `samples/batch2_late_arrivals.csv`：413000005 补入迟到点后被重算，
   413000001/413000002 的重投结果保持稳定，同时出现新船 413000008。
3. 对比每次上传后 `/vessels/{mmsi}` 与 `track.csv`：413000006 在批次 2 后其
   01:00 的旧点变为隔离；同时旧批次原档仍可完整下载。

## 目录

```
app/
  ais.py        CSV 解析、校验、纯函数航迹判定
  reconcile.py  清单解析与站日对账纯规则（缺档/清单外/重复/内容合格性）
  recon_store.py 同一快照事务内装配站日实收批次、清单与修订号
  db.py         SQLite schema、WAL 连接、读写事务
  store.py      原档原子落盘 + 单事务发布 + 全船重算 + 站日修订号/清单
  main.py       FastAPI 路由与 CSV 导出
  templates/    网页（总览/批次/船舶/对账/错误）
samples/        重投、冲突、迟到点、坏行及站日对账样例
tests/          规则单测、HTTP/并发/重启/回滚/对账测试、smoke
```
