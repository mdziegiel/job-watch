# Job Watch Assistant

Self-hosted job monitoring and alerting for Michael Dziegiel's senior infrastructure job search.

## Search criteria

- Titles: Network Administrator, Systems Administrator, IT Administrator, Endpoint Engineer, Desktop Engineer, M365 Engineer
- Location: Lowell MA, 50 mile radius
- Fully remote positions nationwide
- Target salary: at/above target salary range
- Ideal match: hybrid within target commute range, at/above target salary, bonus potential
- Jobs below criteria are flagged and scored, not hidden.

## Sources

- Indeed through the Indeed MCP connector at `https://mcp.indeed.com/claude/mcp`
- LinkedIn public listings scraper
- Dice public listings scraper
- ZipRecruiter public listings scraper
- Deduplication by canonical URL or normalized title/company/location

## Features

- FastAPI backend, React frontend, SQLite persistence
- Dark glassmorphism theme matching the Resume Builder family
- Automatic scrape loop every 6 hours plus manual scrape button
- Status pipeline: New, Saved, Applied, Interview, Rejected, Offer
- Telegram and optional SMTP email alerts for newly discovered jobs
- Notification settings page with alert window, channel toggles, max alerts per cycle, SMTP fields, and quiet hours
- Claude match scoring blended with deterministic profile scoring so title, location, salary, and role fit produce distinct scores
- Date-aware job cards, newest-first default sorting, score/date sorting controls, and date-range filtering
- One-line fit summary and match breakdown
- Gold ideal-match badge for hybrid, within 40 miles, at/above target salary
- Filterable job list and Kanban pipeline
- Job detail with description, salary, apply URL, score, and generated docs
- Generate Cover Letter using the job plus Michael's Resume Builder SQLite data
- Generate Tailored Resume for ATS optimization
- DOCX/PDF export for generated documents

## Screenshots

Place screenshots here after first production deployment:

- `screenshots/dashboard.png`
- `screenshots/jobs.png`
- `screenshots/job-detail.png`
- `screenshots/kanban.png`

## Docker quickstart

```bash
cp .env.example .env
# edit .env with Anthropic and Telegram credentials
docker compose up -d --build
```

Open:

```text
http://localhost:8085
```

Health check:

```bash
curl http://localhost:8085/api/health
```

## Environment

```text
DATABASE_PATH=/data/job-watch.sqlite
RESUME_BUILDER_DB=/resume-builder-data/resume-builder.sqlite
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-20250514
TELEGRAM_BOT_TOKEN=
TELEGRAM_HOME_CHANNEL=
INDEED_MCP_URL=https://mcp.indeed.com/claude/mcp
SCHEDULER_ENABLED=1
ALERT_BATCH_LIMIT=10
NOTIFICATION_WINDOW_HOURS=24
SMTP_ENABLED=0
SMTP_SERVER=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_TO=
QUIET_HOURS_START=
QUIET_HOURS_END=
DASHBOARD_URL=http://10.10.10.237:8085
```

## API highlights

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/rescore`
- `POST /api/scrape`
- `POST /api/jobs/{id}/status`
- `POST /api/jobs/{id}/generate`
- `GET /api/docs/{id}/export/docx`
- `GET /api/docs/{id}/export/pdf`

## Notes

Public job boards frequently change markup and block automated clients. The app keeps source-specific scraping isolated, deduplicates aggressively, and degrades to zero new jobs instead of inventing fake listings.
