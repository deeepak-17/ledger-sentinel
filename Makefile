PY := .venv/bin/python
UV := uv

.PHONY: setup data test lint metrics demo determinism clean

setup:                ## create the venv and install everything
	$(UV) venv --python 3.11 .venv
	$(UV) pip install -e ".[dev]"

data:                 ## regenerate both seed datasets and the distribution table
	$(PY) -m data.generator --seed A
	$(PY) -m data.generator --seed B
	$(PY) -m data.generator --readme

test:                 ## unit, golden, adversarial and API tests
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

metrics:              ## run the pipeline on the held-out seed and print the report
	$(PY) -m src.metrics --seed B

determinism:          ## three identical runs, asserted equal
	$(PY) scripts/determinism.py --seed B --runs 3

demo:                 ## the one command a judge runs
	$(PY) -m streamlit run src/app.py

clean:
	rm -rf .pytest_cache **/__pycache__ ledger.db
