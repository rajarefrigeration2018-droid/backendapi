# Mistrio Backend

FastAPI backend serving the Mistrio User App, Partner App and Admin Panel.

## Setup (GitHub Codespaces)

```bash
pip install -r requirements.txt
cp _env.example .env      # then fill in real values
uvicorn app.main:app --reload
```

Open `/docs` for interactive API documentation.

## Deploy (Railway)

1. New Project → Deploy from GitHub repo
2. Add every variable from `_env.example` under Variables
3. Railway auto-detects the Procfile
4. Settings → Networking → Generate Domain, then point your CNAME at it

## Structure

```
app/
  main.py          FastAPI app, CORS, error handlers, router registration
  config.py        All env-driven settings
  database.py      SQLAlchemy Core helper (db.fetch_all / fetch_one / execute)
  dependencies.py  Auth guards, response envelope, pagination
  core/
    security.py    JWT (3 audiences), password hashing, OTP
  routers/
    config.py      /app-config — drives all hardcode-free behaviour
```

## Response format

Every endpoint returns:

```json
{ "success": true, "message": "Success", "data": {}, "error_code": null }
```
