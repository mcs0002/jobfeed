#!/usr/bin/env python3
"""CLI entry point for the nightly description backstop.

The implementation moved into the enrichment package
(``scrapers/enrich/descriptions.py``); this thin shim keeps the runnable name
``enrich_descriptions.py`` stable for ``deliver.sh`` and any launchd job, and
avoids the ``-m`` package double-import warning.
"""
from scrapers.enrich.descriptions import main

if __name__ == "__main__":
    raise SystemExit(main())
