# Garmin Weight Coach

Personal web app that syncs Garmin scale/watch data, tracks daily nutrition against suggested targets, projects weight change over the next 4 weeks, and includes a Claude coach. It runs on one Windows PC and is reached from a desktop browser or a phone over Tailscale. See [docs/PLAN.md](docs/PLAN.md) for the full design.

- **Today**: progress bars for calories, protein, carbs, fat, fiber, sugar and sodium for the current food day (which ends when you fall asleep, per Garmin). Workouts raise that day's targets once they sync (see below).
- **Add food**: USDA search, your own foods and recipes, or describe a meal in plain English and let Claude itemize it for review. Name a chain restaurant ("Chipotle chicken bowl, no cheese") and Claude looks up the chain's published nutrition on the web, links the source, and saves the item to your foods for next time. If it can't find published numbers, you type them in from the menu or receipt.
- **Trends** and **Projection**: smoothed weight trend, Garmin vs. trend-based burn, and a 4-week projection with an uncertainty band.
- **Coach**: chat about your data, plus an automatic weekly summary.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and, for the default coach backend, [Claude Code](https://claude.com/claude-code) logged in on this PC.

```bash
uv sync
cp .env.example .env   # then fill in your values
```

In `.env`:

| Setting | What it's for |
|---|---|
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Used only when you click **Connect** in Settings |
| `USDA_API_KEY` | Free key from <https://api.data.gov/signup/> for food search |
| `GWC_LLM_BACKEND` | `subscription` (your Claude Code login) or `api` (needs `ANTHROPIC_API_KEY`) |
| `GWC_HOST`, `GWC_PORT` | Keep `127.0.0.1:8060`; use Tailscale for the phone (below) |
| `GWC_TZ` | IANA time zone; blank uses the system's |

Units, goals, targets, sync time and coach models are set in the app under **Settings**.

## Connect Garmin

1. Start the app, open **Settings**, click **Connect**, and enter the 2FA code if Garmin asks.
2. The first sync downloads the last 30 days. After that the app syncs daily at 09:01, catches up on startup or wake if it missed a run, and offers a rate-limited **Sync now** button.

The login token is saved in `data/garmin_tokens/`; your password is only used when you click Connect. Garmin allows only a few logins a day, so the app never logs in on its own.

## Run

```bash
uv run gwc serve          # http://127.0.0.1:8060 (applies DB migrations first)
uv run gwc serve --debug  # Dash dev server with hot reload
```

### Start at log on

`scripts/install_autostart.ps1` registers a Task Scheduler task, for your user only, that runs `uv run gwc serve` 30 seconds after you log on, with no console window. It restarts the app up to 3 times if it crashes. No admin rights are needed.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1             # install
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -StartNow   # install and start now
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Restart    # restart, e.g. after pulling new code
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Stop       # stop the app
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall  # stop and remove
```

Use `-StartNow` only after stopping any copy you started by hand, since both would need port 8060. Output goes to `data/logs/gwc.log`. Stop or restart it with `-Stop` / `-Restart`, not Task Scheduler's **End**: that only ends the hidden console wrapper, and uv and the app keep running. `-Stop` also stops a copy started from a terminal.

## Phone access with Tailscale

The app listens only on `127.0.0.1`. [Tailscale Serve](https://tailscale.com/kb/1312/serve) publishes it over HTTPS to devices on your tailnet and nothing else: not your LAN, not the internet.

1. Install Tailscale on this PC and the phone, and sign in to the same tailnet on both.
2. In the [admin console](https://login.tailscale.com/admin/dns), turn on **MagicDNS** and **HTTPS Certificates** (needed once).
3. On this PC:

   ```bash
   tailscale serve --bg 8060
   ```

   It prints the address, e.g. `https://my-pc.tail1234.ts.net`. `--bg` keeps it running across reboots. No change to `GWC_HOST` is needed.
4. Open that address on the phone. In Chrome, use **⋮ → Add to Home screen** for an app-like shortcut.

Check it with `tailscale serve status`, and turn it off with `tailscale serve --https=443 off` (or `tailscale serve reset`). Don't use `tailscale funnel`, which makes the app public.

The layout is tuned for the OnePlus Open: folded and unfolded both get bottom navigation and full-width pages; the sidebar appears from 992 px wide.

## Workouts and targets

Calorie targets start from your burn **without** workouts. Each Garmin activity recorded during a food day then adds 75% of its calories above resting to that day's target, with the extra going mostly to carbs. The percentage is set in **Settings → Goal and targets**. To see a workout's credit right away, let the watch upload to Garmin Connect, then click **Sync now** in Settings. Rest days are a little lower than your all-in Garmin burn, and a workout is credited once, on its own day, rather than again through next week's average.

## Backups

After each successful Garmin sync the database is copied to `data/backups/gwc-YYYY-MM-DD.db` with SQLite's online backup API, which is safe while the app is running. There is one copy per day, refreshed by later syncs that day, and the newest 14 are kept.

To restore:

1. Stop the app.
2. Delete `data/gwc.db-wal` and `data/gwc.db-shm` if they exist.
3. Copy the backup you want over `data/gwc.db`.
4. Start the app.

`data/backups/` is on the same disk as the database, so copy it elsewhere too (OneDrive, an external drive) if you want protection from disk failure.

## Coach

**Settings → Coach** picks the backend and, for each feature (chat, food logging, weekly summary), a model and effort level. The `subscription` backend uses your Claude Code login through the Claude Agent SDK and never bills an API key. Health and food data you send to the coach goes to Anthropic.

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
uv run gwc db revision -m "describe change"   # after editing src/gwc/db/models.py
```

Tests never touch Garmin, USDA or Claude; they use fakes and a throwaway data directory.

While the app is running it locks `.venv\Scripts\gwc.exe`, so `uv sync` and `uv run` (which syncs first) fail. Use `uv run --no-sync ...`, and add packages with `uv add --no-sync <pkg>` followed by `uv pip install <pkg>`.

All personal data (database, Garmin tokens, logs, backups) lives in `data/`, which is git-ignored along with `.env`.
