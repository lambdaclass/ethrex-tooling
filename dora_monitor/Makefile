.PHONY: all deps run dry-run dry-run-once clean

CONFIG ?= config.yaml

all: deps

.venv:
	uv venv
	uv pip install -e .

deps: .venv

run: deps
	uv run dora-monitor -c $(CONFIG)

dry-run: deps
	uv run dora-monitor -c $(CONFIG) --dry-run --debug

dry-run-once: deps
	uv run dora-monitor -c $(CONFIG) --once --dry-run --debug

clean:
	rm -rf .venv *.egg-info build dist dora_monitor_state.json
