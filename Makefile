.PHONY: install install-gateway test pretest-strategy collect brain cycle stop status gateway daemon logs data-health size-regime fft-backtest

install:
	pip install -e .

install-gateway:
	pip install -e '.[gateway]'

test:
	pytest tests/ -v --ignore=tests/test_strategy_precheck.py

pretest-strategy:
	pytest tests/test_strategy_precheck.py -v --strategy-module=$(STRATEGY)

collect:
	python3 -m src.collector.data_collector

brain:
	python3 -m src.brain.brain_consensus

execute:
	python3 -m src.executor.trade_executor

cycle:
	bash scripts/run_cycle.sh

stop:
	bash scripts/emergency_stop.sh

monitor:
	python3 -m src.monitor.monitor

data-health:
	@if [ -x .venv/bin/python3 ]; then .venv/bin/python3 -m src.collector.data_health_check; else python3 -m src.collector.data_health_check; fi

size-regime:
	@if [ -x .venv/bin/python3 ]; then .venv/bin/python3 -m src.risk.size_regime; else python3 -m src.risk.size_regime; fi

fft-backtest:
	@if [ -x .venv/bin/python3 ]; then .venv/bin/python3 -m src.hypothesis.fft_hypothesis_backtest; else python3 -m src.hypothesis.fft_hypothesis_backtest; fi

gateway:
	python3 -m src.gateway.server

status:
	@echo "=== Kill Switch ===" && python3 -c "from src.risk.kill_switch import get_status; import json; print(json.dumps(get_status(), indent=2))" 2>/dev/null || echo "Not available"
	@echo "=== Positions ===" && cat state/positions.json 2>/dev/null || echo "No positions"
	@echo "=== Daily P&L ===" && cat state/daily_pnl.json 2>/dev/null || echo "No daily PnL"
	@echo "=== Data Health ===" && cat state/data_health.json 2>/dev/null || echo "No data health"
	@echo "=== Data Health Summary (24h) ===" && cat state/data_health_summary.json 2>/dev/null || echo "No data health summary"
	@echo "=== Size Regime ===" && cat state/size_regime.json 2>/dev/null || echo "No size regime"

daemon:
	bash scripts/daemon.sh

logs:
	tail -f logs/daemon.log
