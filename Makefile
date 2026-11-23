# Convenience targets for local checks. None of these need AWS credentials.

PYTHON ?= python3
SHELL := /usr/bin/env bash

.PHONY: help test dry-run agent-dry-run policies scripts

help:
	@echo "make test          — unit tests (simulator, Lambda, IAM JSON)"
	@echo "make dry-run       — 8-second simulator dry-run"
	@echo "make agent-dry-run — demo events from the real-arm hook"
	@echo "make policies      — parse iam/*.json"
	@echo "make scripts       — bash -n on setup/teardown"

test:
	$(PYTHON) -m unittest discover -s tests -v

dry-run:
	$(PYTHON) simulator/simulate.py --dry-run --duration 8 --seed 1 --rate 20 --fault-probability 0 --estop-probability 0

agent-dry-run:
	$(PYTHON) agent/arm_agent.py --dry-run

policies:
	$(PYTHON) -c "import json,pathlib; paths=sorted(pathlib.Path('iam').glob('*.json'));\
	[json.loads(p.read_text()) for p in paths]; print(f'parsed {len(paths)} policy files')"

scripts:
	bash -n infra/setup.sh
	bash -n infra/teardown.sh
	@echo "bash -n ok"
