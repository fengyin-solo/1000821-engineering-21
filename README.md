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
│   └── app/store.py          内存数据仓库与示例数据
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

## 结算试算检查（改了计费口径就跑一遍）

结算样例数据、金额试算和前后端可用性串成了一条可重复跑的流程，
改完结算相关代码后不用再人工去页面上核：

```bash
make settle-check
# 或：python3 scripts/devcheck/settle_check.py
```

它会依次做这几件事：

1. **环境预检**：检查 `backend/.venv`、前端 `node_modules` 和样例数据是否就绪，缺了会提示先 `make install`。
2. **拉起前后端**：用临时端口各起一份后端（uvicorn）和前端（vite，`VITE_PROXY_TARGET` 指到本次的后端），不影响你已经开着的 dev 服务。
3. **可用性检查**：后端 `/api/health`、前端首页、结算页面模块能否被 vite 编译。
4. **金额试算**：对 `scripts/devcheck/fixtures/settle-cases.json` 里的每条用例调
   `POST /api/settle/trial`（计费口径的唯一入口，在 `backend/app/services/settle.py`），
   逐项比对费用拆分和应收金额；异常用例（负数、未配置费率的作业类型）必须被拦下。
5. **接口/页面一致性**：同样的用例经 vite 代理（页面通道）再算一遍，结果必须和直连接口逐条一致。
6. **端到端**：用试算结果登记样例结算单、回读金额、走一遍「发起核对」状态流转。

结束后打印结论：

- `✅ 结论：可以提交` —— 六步全过，本次启动的进程与写入的数据已全部清理。
- `❌ 结论：不可提交。卡在「<步骤名>」` —— 指出卡在哪一步、哪个用例、期望与实际差多少，
  并附上前后端日志末尾；修复后直接重跑同一条命令即可。加 `--keep` 可在失败时保留进程与日志。

样例数据只放一处：`scripts/devcheck/fixtures/settle-cases.json`。
改费率或算法（`RATE_TABLE`、夜间加成、月结优惠）后，同步更新该文件里的 `expected`，
再跑 `make settle-check`。试算数据写在临时服务的内存里，服务进程退出即消失，
不会留下上一次的中间数据；现有结算页面与接口保持不变。


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
