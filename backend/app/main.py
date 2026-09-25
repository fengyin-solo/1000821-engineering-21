"""港口集装箱作业调度平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health

结算模块的示例数据与金额口径以 settlement-fixtures/cases.json 为唯一来源，
启动时自动装载；如需空表排障可 SETTLE_DEVDATA=0。
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.devdata import build_settle_rows
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="港口集装箱作业调度平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)

if os.environ.get("SETTLE_DEVDATA", "1") != "0":
    # 启动时装载失败要直接抛出来：宁可服务起不来，也别带着口径不对的数据对外。
    store.replace_rows("settle", build_settle_rows())


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
