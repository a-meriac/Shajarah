# macOS and Linux get separate virtualenvs, so one checkout can be shared between them.
ifeq ($(shell uname -s),Linux)
VENV ?= .venv-linux
VENV_PY ?= python3
else
VENV ?= .venv
VENV_PY ?= python3.13
endif
PY ?= $(VENV)/bin/python

.PHONY: venv test test-netns lint netns-up netns-down probe experiments-smoke experiments

venv:
	$(VENV_PY) -m venv $(VENV) && $(VENV)/bin/pip install -e '.[dev]'

test:  ## unit + loopback integration tests (macOS or Linux)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check edgeproxy emulation experiments data tests

# --- Linux only (asks for sudo) ---
netns-up:
	sudo emulation/netns_setup.sh up

netns-down:
	sudo emulation/netns_setup.sh down

test-netns:
	sudo -E $(PY) -m pytest -q -m netns tests/emulation

# One short run (config 5, car tunnel, one session) to check the whole pipeline, ~2 min.
experiments-smoke:
	sudo $(PY) -m experiments.runner --batch smoke --configs 5 --scenarios car_tunnel_45s --sessions 1

# The full matrix: 7 configs x scenarios x 5 sessions, ~2 h. Resumes if interrupted.
experiments:
	sudo $(PY) -m experiments.runner --batch main
