.PHONY: install backend frontend check-settle

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 结算核对流水线：示例数据装载 → 金额试算 → 前后端启动可用性，
# 通过则可提交，失败会指出卡在哪一步并保留日志，重跑前自动清场。
check-settle:
	python3 scripts/check-settle.py
