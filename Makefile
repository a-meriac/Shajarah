# The repo folder is shared with the Linux VM, so each OS gets its own virtualenv.
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

# --- Linux VM only (root) ---
netns-up:
	sudo emulation/netns_setup.sh up

netns-down:
	sudo emulation/netns_setup.sh down

test-netns:
	sudo -E $(PY) -m pytest -q -m netns tests/emulation
