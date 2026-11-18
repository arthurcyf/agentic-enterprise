# Thin wrapper for Linux/macOS/WSL. On Windows, run `python -m audit <command>` directly.
PY ?= python

.PHONY: install data up down demo demo-offline test test-live eval verify-log

install:
	$(PY) -m pip install -e ".[dev]"
data:
	$(PY) -m audit data
up:
	$(PY) -m audit up
down:
	$(PY) -m audit down
demo:
	$(PY) -m audit demo
demo-offline:
	$(PY) -m audit demo --offline
test:
	$(PY) -m pytest
test-live:
	RUN_LIVE=1 $(PY) -m pytest tests/sdk_contract
eval:
	$(PY) -m audit eval --n 3 --k 2
verify-log:
	$(PY) -m audit verify-log
