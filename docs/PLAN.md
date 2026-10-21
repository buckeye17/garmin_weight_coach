# Garmin Weight Coach — Implementation Plan

A personal, single-user web app that:

1. Syncs body composition, daily activity/calorie, sleep, and activity data from Garmin Connect.
2. Tracks macro- and micronutrient intake throughout each day against suggested targets.
3. Projects weight change over the next 4 weeks, assuming the last 7 full days of intake and Garmin data continue.
4. Provides an LLM coach (Claude) for natural-language food logging, Q&A about the data, and weekly summaries.

All personal data stays on this machine and is ignored by git.

---

## 1. Decisions

| Topic | Decision |
|---|---|
| Language / env | Python 3.12, managed with `uv` |
| UI | Dash + Dash Mantine Components (DMC), Plotly charts, mobile-first layout |
| Hosting | Runs on this Windows PC; reachable from desktop browser and phone (OnePlus Open) via Tailscale |
| Storage | SQLite in git-ignored `data/` |
| Garmin access | Unofficial `garminconnect` library (≥ 0.3, mobile SSO flow, no `garth`); 2FA supported |
| Garmin backfill | 30 days on first connect |
| Sync | Automatic daily at **09:01** local; catch-up on startup / wake if missed; **manual "Sync now" button** guarded by self-imposed rate limits (§4.4) |
| Food logging | (A) USDA FoodData Central search, (B) custom foods & recipes, (D) natural-language entry parsed by Claude |
| Nutrients | Calories, protein, carbs, fat, fiber, sugar, sodium |
| Meal grouping | None — each entry has a timestamp only |
| Food day | Ends when Garmin's main sleep starts; fallback **09:00** if no sleep recorded (§5) |
| Goal | Target loss per month → app suggests calorie and macro targets with progress bars |
| Projection | Garmin-based and trend-based (adaptive TDEE) methods, dynamic metabolism, uncertainty band |
| Missing food days | Excluded from averages; warning when < 5 of the last 7 food days are logged |
| Units | Pounds by default; lb/kg and g/oz switchable for entry and display; stored internally in metric |
| LLM | Claude via the **Claude Agent SDK on the Max 5x subscription** (default); Anthropic API key backend as an alternative |
| Git workflow | Commit and push directly to `main`; no PRs or merge commits |

---

## 2. Stack

| Concern | Library |
|---|---|
| Web UI | `dash`, `dash-mantine-components`, `plotly` |
| WSGI server | `waitress` (gunicorn does not run on Windows) |
| ORM / migrations | `sqlalchemy` 2.x, `alembic` |
| Config | `pydantic-settings` (reads `.env`) |
| Scheduling | `apscheduler` 3.x `BackgroundScheduler`, in-process |
| Garmin | `garminconnect` |
| Food database | `httpx` → USDA FoodData Central REST API |
| Analysis | `pandas`, `numpy` |
| LLM | `claude-agent-sdk` (subscription), `anthropic` (API key) |
| Time zones | stdlib `zoneinfo` + `tzdata` (needed on Windows) |
| Dev | `pytest`, `ruff` |

Versions are pinned in `uv.lock`; DMC/Dash compatibility is checked when the project is scaffolded.

---

## 3. Repository layout

```
garmin_weight_coach/
├─ pyproject.toml / uv.lock
├─ .env.example                 # documented keys, no real values
├─ .gitignore                   # data/, .env, *.db, tokens, logs
├─ docs/PLAN.md
├─ scripts/
│  └─ install_autostart.ps1     # registers a Task Scheduler "at log on" task
├─ src/gwc/
│  ├─ __main__.py / cli.py      # `gwc serve`, `gwc db upgrade`
│  ├─ config.py
│  ├─ db/
│  │  ├─ models.py
│  │  ├─ session.py
│  │  └─ alembic/
│  ├─ garmin/
│  │  ├─ client.py              # login, 2FA, token storage — the only module that talks to Garmin
│  │  ├─ guard.py               # rate limiting, cooldowns, circuit breaker
│  │  └─ sync.py                # backfill, daily sync, re-sync window, upserts
│  ├─ nutrition/
│  │  ├─ usda.py                # FDC search + local cache
│  │  ├─ foods.py               # custom foods, recipes, servings
│  │  └─ nl_parse.py            # Claude-powered natural-language parsing
│  ├─ analysis/
│  │  ├─ food_day.py            # food-day boundaries
│  │  ├─ trend.py               # smoothed weight trend
│  │  ├─ tdee.py                # Garmin and adaptive expenditure estimates
│  │  ├─ projection.py          # 28-day projection + uncertainty
│  │  ├─ targets.py             # calorie / macro targets with guardrails
│  │  └─ units.py
│  ├─ coach/
│  │  ├─ llm.py                 # backend interface: subscription | api
│  │  ├─ tools.py               # read-only data tools exposed to Claude
│  │  └─ prompts.py
│  ├─ scheduler.py
│  └─ web/
│     ├─ app.py                 # DMC AppShell, navigation, theme
│     ├─ components/
│     └─ pages/                 # today, add_food, foods, trends, projection, coach, settings
├─ tests/
│  ├─ fixtures/                 # synthetic Garmin/USDA payloads (never real data)
│  └─ ...
└─ data/                        # git-ignored: gwc.db, garmin_tokens/, logs/, backups/
```

