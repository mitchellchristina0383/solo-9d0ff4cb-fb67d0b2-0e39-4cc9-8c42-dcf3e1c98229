# 文印室双面打印队列

按页录入文档，操作两台模拟打印机逐面出纸；卡纸后原机确认停稳，再把续单派给另一台打印机，从首张未完整出纸处重印两面。奇数页文档末张背面留白。所有出纸记录写入 SQLite，网页可核对装订页序，重启容器后进度不丢。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000/>。

停止：`docker compose down`；数据在命名卷 `printroom-data` 中，`docker compose down -v` 会连同数据一起删除。

## 操作流程

1. **录入文档**：填标题新建 → 按顺序逐页添加内容（可留空表示空白页）→ “提交入队”。
   入队后页面锁定；同一文档重复提交只产生一个作业。
2. **开始打印**：在空闲打印机上选择排队作业 → 开始。
3. **逐面出纸**：每张纸先“出正面”，再“出背面”。双面都出完（或奇数页末张的正面出完）才算**完整出纸**，自动进入下一张。
4. **卡纸**：点“报告卡纸”。当前半张立即作废；原机停在“卡纸待停稳”，此时**不能**派续单，也不能继续出纸。
5. **确认停稳**：原机点“确认原机停稳”。停稳前已完整出纸的纸张全部保留，只有作废的半张需要重印。
6. **换机**：对已停稳的作业点“换机派续单”，系统派给另一台打印机，从首张未完整出纸的那张开始**重印两面**。重复点击不会重复派单。
7. **核对**：页面底部“出纸记录”按次列出每张纸的正/背面页码与状态；作业卡片里的“装订核对”只列完整出纸的纸张并按张号排序，按此顺序叠放即为原页序。

页序规则：第 s 张纸正面印第 `2s-1` 页，背面印第 `2s` 页；`2s` 超过总页数时背面留白。

## 本地运行（不装 Docker）

只依赖 Python 3.11+ 标准库：

```bash
python3 -m printroom          # 默认 0.0.0.0:8000，数据库 data/printroom.db
```

环境变量：`PRINTROOM_HOST`、`PRINTROOM_PORT`、`PRINTROOM_DB`。

## 测试与自检

```bash
python3 -m unittest tests.test_printroom -v   # 31 个端到端用例（含 /healthz 持锁/只读/缺失/恢复）
python3 scripts/check_ui.py                   # 静态页面与脚本引用一致性
python3 scripts/demo.py                       # 跑通“半张卡纸”和“停稳前出纸”两条验收路径
```

测试使用临时 SQLite 数据库和随机端口，不会污染 `data/`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存储健康探测：仅当作业库能实际提交一次探测写入时返回 200，否则 503 |
| GET | `/api/state` | 文档、作业、打印机、出纸记录全量快照 |
| GET | `/api/jobs/{id}` | 单个作业（含 `collation` 装订视图） |
| GET | `/api/documents/{id}` | 文档及页面 |
| POST | `/api/documents` | 新建文档 `{title}` |
| POST | `/api/documents/{id}/pages` | 追加一页 `{content}` |
| POST | `/api/documents/{id}/pages/last` | 删除末页 |
| POST | `/api/documents/{id}/finalize` | 提交入队（幂等） |
| POST | `/api/jobs/{id}/start` | 开始打印 `{printer_id}` |
| POST | `/api/jobs/{id}/print` | 出一面 `{side: front\|back, sheet_no?}` |
| POST | `/api/jobs/{id}/jam` | 报告卡纸（幂等） |
| POST | `/api/jobs/{id}/stop` | 原机确认停稳（幂等） |
| POST | `/api/jobs/{id}/switch` | 换机派续单 `{printer_id?}`（每个停稳段最多一份） |

错误返回 `{error, code}`，常见 `code`：`printer_busy`、`not_stopped`、`sheet_mismatch`、`empty_document`。

## 数据一致性

- 写操作走 `BEGIN IMMEDIATE` 事务，`print_segments` 上有“每台打印机最多一个活动/卡纸段”的部分唯一索引。
- `sheet_attempts(job_id, sheet_no) WHERE status='complete'` 唯一，保证同一张纸不会被计为两次完整出纸。
- `print_segments.continued_from` 唯一，保证一个停稳段最多派生一份续单；并发重复请求返回同一份。
- 进度全部落在 SQLite（`data/` 命名卷），服务重启后继续从数据库状态续印。

## 存储健康探测（/healthz）

`/healthz` 不再只看进程存活，而是**每次都对当前作业库实际提交一次探测写入**（专用表
`health_probe` 单行心跳，不触碰文档、作业及出纸记录；用 `mode=rw` 打开，库文件缺失时
不会新建空库）。Docker 的 `HEALTHCHECK` 和网页顶栏的“存储状态”徽标都以此为唯一依据，
网页徽标每 3 秒独立轮询，作业列表读失败时探测结论照样显示。

判定规则：

| 情形 | HTTP | `status` | 说明 |
| --- | --- | --- | --- |
| 探测写入提交并回读成功 | 200 | `ready` | 就绪 |
| 出纸事务占锁（1 秒内报暂忙） | 沿用上次已确认判定（无结论则 503） | `busy` | 暂忙，不改写结论 |
| 卷只读 / 库文件缺失 / 无法打开 | 503 | `error` | 故障 |

故障恢复后**无需重启**：下一次探测写入真正提交成功即重新就绪；恢复后若恰好有事务占锁，
在重新确认前保持未就绪。

故障演练（容器内）：

```bash
docker compose exec printroom chmod -R a-w /app/data   # 模拟卷只读 → 徽标变“存储故障”，容器 unhealthy
docker compose exec printroom chmod -R u+w /app/data   # 恢复 → 下一次探测提交成功即回“存储正常”
docker inspect -f '{{.State.Health.Status}}' printroom  # 查看容器健康结论
```
