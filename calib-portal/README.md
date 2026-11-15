# 校准读数补录助手（FastAPI + Playwright + SQLite）

计量室数据员把仪器导出的校准读数 CSV 补录到旧门户。系统用 **Playwright 像人一样操作
门户的表单页与查询页**（不直接调用门户数据接口），用 **SQLite 按门户 + 设备编号 +
校准日期持久保存每行进度**，断线或服务重启后可续办，避免重复填报。

## 一、安全规则（状态机铁律）

每行 CSV 都严格按下面的规则处理：

1. **先查后填**：先在门户查询页按「设备编号 + 校准日期」查询。
   - 门户有**唯一且示值误差同值**记录 → 记为「已完成·跳过」，不提交；
   - **查无记录** → 才打开新增表单提交；
   - **多条记录** → 暂停该批，不提交；
   - **唯一但异值** → 暂停该批，**绝不覆盖门户原记录**；
   - **查询失败（500/超时/页面异常）** → 暂停该批。
2. **提交后必须复核**：无论提交是否拿到正常响应，都再次查询门户确认：
   - 复核到唯一同值 → 记完成（门户保存成功却断线的情形也由此确认）；
   - 复核查询失败 / 查无 / 多条 / 异值 → 分别暂停，禁止自动重提。
3. **恢复只查不提**：服务在提交途中重启、或提交后断线的行，恢复时**只做查询**：
   - 唯一同值 → 记完成；查无 → 暂停待人工核实；多条/异值/查询失败 → 暂停；
   - 系统**永远不会自动重新提交**，防止门户其实已保存导致重复填报。
4. 暂停后该批次后续行不再处理；人工在网页上逐行处理（重新查询 / 核实记完成 /
   显式授权重提 / 忽略），或整体续办剩余待处理行。
5. 进度键为 `(门户地址, 设备编号, 校准日期)`，数据库唯一约束保证重复上传同一 CSV
   不会产生重复行，而是复用原进度续办。

## 二、目录结构

```
calib-portal/
├── docker-compose.yml      # app + 模拟门户 两个服务
├── Dockerfile.app          # 含 Chromium 与中文字体的填报助手镜像
├── Dockerfile.portal       # 无账号模拟旧门户镜像
├── app/                    # FastAPI 填报助手
│   ├── webapp.py           # 页面与 API（上传、逐行状态、人工操作）
│   ├── worker.py           # 后台工作线程与安全状态机
│   ├── automation.py       # Playwright 封装（专属线程执行同步 API）
│   ├── database.py         # SQLite 持久化
│   ├── csvutil.py          # CSV 解析与示值误差同值比较
│   ├── config.py           # 状态常量与配置
│   └── templates/          # 上传页 / 批次逐行状态页
├── portal/                 # 模拟旧门户（服务端渲染表单 + 查询页）
│   └── app.py              # 含故障注入：drop_submit / query_500
├── data/sample.csv         # 示例 CSV
└── demo.sh                 # 宿主机端到端演示脚本
```

## 三、快速开始（Docker Compose）

```bash
cd calib-portal
docker compose up --build
```

- 填报助手：<http://localhost:8000>
- 模拟旧门户：<http://localhost:8080>（免登录）
- 门户管理后台（故障注入/重置数据）：<http://localhost:8080/admin/records>

打开填报助手，门户地址保持默认 `http://localhost:8080`，选择
`data/sample.csv` 上传即可。门户预置了 4 条种子记录，配合示例 CSV 可直接看到：

| CSV 行 | 门户种子情况 | 预期结果 |
|---|---|---|
| EQ-1002 / 2026-11-01 / +0.02 | 唯一同值 | 已完成·跳过提交 |
| EQ-1003 / 2026-11-02 / +0.10 | 唯一异值（门户 +0.05） | 暂停·异值不覆盖 |
| EQ-1004 / 2026-11-03 / +0.02 | 同设备同日期两条 | 暂停·多条 |
| EQ-1001 等其余行 | 查无 | 查询确认无记录后提交，再复核记完成 |

## 四、三个故障演示

1. **保存成功后断线**：门户后台把故障模式切到 `drop_submit`（门户会先把记录落库，
   再直接断开 HTTP 响应），再上传含新设备的 CSV。助手提交时浏览器收到断连异常，
   随后自动复核查询，发现唯一同值后记完成，不会重复提交。
2. **异值记录**：示例 CSV 的 EQ-1003 即异值情形，批次直接暂停、门户原记录保持不变。
3. **服务重启续办**：批次处理中 `docker compose restart app`（或 `kill -9` 应用
   进程）。重启后工作线程对停留在「查询中」的在途行**只查询不重提**：同值记完成、
   查无暂停待核；暂停行保持暂停等待人工处理，不会被自动反复重查或重提。

也可以把门户故障切到 `query_500` 观察查询失败暂停；故障恢复后在网页点该行
「重新查询（只查不提）」。

## 五、网页与接口

- `GET /` 上传页与批次列表
- `GET /batches/{id}` 逐行状态页（2 秒自动刷新，含状态、说明、截图、门户记录链接、
  尝试次数、人工操作按钮）
- `POST /api/batches` 上传 CSV 建批（multipart：`portal_url`、`file`）
- `POST /api/batches/{id}/resume` 续办（仅处理待处理行）
- `POST /api/rows/{id}/retry-query` 暂停行重新查询（只查不提）
- `POST /api/rows/{id}/mark-done` 人工核实记完成（须提交门户记录链接）
- `POST /api/rows/{id}/resubmit` 人工显式授权后才允许重新走 查→提→复核
- `POST /api/rows/{id}/ignore` 人工忽略

## 六、持久化与数据

- SQLite 数据库、上传的 CSV、浏览器失败截图都在应用容器 `/data`（compose 卷
  `app-data`），重启不丢；门户数据在卷 `portal-data`。
- 行状态：`pending / querying / done_existing / done_submitted /
  paused_no_record / paused_multi / paused_diff / paused_query_fail /
  paused_submit_fail / ignored`。
- 示值误差按数值比较（`+0.03` 与 `0.030` 视为同值），日期支持
  `YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD` 等写法并统一归一。

## 七、设计说明

- Playwright 同步 API 不能运行在 FastAPI 的 asyncio 线程，所有浏览器调用都通过
  队列交给一个专属「playwright-runner」线程执行；业务工作线程是另一个独立线程。
- 门户记录链接区分容器内地址（`PORTAL_INTERNAL`，浏览器自动化用）与外部地址
  （`PORTAL_EXTERNAL`，用户点击用），持久化保存的是外部可访问链接。
- 模拟门户只依赖 FastAPI，服务端渲染 HTML，表单/查询页元素带稳定 id，便于自动化。
