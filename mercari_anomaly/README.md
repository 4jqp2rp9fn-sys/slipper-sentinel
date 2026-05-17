# Mercari JP Slipper/Sandal Anomaly Watcher

Lightweight Python service that watches new Mercari Japan listings for
slippers/sandals and pings Discord when something looks off
(underpriced, sudden price drop, listing spike, similar cheaper twins).

## Layout

```
mercari_anomaly/
  scraper.py     # fetch Mercari listings by keyword
  storage.py     # SQLite history + price snapshots
  analyzer.py    # rule-based anomaly detection + score
  notifier.py    # Discord webhook
  main.py        # orchestrator
  requirements.txt
  data/listings.db  (auto-created)
.github/workflows/mercari-anomaly.yml   # runs every 10 min
```

## Run locally

```bash
pip install -r mercari_anomaly/requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m mercari_anomaly.main
```

## GitHub Actions

1. Add repo secret `DISCORD_WEBHOOK_URL`.
2. The workflow runs on `*/10 * * * *` and commits the updated
   `listings.db` back to the repo so history accumulates between runs.

## Tuning

All thresholds live at the top of `analyzer.py`:

- `UNDERPRICED_Z` — z-score cutoff vs recent keyword baseline
- `PRICE_DROP_PCT` — minimum drop to flag a price cut
- `LISTING_SPIKE_RATIO` — last hour vs 24h hourly average
- `TITLE_SIMILARITY` — similarity ratio for "same model" matching

## Notes

- Mercari's search endpoint occasionally changes; if `scraper.py` stops
  returning items, inspect `WEB_SEARCH_URL` and headers.
- Keep this respectful: one polite request per keyword per run.
