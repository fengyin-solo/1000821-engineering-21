# 港口集装箱作业调度平台

面向船舶靠泊、集装箱装卸、堆场堆存、闸口进出与理货结算的一体化港口作业调度后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   │   └── billing.py        结算金额口径（试算/汇总的唯一实现）
│   ├── app/devdata.py        结算示例数据装载
│   └── app/store.py          内存数据仓库与示例数据
├── settlement-fixtures/
│   └── cases.json            结算示例数据 + 每单试算期望值（唯一事实来源）
├── scripts/
│   ├── check-settle.py       结算核对流水线（数据→试算→前后端可用性）
│   └── page-check.mjs        页面前端口径与后端接口逐单对拍
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

后端启动时会自动把 `settlement-fixtures/cases.json` 装载成结算模块的示例数据
（需要空表排障时 `SETTLE_DEVDATA=0`）。

## 结算核对流水线

改计费口径、动结算页面或准备提交前，跑一条命令把「示例数据 → 金额试算 →
前后端启动后的可用性」串起来核对：

```bash
make check-settle        # 等价于 python3 scripts/check-settle.py
```

流水线按顺序执行，每一步都打印通过/失败：

1. **前置检查**：python/node/npm、前后端依赖、示例数据可读；
2. **清场**：清理隔离端口（默认 8010/5180）上的残留进程和运行目录，不带入上一次的中间数据；
3. **起后端 + 健康检查**：`/api/health` 可用且模块齐全；
4. **数据核对**：`/api/settle/export` 与 `settlement-fixtures/cases.json` 逐单比对字段、状态、金额；
5. **试算核对**：每单调 `POST /api/settle/trial` 与 fixture 里的 `expected` 比对，
   `GET /api/settle/summary` 与 `expectedSummary` 比对，负数等非法入参必须被拦下；
6. **起前端**：vite dev server 在隔离端口就绪；
7. **端到端核对**：首页可取、`/api` 经 vite 代理到后端、页面实际 import 的
   `billing.ts` 与后端试算结果逐单零差异。

结束时给出明确结论：

- **可以提交**：全部通过，流水线自己起的进程全部关闭、运行目录删除，不留中间数据；
- **不可提交**：终端指出卡在第几步和具体差异（哪一单、哪个字段、页面值 vs 接口值），
  日志保留在 `.settle-check/`（`backend.log`、`frontend.log`）。修复后直接重跑同一条命令，
  开头的清场阶段会把现场清掉。

隔离端口可用 `SETTLE_CHECK_BACKEND_PORT` / `SETTLE_CHECK_FRONTEND_PORT` 覆盖。

### 金额口径放在哪里

- 后端唯一实现：`backend/app/services/billing.py`（Decimal 计算，ROUND_HALF_UP 保留两位），
  固定试算入口 `POST /api/settle/trial`、汇总入口 `GET /api/settle/summary`；
- 前端同口径实现：`frontend/src/views/settle/billing.ts`，结算页统计卡直接使用；
- 两份实现不共享代码，由流水线的端到端阶段逐单对拍，任何一边改了口径没同步另一边都会拦下。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 泊位计划 | `berth` | 泊位计划 | 计划编号、泊位编号、靠泊船舶 |
| 船舶档案 | `vessel` | 船舶 | 船舶编号、船舶名称、船舶类型 |
| 航次管理 | `voyage` | 航次 | 航次编号、关联船舶、进口航次号 |
| 岸桥作业 | `crane` | 岸桥 | 设备编号、岸桥型号、额定起重量 |
| 装卸任务 | `loading` | 装卸任务 | 任务编号、关联航次、作业类型 |
| 堆场管理 | `yard` | 箱区 | 箱区编号、箱区名称、堆放层数 |
| 集装箱档案 | `container` | 集装箱 | 箱号、箱型、箱况等级 |
| 堆存记录 | `yardstore` | 堆存单 | 堆存单号、关联箱号、箱区编号 |
| 闸口通行 | `gate` | 通行记录 | 通行编号、车牌号码、关联箱号 |
| 集卡调度 | `truck` | 集卡 | 调度单号、集卡牌号、司机姓名 |
| 理货作业 | `tally` | 理货单 | 理货单号、关联航次、理货方式 |
| 残损登记 | `damage` | 残损记录 | 残损编号、关联箱号、残损类型 |
| 单证处理 | `manifest` | 单证 | 单证编号、单证类型、关联航次 |
| 堆存计费 | `storage` | 计费单 | 计费单号、关联箱号、计费周期 |
| 引航拖轮 | `pilot` | 引航作业 | 作业编号、作业类型、关联船舶 |
| 安全监督 | `safety` | 安全检查 | 检查编号、检查区域、检查类型 |
| 货主档案 | `customer` | 货主 | 客户编码、客户名称、客户类型 |
| 作业结算 | `settle` | 结算单 | 结算单号、结算对象、结算周期 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
