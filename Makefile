.PHONY: install backend frontend settle-check

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 结算样例数据 → 金额试算 → 前后端可用性，一条流程跑完并给出能不能提交的结论
settle-check:
	python3 scripts/devcheck/settle_check.py
