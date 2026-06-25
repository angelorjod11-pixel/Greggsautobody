# Convenience targets for the Polymarket trader research system.
# Override the database with:  make run DB=postgresql+psycopg2://user:pass@host/db
PY ?= python3
DB ?= sqlite:///data/polytrader.db
export POLYTRADER_DATABASE_URL = $(DB)
export PYTHONPATH = $(CURDIR)

.PHONY: help install run run-live test top report api dashboard daily clean

help:
	@echo "Targets:"
	@echo "  install     pip install -r requirements.txt"
	@echo "  run         full pipeline on synthetic data (default)"
	@echo "  run-live    full pipeline against the live Polymarket APIs"
	@echo "  test        run the pytest suite"
	@echo "  top N=25    print the top-N ranked wallets"
	@echo "  report      regenerate the Markdown final report"
	@echo "  api         serve the FastAPI app on :8000"
	@echo "  dashboard   launch the Streamlit dashboard"
	@echo "  daily       run the daily update workflow"
	@echo "  clean       remove the local SQLite db and generated reports"

install:
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) -m polytrader.cli all

run-live:
	POLYTRADER_DATA_SOURCE=live $(PY) -m polytrader.cli all

test:
	$(PY) -m pytest tests/ -q

N ?= 25
top:
	$(PY) -m polytrader.cli top --n $(N)

report:
	$(PY) -m polytrader.cli report

api:
	$(PY) -m uvicorn api.main:app --port 8000 --reload

dashboard:
	$(PY) -m streamlit run dashboard/app.py

daily:
	$(PY) workflows/daily_update.py

clean:
	rm -f data/*.db data/*.db-wal data/*.db-shm
	rm -f reports/generated/FINAL_REPORT*.md
