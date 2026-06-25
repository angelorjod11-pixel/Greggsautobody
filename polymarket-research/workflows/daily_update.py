"""Daily update workflow.

Refreshes the whole research dataset and regenerates every output:

    ingest (live re-collect OR synthetic regen) → ETL → ranking → clustering →
    correlation → backtest → small-account → signals → alpha → report

then archives a dated copy of the report.  Designed to be driven by cron / a CI
schedule.  Logs to ``workflows/logs/`` (gitignored).

Usage
-----
    python workflows/daily_update.py                 # uses POLYTRADER_DATA_SOURCE
    POLYTRADER_DATA_SOURCE=live python workflows/daily_update.py
    python workflows/daily_update.py --keep          # append, don't drop (live incremental)

Cron (daily 06:00 UTC):
    0 6 * * *  cd /path/to/polymarket-research && \
        POLYTRADER_DATA_SOURCE=live /usr/bin/python3 workflows/daily_update.py >> workflows/logs/cron.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _setup_logging() -> Path:
    logdir = ROOT / "workflows" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    logfile = logdir / f"update_{stamp}.log"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(logfile), logging.StreamHandler()])
    return logfile


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Polytrader daily update")
    p.add_argument("--keep", action="store_true",
                   help="do not drop existing data (incremental-style append)")
    args = p.parse_args(argv)

    logfile = _setup_logging()
    log = logging.getLogger("daily_update")
    from polytrader.config import get_config
    cfg = get_config()
    log.info("Daily update starting | source=%s | db=%s", cfg.data_source, cfg.database_url)

    try:
        from polytrader.pipeline import run_all
        results = run_all(drop=not args.keep, verbose=False)
        for stage, info in results.items():
            log.info("stage %-14s %6.2fs", stage, info["seconds"])

        # archive a dated copy of the report
        src = ROOT / "reports" / "generated" / "FINAL_REPORT.md"
        if src.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            dst = ROOT / "reports" / "generated" / f"FINAL_REPORT_{stamp}.md"
            shutil.copyfile(src, dst)
            log.info("archived report -> %s", dst.name)

        ingest = results.get("ingest", {}).get("result", {})
        rank = results.get("rank", {}).get("result", {})
        log.info("DONE | markets=%s wallets=%s trades=%s ranked=%s",
                 ingest.get("markets"), ingest.get("wallets"), ingest.get("trades"),
                 rank.get("ranked"))
        return 0
    except Exception:
        log.exception("Daily update FAILED (see %s)", logfile)
        return 1


if __name__ == "__main__":
    sys.exit(main())
