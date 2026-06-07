# AI Agency Lead Agent

A dependency-free Python agent for a fictional AI automation agency. It accepts a client request, analyzes it, extracts structured data, classifies and scores the lead, stores it in SQLite, appends a Google Sheets row, sends a Telegram notification, drafts a reply, and creates a follow-up task.

The app requires OpenAI for lead extraction, summary, spam detection, and reply drafting. It uses the OpenAI Responses API for the first analysis/drafting call, then runs a second OpenAI reviewer call to approve, correct, or block non-spam replies.

The scoring and lead classification stay deterministic after OpenAI extraction so routing remains predictable.

## Run the Web App

```powershell
$env:OPENAI_API_KEY="sk-your-key"
py agency_agent.py serve
```

Open `http://127.0.0.1:8000`.

## Run from the CLI

```powershell
$env:OPENAI_API_KEY="sk-your-key"
py agency_agent.py analyze "We are a logistics company. We receive many client emails and want to automate support and order-status questions. Budget around EUR 3000. Need something this month."
```

## OpenAI Setup

Set an API key before running the app:

```powershell
$env:OPENAI_API_KEY="sk-your-key"
py agency_agent.py serve
```

Optional model override:

```powershell
$env:OPENAI_MODEL="gpt-5-mini"
```

By default the app uses `gpt-5-mini`, a cost-efficient model supported by the OpenAI Responses API. You can swap it with another OpenAI text model that supports structured outputs.

If `OPENAI_API_KEY` is not set, lead analysis fails instead of falling back to local template logic.

## Outputs

The app writes local data under `data/`:

- `agency_agent.sqlite3`: leads and follow-up tasks
- `google_sheets_fallback.csv`: local fallback when `GOOGLE_SHEETS_WEBHOOK_URL` is not configured
- `telegram_outbox.jsonl`: local fallback when Telegram credentials are not configured

## Optional Integrations

Google Sheets is supported through a Google Apps Script webhook. Set:

```powershell
$env:GOOGLE_SHEETS_WEBHOOK_URL="https://script.google.com/macros/s/YOUR_DEPLOYMENT/exec"
```

Telegram is supported through the Bot API. Set:

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:bot-token"
$env:TELEGRAM_CHAT_ID="123456789"
```

If these variables are not present, the agent still completes the workflow using local fallback files.

## Test

```powershell
py -m unittest discover -s tests
```
