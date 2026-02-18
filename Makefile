.PHONY: install install-gateway test collect brain cycle stop status gateway

install:
	pip install -e .

install-gateway:
	pip install -e '.[gateway]'

test:
	pytest tests/ -v

collect:
	python3 -m src.collector.data_collector

brain:
	bash src/brain/brain.sh

execute:
	python3 -m src.executor.trade_executor

cycle:
	bash scripts/run_cycle.sh

stop:
	bash scripts/emergency_stop.sh

monitor:
	python3 -m src.monitor.monitor

gateway:
	python3 -m src.gateway.server

status:
	@echo "=== Kill Switch ===" && python3 -c "from src.risk.kill_switch import get_status; import json; print(json.dumps(get_status(), indent=2))" 2>/dev/null || echo "Not available"
	@echo "=== Positions ===" && cat state/positions.json 2>/dev/null || echo "No positions"
	@echo "=== Daily P&L ===" && cat state/daily_pnl.json 2>/dev/null || echo "No daily PnL"
