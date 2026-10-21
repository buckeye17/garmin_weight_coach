# Garmin Weight Coach

Personal web app that syncs Garmin scale/watch data, tracks daily nutrition against suggested targets, and projects weight change over the next 4 weeks. See [docs/PLAN.md](docs/PLAN.md) for the full design and milestones.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in your values
```

## Run

```bash
uv run gwc serve          # http://127.0.0.1:8060 (applies DB migrations first)
uv run gwc serve --debug  # Dash dev server with hot reload
```

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
uv run gwc db revision -m "describe change"   # after editing src/gwc/db/models.py
```

All personal data (database, Garmin tokens, logs, backups) lives in `data/`, which is git-ignored along with `.env`.