---

## 4. Garmin sync

### 4.1 Authentication and 2FA
- `GARMIN_EMAIL` / `GARMIN_PASSWORD` live in `.env` (git-ignored); the user fills them in.
- Tokens are stored in `data/garmin_tokens/` and refresh automatically; a full login happens only when the refresh token is expired or revoked.
- Login runs from **Settings → Garmin → Connect**. If Garmin asks for an MFA code, the app shows a modal and resumes the paused login with the code (`resume_login`).
- Unattended (scheduled) syncs **never** start a fresh login. If tokens are invalid they stop and show a "Reconnect Garmin" banner.

### 4.2 What is synced
Stored both as normalized columns and as raw JSON (`garmin_raw`) so data can be re-parsed later without re-fetching.

| Table | Contents |
|---|---|
| `body_comp` | weight, body fat %, muscle mass, bone mass, body water %, BMI (per weigh-in) |
| `daily_summary` | total / active / resting (BMR) kcal, steps, resting HR, stress, Body Battery |
| `sleep` | start/end timestamps, duration, stages, sleep score, HRV, `calendar_date` (wake date), nap flag |
| `activity` | type, start, duration, distance, kcal |
| `setting` → `garmin.last_device_sync_at` | watch's last upload time to Garmin (`get_sync_timestamp`, for provisional-day detection) |

Requests per sync: body composition and activities are fetched once for the whole window; daily summary and sleep once per day; plus one device-sync timestamp. A daily sync is ~14 requests; the 30-day backfill ~66.

### 4.3 Schedule
- **First connect:** backfill the last 30 days.
- **Daily at 09:01** local: re-fetch the last 3 calendar days plus today (Garmin finalizes daily totals and sleep late). Upserts are idempotent.
- **Catch-up:** on app start, and on wake (APScheduler `coalesce=True` with a long `misfire_grace_time`), if the last successful scheduled sync predates the most recent 09:01, sync now.
- **Manual "Sync now" button:** same window as the daily sync (today + last 3 days), subject to §4.4.
- **Provisional days:** if the watch's last upload was before a day ended, that day is marked *provisional* in the UI and the next sync fills it in. Projections and targets are recomputed whenever synced data changes.
- Only one sync runs at a time (DB-backed lock).

