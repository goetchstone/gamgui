.PHONY: setup gam test run app clean help

VENV := .venv
PY := $(VENV)/bin/python

help:
	@echo "make setup   - create venv and install (dev + native window)"
	@echo "make gam     - vendor the GAM7 binary into gamgui/resources/gam7"
	@echo "make test    - run the offline test suite"
	@echo "make run     - launch the app (native window; falls back to a browser URL)"
	@echo "make app     - build the standalone macOS .app (PyInstaller, macOS only)"
	@echo "make clean   - remove venv and build artifacts"

setup:
	python3 -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev,desktop]"

gam:
	./scripts/fetch_gam.sh

test:
	$(PY) -m pytest -q

run:
	$(PY) -m gamgui.app

app:
	./scripts/build_app.sh

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache
