# Scheduling the daily scrape

Colab cannot do this. Notebooks only run while a browser session is attached,
free runtimes disconnect after ~90 minutes idle, and there is no scheduler.
Pro+ background execution keeps a session alive but still will not start one.

This folder runs the scrape on GitHub Actions instead: free, real cron, Chrome
already on the runner, and each day's CSV committed into the repo so the panel
builds itself with a timestamped, auditable history.

## Setup (about five minutes)

1. Create a repository — private is fine.

2. Copy these files in, keeping the paths:

   ```
   .github/workflows/daily-scrape.yml
   othoba_daily.py
   build_panel.py
   requirements.txt
   data/.gitkeep
   ```

3. Settings → Actions → General → Workflow permissions →
   **Read and write permissions**. Without this the commit step cannot push.

4. Actions tab → *Daily Othoba scrape* → **Run workflow**. Do this once by
   hand before trusting the schedule; watch the log and confirm a CSV lands in
   `data/`.

5. Leave it. It runs at 03:00 Asia/Dhaka daily.

## Changing the time

`cron` is UTC only. Dhaka is UTC+6, so subtract six hours:

| local (Dhaka) | cron |
|---|---|
| 03:00 | `0 21 * * *` |
| 06:00 | `0 0 * * *` |
| 12:00 | `0 6 * * *` |
| 21:00 | `0 15 * * *` |

GitHub may delay a scheduled job by 5–30 minutes when the queue is busy. It
will not run early. For a daily index that jitter does not matter, but do not
schedule two jobs expecting an exact gap between them.

## Why the run can fail on purpose

`othoba_daily.py` exits non-zero when it collects fewer than `MIN_ROWS` rows,
or when more than a third of categories error. GitHub emails you on a failed
run, so a broken scrape becomes a notification rather than silence.

This is deliberate. A silent partial harvest does not look like an error, it
looks like a quiet day — and a missing day cannot be back-filled later, because
the site only ever shows today's prices. Tune `MIN_ROWS` in the workflow once
you know your normal row count; set it around 60% of a healthy run.

## Building the panel

```
python build_panel.py            # -> othoba_panel.csv   (long format)
python build_panel.py --wide     # -> othoba_panel_wide.csv (SKU x date)
```

Daily files stay immutable so the raw record is auditable; the panel is derived
and can be rebuilt at any time. The `--wide` output matches the SKU-by-date
layout the Chaldal pipeline expects.

## Two things to watch

**Scheduled workflows are disabled after 60 days without repository activity.**
The bot's daily commits normally count, but if you ever pause the scrape, check
the Actions tab when you resume.

**The 6-hour job ceiling.** `FOOD_ONLY=1` keeps this to roughly 50 categories,
comfortably inside the limit. If you widen the scope, split the run across
several jobs by category rather than raising the timeout, which cannot exceed
360 minutes.

## Alternatives

| option | cost | notes |
|---|---|---|
| **GitHub Actions** | free | recommended; versioned data, cron, alerts |
| Your own PC (cron / Task Scheduler) | free | only runs when the machine is on and awake |
| Small VPS (Hetzner, DigitalOcean) | ~$4–5/mo | most reliable; full control over timing |
| Cloud Run Jobs + Cloud Scheduler | ~free tier | more setup, scales well |
| PythonAnywhere scheduled tasks | free tier | headless Chrome is painful there; not advised |

If the panel ever becomes central to a publication, a cheap VPS is worth it —
GitHub Actions is free precisely because it comes with no uptime guarantee.