### 4.4 Rate limiting (self-imposed)
Garmin publishes no limits for the endpoints `garminconnect` uses (the 200/6,000-calls-per-day figures belong to the official partner API and don't apply). In practice the **login** flow is the sensitive part: repeated failed logins have produced account-level HTTP 429 blocks lasting 48+ hours. Data endpoints are more tolerant but still return 429 when hammered. The app therefore enforces conservative, configurable limits in `garmin/guard.py`, through which every Garmin request passes:

| Guard | Default |
|---|---|
| Spacing between Garmin requests | ≥ 1 s |
| Garmin requests per rolling 24 h (all syncs) | 500 (a daily sync is ~25 requests; the 30-day backfill is ~200) |
| Manual sync cooldown (since the start of any sync) | 30 min |
| Manual syncs per calendar day | 6 |
| Login attempts per rolling 24 h | 3; no automatic retry after a failure |
| On HTTP 429 / Cloudflare challenge during data sync | stop immediately; lock out 1 h → 4 h → 24 h if it happens again within 24 h of the previous lockout ending |
| On HTTP 429 during login | lock out 24 h |

The Sync button is disabled while a guard applies and shows why (e.g. "Available in 12 min", "Daily manual limit reached", "Garmin rate-limited us — retry after 15:40"). Scheduled and catch-up syncs respect lockouts and run once a lockout expires.

---

## 5. Food day

The user eats past midnight and usually sleeps after 04:00, so a food day is not a calendar day.

- **Food day D ends** at the start time of the main (non-nap) Garmin sleep whose `calendar_date` (wake date) is D+1. That works for both a 04:15 and a 23:00 bedtime.
- **Fallback:** if there is no main sleep for D+1 (all-nighter, watch not worn, not yet synced), D ends at **09:00 on D+1** (configurable).
- **Live "today":** until last night's sleep is synced, the current food day uses the fallback rule (before 09:00 → still yesterday).
- Entries store raw UTC timestamps and the time zone. Food day is **computed, not stored**, so entries near a boundary move to the correct day once sleep data arrives. Boundaries are computed on the fly from `sleep` (cheap enough that no cache table is needed). A sleep only counts if it starts between 12:00 on D and 18:00 on D+1.
- Any single entry can be manually pinned to a specific food day.
- A food day can be marked **incomplete** (partially logged) so it is excluded from averages.
- Garmin daily totals are per calendar day and are mapped to the food day of the same date. The few post-midnight hours land in the next calendar day; over 7-day averages this mostly cancels out.

---

## 6. Nutrition

### 6.1 Foods
| Source | Details |
|---|---|
| USDA FoodData Central (A) | Search Foundation, SR Legacy, Survey (FNDDS) and Branded foods. Needs a free key from api.data.gov (`USDA_API_KEY`). Searches use `POST /foods/search` (FDC's front end rejects GET searches filtered to "Survey (FNDDS)"). A food is cached in `food` when picked; SR Legacy/Foundation portions cost one extra detail request. |
| Custom foods (B) | Nutrients per serving or per 100 g, named servings (e.g. "1 scoop = 31 g"). |
| Recipes (B) | Ingredients with amounts; total yield in grams or servings; nutrients computed. |
| Favorites / recents | Quick re-logging. |

All foods are normalized to nutrients per 100 g plus named servings. The log dialog prefills the amount and unit last logged for that food; otherwise it uses a typical single portion ("1 large", label serving) rather than USDA's first listed portion.

### 6.2 Log entries (`food_log`)
`eaten_at` (UTC) + `tz`, `food_id`, `grams`, a **snapshot** of nutrient values at log time (editing a food later doesn't change history), optional `food_day_override`, `source` (search / custom / recipe / ai), and a note.

### 6.3 Natural-language logging (D)
1. The user types e.g. "2 eggs scrambled in butter and a slice of sourdough at 1:30am".
2. Claude parses it into items, amounts and time, using `search_usda` and `search_my_foods` tools to ground each item in a real food record rather than guessing nutrients.
3. The app shows an editable review table (food match, amount, unit, nutrients). Nothing is saved until the user confirms.

---

## 7. Analysis

### 7.1 Weight trend
Exponentially weighted moving average of daily weigh-ins (α = 0.1 by default, Hacker's Diet style), warmed up as a running mean over the first 1/α weigh-ins so an outlier first reading doesn't skew it for weeks. Multiple weigh-ins per day are averaged; missing days are carried by the trend.

### 7.2 Energy density of weight change
Instead of a flat 3,500 kcal/lb, the fat vs. lean split of loss follows Forbes' relation using fat mass from the scale:
- fat fraction of a change = FM / (FM + 10.4 kg)
- ρ = fat_fraction × 9,440 kcal/kg + (1 − fat_fraction) × 1,816 kcal/kg

(≈ 3,400 kcal/lb at 30 kg fat mass.)

### 7.3 Expenditure (TDEE) estimates
| Method | Calculation | Availability |
|---|---|---|
| **Garmin** | mean Garmin total kcal (resting + active) over the last 7 full food days | immediately |
| **Adaptive (trend-based)** | mean logged intake − ρ × weight slope over a trailing window (default 21 days; needs weigh-ins spanning ≥ 14 days and ≥ 10 complete food days). The slope is a least-squares fit to the daily weigh-ins, not the EWMA trend's change, which lags ~10 days | after ~2–3 weeks of food logging; shows "calibrating" until then |

Both include whatever workouts you did. Each estimate also reports the mean **net workout kcal** per day over its own days (Garmin activity kcal minus the resting burn for the activity's duration, at the rate of finished days, since Garmin accrues resting kcal through the day), and a **base** burn without it, which targets start from (§7.5).

### 7.4 Projection (28 days)
- Scenarios: **at your target** (a typical day's calorie target, meaning the base target plus the eat-back share of the average workout, with the burn estimate that sets targets; works from day one), and **as eaten** with each available TDEE method (mean intake over the complete days among the last 7 food days; shown once ≥ 3 are complete).
- Simulated day by day. Expenditure falls as weight drops (default −22 kcal/day per kg lost, configurable), and ρ is updated as fat mass changes.
- Uncertainty band (10th–90th percentile) via Monte Carlo: bootstrap the daily intakes and sample TDEE from its estimate's standard error (Garmin's includes a 5 % systematic term).
- Chart: trend weight history + both projections with bands, goal line, and data-completeness indicators ("6/7 days logged", provisional days).
- Warning when < 5 of the last 7 food days are complete.

### 7.5 Targets
From goal loss per month (in the display unit):
- Daily deficit = goal × ρ / 30.44 days.
- Calorie target = base TDEE (without workouts, §7.3) − deficit, plus that food day's workouts (below). Settings picks the TDEE: **Auto** (adaptive once calibrated, else Garmin), Garmin, or Trend-based.
- **Same-day workouts:** each food day adds a share (default 75 %, set in Settings; 0 turns workout credit off) of the net kcal of the Garmin activities that started in it, live, so a manual sync after a workout raises that day's target. It adds on top of a manual calorie override too. The extra kcal go 25/75 to fat/carbs (all to whichever isn't overridden), with fiber and sugar scaled along. Because the base leaves workouts out, a workout is credited once, on its own day, not again through next week's burn average. Rest days are correspondingly a little lower than the all-in burn. Only recorded activities count; extra walking stays in the base.
- Guardrails: warn and cap if the goal exceeds ~1 % of body weight per week (configurable); calorie target never below Garmin's resting kcal (hard floor — the app shows the slower rate this allows).
- Protein: 2.4 g per kg of lean mass (from the scale; ≈ 1.1 g/lb).
- Fat: max(25 % of kcal, 0.5 g/kg body weight).
- Carbs: remaining calories.
- Fiber: 14 g per 1,000 kcal.
- Sugar: ≤ 10 % of kcal (total sugar is tracked, so this is a soft guide).
- Sodium: ≤ 2,300 mg.
- Every target can be overridden manually; overridden calories, protein or fat feed into the macros derived from them.
- A food day's base targets are frozen into `daily_target` the first time they're computed, so later syncs don't move them; the day's workouts are added on read. Saving the goal, overrides or analysis settings re-computes today; earlier days keep theirs. Days frozen before the workout adjustment existed (no `eat_back_pct` in their details) get no workout credit.

---

## 8. LLM coach

### 8.1 Backends (`coach/llm.py`)
| Backend | Auth | Notes |
|---|---|---|
| `subscription` (default) | Claude Code login on this machine (Max 5x) via `claude-agent-sdk`, which bundles the CLI | `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are blanked in the SDK subprocess environment so usage can't silently bill a key. Built-in tools are removed (`tools=[]`), user/project settings aren't loaded, and only the app's tools (an in-process MCP server) are allowed. Tool search is off: the tools load upfront. |
| `api` | `ANTHROPIC_API_KEY` via `anthropic` SDK | Streaming manual tool loop; server-side refusal fallbacks (`fallbacks: "default"`) on models that support them. |

Settings → Coach picks the backend and, per feature (coach chat, food logging, weekly summary), a model — an alias (`opus`, `sonnet`, `haiku`, `fable`; aliases follow the latest version) or a full model ID — and an effort level. Defaults: `opus`; effort medium / medium / high. An optional fallback model applies to the subscription backend. Chat history is flattened into one prompt for the subscription backend and sent as messages to the API.

LLM calls run as background jobs (`coach/jobs.py`); pages poll for streamed text, tool activity and the result, so a long turn never blocks a request.

### 8.2 Tools exposed to Claude (read-only, `coach/tools.py`)
`get_overview`, `get_weight_trend`, `get_body_comp`, `get_daily_nutrition`, `get_food_entries`, `get_targets`, `get_garmin_daily`, `get_sleep`, `get_activities`, `get_projection`, plus `search_my_foods` / `search_usda` for food parsing. Weights come back in the display unit. Claude never writes to the database; proposed food entries go through the review table.

### 8.3 Features
- **Natural-language logging** (Add food → Describe): structured output (a JSON schema) lists items, each tied to one of the user's foods, a USDA result Claude actually looked up, or — only when nothing fits — Claude's own estimate (flagged). Nutrients are computed from the matched record. The review shows each item with editable grams and an include toggle; nothing is saved until confirmed. Entries are logged with source `ai` and the original description as a note.
- **Coach chat page** with persisted conversations, suggested questions, and streamed replies rendered as Markdown.
- **Weekly summary:** checked daily 10 minutes after the sync time and on startup; on the configured weekday (default Monday) the previous Monday–Sunday week is summarized if it hasn't been. Also on demand. Stored in `coach_summary`.
- Health and food data sent to Claude leaves this machine (Anthropic); this is noted in Settings and on the Coach page.

---

## 9. Web UI

DMC `AppShell` with a sidebar on desktop and bottom navigation below Mantine's `md` breakpoint (992 px), so the OnePlus Open gets touch navigation and full-width content both folded (~380 px) and unfolded (~750–900 px); light/dark theme. On folded widths tab rows drop their icons, and wide tables drop secondary columns rather than scroll.

| Page | Contents |
|---|---|
| **Today** (home) | Progress bars for the 7 nutrients vs. targets, remaining kcal, entry timeline for the current food day, quick add (favorites / recents) |
| **Add food** | Tabs: Search USDA · My foods & recipes · Describe (AI); amount + unit input (g/oz/servings); time eaten (defaults to now) |
| **Foods** | Create/edit custom foods and recipes |
| **Trends** | Weight (raw + trend), body fat, intake vs. Garmin burn and target, sleep, steps, activities; 30d / 90d / 1y / All range |
| **Projection** | 4-week projection chart (§7.4), TDEE estimates, data completeness |
| **Coach** | Chat + weekly summaries |
| **Settings** | Units (weight lb/kg, food g/oz), goal loss per month, target overrides, food-day fallback hour, sync time, LLM backend/model, Garmin connection + 2FA, sync status and **Sync now** button with guard state |

A header badge shows last sync time, provisional days, and any lockout.

---

## 10. Configuration (`.env`)

```
GARMIN_EMAIL=
GARMIN_PASSWORD=
USDA_API_KEY=
GWC_LLM_BACKEND=subscription      # subscription | api
ANTHROPIC_API_KEY=                # only for the api backend
GWC_HOST=127.0.0.1
GWC_PORT=8060
GWC_TZ=                           # blank = system time zone
```

User preferences (units, goal, sync time, guard limits, etc.) are stored in the database and edited in Settings.

---

## 11. Running

- `uv run gwc serve` starts waitress on `127.0.0.1:8060` plus the scheduler.
- `scripts/install_autostart.ps1` registers a Windows Task Scheduler task that runs the app at log on.
- Phone access: `tailscale serve --bg 8060` publishes the app at `https://<machine>.<tailnet>.ts.net` to tailnet devices only. Nothing is exposed on the LAN or internet.
- `data/backups/`: after each successful Garmin sync the SQLite DB is copied with SQLite's online backup API to `gwc-YYYY-MM-DD.db` (one file per local day; a later sync that day refreshes it). The newest 14 are kept.

---

## 12. Testing

- Unit tests for food-day boundaries, trend, ρ, TDEE, projection, targets, unit conversion, and the sync guard (with a fake clock).
- Garmin and USDA clients tested against synthetic fixtures; no network in tests.
- Sync tests use a fake Garmin client to verify idempotent upserts, the re-sync window, provisional days, and lockout behavior.
- LLM tests mock the backend and verify tool wiring and that parsed entries require confirmation.
- Smoke test that every Dash page renders.

---

## 13. Milestones

Each milestone ends with tests passing and a commit pushed to `main`.

1. **Scaffold** — `uv` project, `pyproject.toml`, `.gitignore`, `.env.example`, config, SQLAlchemy models, Alembic baseline, `gwc serve` with an empty DMC AppShell and navigation.
2. **Garmin** — client with token storage and 2FA flow in Settings, rate-limit guard, 30-day backfill, daily 09:01 sync, startup/wake catch-up, manual Sync button, provisional days, sync status badge.
3. **Nutrition** — USDA search + cache, custom foods and recipes, food log, food-day boundaries, Today page with progress bars, unit switching.
4. **Analysis** — weight trend, ρ, TDEE (both methods), projection with uncertainty, targets with guardrails; Trends and Projection pages.
5. **LLM** — backend interface (subscription default, API alternative), natural-language logging with review, coach chat, weekly summary.
6. **Polish & ops** — mobile layout pass (folded and unfolded), autostart script, Tailscale instructions, DB backups, README.

---

## 14. Risks

| Risk | Mitigation |
|---|---|
| Garmin changes auth or blocks the library | All Garmin access isolated in `garmin/client.py`; keep `garminconnect` current; headless-browser login as a fallback option |
| Account rate-limited by Garmin | Conservative guard (§4.4), token reuse, no automatic re-login |
| Unofficial access is against Garmin's ToS | Low-volume personal use only |
| Garmin calorie burn is inaccurate | Adaptive TDEE method shown alongside |
| Partial food logging biases estimates | "Incomplete" day flag, completeness warnings |
| Anthropic changes subscription/SDK terms | `api` backend available behind the same interface |
| Personal data accidentally committed | Everything under `data/` and `.env` git-ignored from the first code commit; fixtures are synthetic |
