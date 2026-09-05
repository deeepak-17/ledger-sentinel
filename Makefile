PY := .venv/bin/python
UV := uv

.PHONY: setup data test lint fmt metrics ai models cache demo api determinism clean

setup:                ## create (or recreate) the venv and install everything
	$(UV) venv --clear --python 3.11 .venv
	$(UV) pip install -e ".[dev]"

data:                 ## regenerate both seed datasets and the distribution table
	$(PY) -m data.generator --seed A
	$(PY) -m data.generator --seed B
	$(PY) -m data.generator --readme

test:                 ## unit, golden, adversarial, tools, AI-layer and API tests
	$(PY) -m pytest

lint:                 ## exactly what CI runs, so a green local run means a green build
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:                  ## apply the formatter rather than just complaining about it
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

metrics:              ## deterministic layer only, on the held-out seed
	$(PY) -m src.metrics --seed B

ai:                   ## the whole system, replaying the committed response cache
	$(PY) -m src.metrics --seed B --ai

models:               ## which models this key can reach, and is the pin valid
	$(PY) scripts/models.py

cache:                ## re-record the LLM cache against the live API (needs a key)
	$(PY) scripts/record_cache.py --seed A --seed B

determinism:          ## three identical runs, asserted equal, classifier included
	$(PY) scripts/determinism.py --seed B --runs 3 --ai

api:                  ## FastAPI on :8000; /docs is the interactive evidence
	$(PY) -m uvicorn src.api:app --reload

demo:                 ## the one command a judge runs
	$(PY) -m streamlit run src/app.py

clean:
	rm -rf .pytest_cache **/__pycache__ ledger.db
