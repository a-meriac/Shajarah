# macOS and Linux get separate virtualenvs, so one checkout can be shared between them.
ifeq ($(shell uname -s),Linux)
VENV ?= .venv-linux
VENV_PY ?= python3
else
VENV ?= .venv
VENV_PY ?= python3.13
endif
PY ?= $(VENV)/bin/python

.PHONY: venv test test-netns lint netns-up netns-down probe

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
