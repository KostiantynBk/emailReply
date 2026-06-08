import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import textwrap
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "agency_agent.sqlite3"
SHEETS_FALLBACK_CSV = DATA_DIR / "google_sheets_fallback.csv"
TELEGRAM_OUTBOX = DATA_DIR / "telegram_outbox.jsonl"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"


@dataclass
class LeadAnalysis:
    analysis_source: str
    is_spam: bool
    spam_reason: str | None
    company_type: str
    request_summary: str
    automation_goals: list[str]
    budget_amount: int | None
    budget_currency: str | None
    timeline: str
    urgency: str
    integrations: list[str]
    lead_class: str
    score: int
    score_reasons: list[str]
    recommended_action: str
    reply_draft: str
    review_status: str
    review_notes: list[str]
    follow_up_task: str
    follow_up_due_at: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            raw_request TEXT NOT NULL,
            analysis_source TEXT NOT NULL DEFAULT 'openai',
            is_spam INTEGER NOT NULL DEFAULT 0,
            spam_reason TEXT,
            company_type TEXT NOT NULL,
            request_summary TEXT NOT NULL,
            automation_goals TEXT NOT NULL,
            budget_amount INTEGER,
            budget_currency TEXT,
            timeline TEXT NOT NULL,
            urgency TEXT NOT NULL,
            integrations TEXT NOT NULL,
            lead_class TEXT NOT NULL,
            score INTEGER NOT NULL,
            score_reasons TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            reply_draft TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'not_reviewed',
            review_notes TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS follow_up_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
        """
    )
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
    }
    migrations = {
        "analysis_source": "ALTER TABLE leads ADD COLUMN analysis_source TEXT NOT NULL DEFAULT 'openai'",
        "is_spam": "ALTER TABLE leads ADD COLUMN is_spam INTEGER NOT NULL DEFAULT 0",
        "spam_reason": "ALTER TABLE leads ADD COLUMN spam_reason TEXT",
        "review_status": "ALTER TABLE leads ADD COLUMN review_status TEXT NOT NULL DEFAULT 'not_reviewed'",
        "review_notes": "ALTER TABLE leads ADD COLUMN review_notes TEXT NOT NULL DEFAULT '[]'",
    }
    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)
    conn.commit()
    return conn


def extract_budget(text: str) -> tuple[int | None, str | None]:
    text = text.replace("\u20ac", "eur ")
    patterns = [
        r"(?:€|eur\s*)\s?([0-9][0-9., ]*)",
        r"([0-9][0-9., ]*)\s?(?:€|eur|euro|euros)\b",
        r"(?:\$|usd\s*)\s?([0-9][0-9., ]*)",
        r"([0-9][0-9., ]*)\s?(?:\$|usd|dollars)\b",
    ]
    lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lower, re.IGNORECASE)
        if not match:
            continue
        amount = int(re.sub(r"[^0-9]", "", match.group(1)) or "0")
        currency = "EUR" if "€" in match.group(0) or "eur" in match.group(0) or "euro" in match.group(0) else "USD"
        return amount, currency
    return None, None


def detect_company_type(text: str) -> str:
    industries = {
        "logistics": ["logistics", "shipping", "freight", "delivery", "warehouse", "transport"],
        "ecommerce": ["ecommerce", "e-commerce", "shopify", "online store", "retail"],
        "healthcare": ["clinic", "healthcare", "medical", "patient"],
        "finance": ["finance", "fintech", "accounting", "invoice", "bank"],
        "real estate": ["real estate", "property", "brokerage"],
        "education": ["school", "university", "education", "course"],
    }
    lower = text.lower()
    for industry, keywords in industries.items():
        if any(keyword in lower for keyword in keywords):
            return industry
    company_match = re.search(r"we are (?:a|an)\s+([^.\n]+?)(?: company| agency| business| firm)?[.\n]", lower)
    if company_match:
        return company_match.group(1).strip()
    return "unknown"


def extract_goals(text: str) -> list[str]:
    lower = text.lower()
    goals: list[str] = []
    signals = {
        "email triage and support automation": ["email", "emails", "support"],
        "order-status automation": ["order-status", "order status", "tracking", "shipment status"],
        "customer self-service chatbot": ["chatbot", "self-service", "faq"],
        "lead qualification": ["qualify", "lead", "sales"],
        "appointment scheduling": ["appointment", "booking", "calendar"],
        "document processing": ["invoice", "document", "pdf", "ocr"],
    }
    for goal, keywords in signals.items():
        if any(keyword in lower for keyword in keywords):
            goals.append(goal)
    if not goals and ("automate" in lower or "automation" in lower):
        goals.append("business-process automation")
    return goals or ["needs discovery"]


def extract_integrations(text: str) -> list[str]:
    lower = text.lower()
    integrations = []
    known = {
        "email inbox": ["email", "gmail", "outlook", "inbox"],
        "order management system": ["order", "orders", "oms"],
        "crm": ["crm", "hubspot", "salesforce", "pipedrive"],
        "helpdesk": ["zendesk", "intercom", "freshdesk", "helpdesk"],
        "calendar": ["calendar", "calendly"],
        "slack": ["slack"],
        "telegram": ["telegram"],
        "google sheets": ["google sheets", "spreadsheet"],
    }
    for name, keywords in known.items():
        if any(keyword in lower for keyword in keywords):
            integrations.append(name)
    return integrations


def classify_timeline(text: str) -> tuple[str, str]:
    lower = text.lower()
    if any(term in lower for term in ["this month", "asap", "urgent", "immediately", "next week"]):
        return "this month", "high"
    if any(term in lower for term in ["quarter", "q1", "q2", "q3", "q4"]):
        return "this quarter", "medium"
    if any(term in lower for term in ["no rush", "later", "someday"]):
        return "flexible", "low"
    return "not specified", "medium"


def score_lead(
    budget_amount: int | None,
    urgency: str,
    goals: list[str],
    integrations: list[str],
    company_type: str,
) -> tuple[int, list[str], str, str]:
    score = 0
    reasons: list[str] = []

    if budget_amount is None:
        score += 10
        reasons.append("Budget is missing, so buying intent needs qualification.")
    elif budget_amount >= 5000:
        score += 35
        reasons.append("Budget is strong for an automation project.")
    elif budget_amount >= 2500:
        score += 25
        reasons.append("Budget is viable for a focused first automation.")
    elif budget_amount >= 1000:
        score += 12
        reasons.append("Budget may support a narrow MVP.")
    else:
        score += 4
        reasons.append("Budget is likely too low for a custom build.")

    if urgency == "high":
        score += 25
        reasons.append("Timeline shows immediate need.")
    elif urgency == "medium":
        score += 15
        reasons.append("Timeline is active but not urgent.")
    else:
        score += 5
        reasons.append("Timeline is flexible.")

    if goals and goals != ["needs discovery"]:
        score += min(25, 8 * len(goals))
        reasons.append("Use case is specific enough to scope.")
    else:
        score += 3
        reasons.append("Use case needs discovery.")

    if integrations:
        score += min(10, 3 * len(integrations))
        reasons.append("Integration points are identifiable.")

    if company_type != "unknown":
        score += 5
        reasons.append("Industry context is clear.")

    score = max(0, min(score, 100))
    if score >= 75:
        lead_class = "hot"
        action = "Book a discovery call within 24 hours and send a short MVP scope."
    elif score >= 50:
        lead_class = "warm"
        action = "Ask two qualification questions, then offer a discovery call."
    else:
        lead_class = "cold"
        action = "Send a lightweight qualification reply before investing sales time."

    return score, reasons, lead_class, action


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def call_openai_json(
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    payload = {
        "model": openai_model(),
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 2500,
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    text = response_text(body)
    if not text:
        raise RuntimeError("OpenAI response did not include output text.")
    return json.loads(text)


LEAD_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "company_type": {"type": "string"},
        "is_spam": {"type": "boolean"},
        "spam_reason": {"type": ["string", "null"]},
        "request_summary": {"type": "string"},
        "automation_goals": {"type": "array", "items": {"type": "string"}},
        "budget_amount": {"type": ["integer", "null"]},
        "budget_currency": {"type": ["string", "null"]},
        "timeline": {"type": "string"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
        "integrations": {"type": "array", "items": {"type": "string"}},
        "reply_draft": {"type": "string"},
    },
    "required": [
        "company_type",
        "is_spam",
        "spam_reason",
        "request_summary",
        "automation_goals",
        "budget_amount",
        "budget_currency",
        "timeline",
        "urgency",
        "integrations",
        "reply_draft",
    ],
}


REPLY_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["approved", "corrected", "blocked"]},
        "notes": {"type": "array", "items": {"type": "string"}},
        "corrected_reply": {"type": "string"},
    },
    "required": ["status", "notes", "corrected_reply"],
}


def normalize_string_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or fallback


def analyze_request_with_openai(raw_request: str) -> LeadAnalysis:
    extracted = call_openai_json(
        "You analyze inbound leads for an AI automation agency. Extract only facts grounded in the request. "
        "Mark is_spam true when the message is promotional spam, phishing-like, irrelevant outreach, link spam, "
        "or not a plausible client request for automation services. If is_spam is true, reply_draft must be a short "
        "client-facing clarification message explaining that the email was marked as spam and what a genuine business "
        "request should include. "
        "Draft a natural, concise, client-facing email reply from the agency to the potential client. "
        "Assume the client already knows they contacted an automation agency, so do not introduce the sender, "
        "do not say 'I run an agency', and do not explain what the agency is. Write like a real human replying "
        "The reply must start with a simple greeting such as 'Hi,' or 'Hello,'. Use 'we' and 'our' consistently "
        "for the agency voice; do not switch between 'I' and 'we'. Do not begin with a blunt recap such as "
        "'You want to...' or 'You are looking to...'. Prefer phrasing like 'For this, we'd recommend...' or "
        "'We'd start by...' instead of 'A practical first step is...'. "
        "to an inbound email, not like a proposal, checklist, or generated sales script. Do not use section labels "
        "such as 'Pilot approach:', 'Next step:', or 'Two quick questions:' unless the wording flows naturally. "
        "Do not open with a deadline, call request, or budget sentence. Start with a simple acknowledgement of the "
        "problem and a practical first step. Keep implementation detail light; mention the workflow at a high level "
        "instead of listing every component. Avoid phrases like 'understood', 'confirm feasibility', 'realistic outcome', "
        "'typical MVP', 'intent classifier', 'canned auto-replies', and 'cost split'. Ask at most two useful qualification "
        "questions. Do not ask for sample email threads in the first reply unless the client has already offered them. "
        "Do not propose exact meeting times, dates, or timezone placeholders unless the client's timezone and availability "
        "are explicitly provided. Never write '(your timezone)' or ask the client to confirm their timezone. If suggesting "
        "a call, use a plain sentence such as 'Happy to jump on a short call after that if useful.' "
        "Keep the tone warm, specific, and plain-spoken. Avoid promising delivery until requirements are confirmed. "
        "Target style example: 'Hi, thanks for sharing this. For this month, we'd recommend keeping the first version focused "
        "on the support emails that create the most repeat work: order-status questions and common client requests. The main "
        "thing to check is how your team can access order data today. Where does that data live, and roughly how many "
        "support emails do you receive each week? Once we know that, we can send a short pilot scope with timeline and pricing.'",
        f"Client request:\n{raw_request}",
        "lead_extraction",
        LEAD_EXTRACTION_SCHEMA,
    )

    company_type = str(extracted["company_type"]).strip() or "unknown"
    is_spam = bool(extracted["is_spam"])
    spam_reason = extracted["spam_reason"]
    if spam_reason is not None:
        spam_reason = str(spam_reason).strip() or None
    automation_goals = normalize_string_list(extracted["automation_goals"], ["needs discovery"])
    integrations = normalize_string_list(extracted["integrations"], [])
    budget_amount = extracted["budget_amount"]
    if budget_amount is not None:
        budget_amount = int(budget_amount)
    budget_currency = extracted["budget_currency"]
    if budget_currency is not None:
        budget_currency = str(budget_currency).upper()
    timeline = str(extracted["timeline"]).strip() or "not specified"
    urgency = str(extracted["urgency"]).strip().lower()
    if urgency not in {"low", "medium", "high"}:
        urgency = "medium"

    score, score_reasons, lead_class, recommended_action = score_lead(
        budget_amount,
        urgency,
        automation_goals,
        integrations,
        company_type,
    )
    due_hours = 168 if is_spam else 12 if lead_class == "hot" else 36 if lead_class == "warm" else 72
    follow_up_due = now_utc() + timedelta(hours=due_hours)
    reply = str(extracted["reply_draft"]).strip()
    if is_spam:
        lead_class = "spam"
        score = 0
        score_reasons = [spam_reason or "Message was marked as spam."]
        recommended_action = "Do not notify sales. Use the spam clarification reply only if manual review says it is appropriate."

    if is_spam:
        review_status = "spam_not_reviewed"
        review_notes = [spam_reason or "Message was marked as spam."]
    else:
        review = call_openai_json(
            "You are a strict reviewer for client-facing sales replies. Check the reply against the original request. "
            "Approve only if it is accurate, professional, grounded, natural, and does not overpromise. If needed, provide a corrected reply. "
            "The corrected reply must start with 'Hi,' or 'Hello,'. Use 'we' and 'our' consistently for the agency voice; "
            "do not mix 'I' and 'we'. Prefer a concise agency email that reads like a normal human reply, opens warmly, avoids "
            "sender introductions, avoids proposal-style labels, avoids blunt recaps like 'You want to...', asks no more than two questions, "
            "does not request sample email threads immediately, and ends with a clear next step. If the reply starts with "
            "a budget/deadline sentence or uses labels like 'Pilot approach:', correct it. If the reply invents meeting slots, "
            "dates, or timezone placeholders, correct it. If it says 'A practical first step is', rewrite it with a more natural "
            "agency phrasing such as 'we'd recommend' or 'we'd start by'.",
            json.dumps(
                {
                    "client_request": raw_request,
                    "structured_analysis": {
                        "company_type": company_type,
                        "automation_goals": automation_goals,
                        "budget_amount": budget_amount,
                        "budget_currency": budget_currency,
                        "timeline": timeline,
                        "urgency": urgency,
                        "lead_class": lead_class,
                        "score": score,
                        "recommended_action": recommended_action,
                    },
                    "reply_draft": reply,
                },
                indent=2,
            ),
            "reply_review",
            REPLY_REVIEW_SCHEMA,
        )
        review_status = str(review["status"])
        review_notes = normalize_string_list(review["notes"], [])
        if review_status in {"corrected", "approved"} and str(review["corrected_reply"]).strip():
            reply = str(review["corrected_reply"]).strip()
        if review_status == "blocked":
            recommended_action = "Review reply manually before sending; reviewer blocked the draft."

    return LeadAnalysis(
        analysis_source=f"openai:{openai_model()}",
        is_spam=is_spam,
        spam_reason=spam_reason,
        company_type=company_type,
        request_summary=str(extracted["request_summary"]).strip(),
        automation_goals=automation_goals,
        budget_amount=budget_amount,
        budget_currency=budget_currency,
        timeline=timeline,
        urgency=urgency,
        integrations=integrations,
        lead_class=lead_class,
        score=score,
        score_reasons=score_reasons,
        recommended_action=recommended_action,
        reply_draft=reply,
        review_status=review_status,
        review_notes=review_notes,
        follow_up_task=(
            "No sales follow-up. Keep spam intake for audit only."
            if is_spam
            else f"Follow up with {lead_class} lead about {', '.join(automation_goals)}."
        ),
        follow_up_due_at=follow_up_due.isoformat(timespec="seconds"),
    )


def analyze_request(raw_request: str) -> LeadAnalysis:
    return analyze_request_with_openai(raw_request)


def save_to_database(raw_request: str, analysis: LeadAnalysis) -> int:
    conn = connect_db()
    created_at = now_utc().isoformat(timespec="seconds")
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                created_at, raw_request, analysis_source, is_spam, spam_reason, company_type, request_summary, automation_goals,
                budget_amount, budget_currency, timeline, urgency, integrations, lead_class,
                score, score_reasons, recommended_action, reply_draft, review_status, review_notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                raw_request,
                analysis.analysis_source,
                1 if analysis.is_spam else 0,
                analysis.spam_reason,
                analysis.company_type,
                analysis.request_summary,
                json.dumps(analysis.automation_goals),
                analysis.budget_amount,
                analysis.budget_currency,
                analysis.timeline,
                analysis.urgency,
                json.dumps(analysis.integrations),
                analysis.lead_class,
                analysis.score,
                json.dumps(analysis.score_reasons),
                analysis.recommended_action,
                analysis.reply_draft,
                analysis.review_status,
                json.dumps(analysis.review_notes),
            ),
        )
        lead_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO follow_up_tasks (lead_id, task, due_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (lead_id, analysis.follow_up_task, analysis.follow_up_due_at, created_at),
        )
    conn.close()
    return lead_id


def add_to_google_sheets(lead_id: int, raw_request: str, analysis: LeadAnalysis) -> dict[str, Any]:
    webhook_url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL")
    row = {
        "lead_id": lead_id,
        "created_at": now_utc().isoformat(timespec="seconds"),
        "analysis_source": analysis.analysis_source,
        "is_spam": analysis.is_spam,
        "spam_reason": analysis.spam_reason,
        "company_type": analysis.company_type,
        "summary": analysis.request_summary,
        "budget": analysis.budget_amount,
        "currency": analysis.budget_currency,
        "timeline": analysis.timeline,
        "class": analysis.lead_class,
        "score": analysis.score,
        "review_status": analysis.review_status,
        "recommended_action": analysis.recommended_action,
        "raw_request": raw_request,
    }
    if webhook_url:
        payload = json.dumps(row).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"mode": "webhook", "status": response.status}

    ensure_data_dir()
    file_exists = SHEETS_FALLBACK_CSV.exists()
    with SHEETS_FALLBACK_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    return {"mode": "local_csv", "path": str(SHEETS_FALLBACK_CSV)}


def send_telegram_notification(lead_id: int, analysis: LeadAnalysis) -> dict[str, Any]:
    if analysis.is_spam:
        return {"mode": "skipped", "reason": "spam"}

    message = (
        f"New {analysis.lead_class.upper()} lead #{lead_id}\n"
        f"Score: {analysis.score}/100\n"
        f"Source: {analysis.analysis_source}\n"
        f"Review: {analysis.review_status}\n"
        f"Summary: {analysis.request_summary}\n"
        f"Next: {analysis.recommended_action}"
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
        with urllib.request.urlopen(url, data=payload, timeout=10) as response:
            return {"mode": "telegram", "status": response.status}

    ensure_data_dir()
    with TELEGRAM_OUTBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"created_at": now_utc().isoformat(), "message": message}) + "\n")
    return {"mode": "local_outbox", "path": str(TELEGRAM_OUTBOX)}


def process_request(raw_request: str) -> dict[str, Any]:
    analysis = analyze_request(raw_request)
    lead_id = save_to_database(raw_request, analysis)
    sheets_result = add_to_google_sheets(lead_id, raw_request, analysis)
    telegram_result = send_telegram_notification(lead_id, analysis)
    return {
        "lead_id": lead_id,
        "analysis": asdict(analysis),
        "database": {"path": str(DB_PATH)},
        "google_sheets": sheets_result,
        "telegram": telegram_result,
    }


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Agency Lead Agent</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f7f8;
      color: #182024;
    }
    body { margin: 0; }
    main { width: 78%; max-width: 1040px; min-width: 760px; margin: 0 auto; padding: 32px 20px; }
    header { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
    h1 { font-size: clamp(28px, 4vw, 44px); margin: 0 0 8px; letter-spacing: 0; }
    p { margin: 0; color: #4c5b61; line-height: 1.5; }
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(340px, 0.9fr); gap: 20px; align-items: start; }
    textarea {
      width: 100%; min-height: 260px; box-sizing: border-box; resize: vertical;
      border: 1px solid #c9d2d6; border-radius: 8px; padding: 16px; font: inherit; line-height: 1.45;
      background: white; color: #182024;
    }
    button {
      appearance: none; border: 0; border-radius: 8px; padding: 12px 16px; margin-top: 12px;
      font: inherit; font-weight: 700; background: #126b5d; color: white; cursor: pointer;
    }
    button:disabled { background: #8aa09b; cursor: progress; }
    section, .panel {
      background: white; border: 1px solid #dbe2e5; border-radius: 8px; padding: 18px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
    .metric { border: 1px solid #e2e8eb; border-radius: 8px; padding: 12px; min-height: 72px; }
    .label { color: #66767c; font-size: 12px; text-transform: uppercase; font-weight: 800; letter-spacing: .04em; }
    .value { display: block; margin-top: 6px; font-size: 22px; font-weight: 800; overflow-wrap: anywhere; }
    h2 { margin: 0 0 10px; font-size: 18px; }
    pre {
      white-space: pre-wrap; overflow-wrap: anywhere; background: #f2f5f6; border-radius: 8px;
      padding: 12px; color: #263238; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .reply { white-space: pre-wrap; line-height: 1.5; color: #253238; }
    .empty { min-height: 260px; display: grid; place-items: center; color: #66767c; text-align: center; }
    @media (max-width: 820px) {
      main { width: auto; min-width: 0; }
      header, .layout { display: block; }
      .panel { margin-top: 16px; }
      .metrics { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>AI Agency Lead Agent</h1>
        <p>Analyze a client request, qualify it, persist it, notify the team, and draft the next response.</p>
      </div>
    </header>
    <div class="layout">
      <section>
        <h2>Client Request</h2>
        <textarea id="request">We are a logistics company. We receive many client emails and want to automate support and order-status questions. Budget around €3000. Need something this month.</textarea>
        <button id="run">Analyze Lead</button>
      </section>
      <aside class="panel" id="result"><div class="empty">Submit a request to see the agent output.</div></aside>
    </div>
  </main>
  <script>
    const button = document.querySelector("#run");
    const textarea = document.querySelector("#request");
    const result = document.querySelector("#result");

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[ch]));
    }

    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Analyzing...";
      try {
        const response = await fetch("/api/leads", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({request: textarea.value})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Request failed");
        const a = payload.analysis;
        result.innerHTML = `
          <div class="metrics">
            <div class="metric"><span class="label">Lead</span><span class="value">${esc(a.lead_class)}</span></div>
            <div class="metric"><span class="label">Score</span><span class="value">${esc(a.score)}/100</span></div>
            <div class="metric"><span class="label">Budget</span><span class="value">${esc(a.budget_currency || "")} ${esc(a.budget_amount || "TBD")}</span></div>
            <div class="metric"><span class="label">Review</span><span class="value">${esc(a.review_status)}</span></div>
          </div>
          <h2>Analysis Source</h2>
          <p>${esc(a.analysis_source)}</p>
          ${a.is_spam ? `<h2 style="margin-top:16px">Spam Reason</h2><p>${esc(a.spam_reason || "Marked as spam.")}</p>` : ""}
          <h2>Recommended Action</h2>
          <p>${esc(a.recommended_action)}</p>
          <h2 style="margin-top:16px">Reply Draft</h2>
          <div class="reply">${esc(a.reply_draft)}</div>
          <h2 style="margin-top:16px">Structured Data</h2>
          <pre>${esc(JSON.stringify(payload, null, 2))}</pre>
        `;
      } catch (error) {
        result.innerHTML = `<p>${esc(error.message)}</p>`;
      } finally {
        button.disabled = false;
        button.textContent = "Analyze Lead";
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/favicon.ico":
            self.respond(204, "", "image/x-icon")
            return
        if self.path != "/":
            self.send_error(404)
            return
        self.respond(200, HTML, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/leads":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            raw_request = str(payload.get("request", "")).strip()
            if not raw_request:
                raise ValueError("Request text is required.")
            result = process_request(raw_request)
            self.respond(200, json.dumps(result, indent=2), "application/json")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self.respond(400, json.dumps({"error": str(exc)}), "application/json")
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def respond(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)


def serve(host: str, port: int) -> None:
    ensure_data_dir()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AI Agency Lead Agent running at http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI automation agency lead agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze and process one request")
    analyze_parser.add_argument("request", nargs="*", help="Client request text")
    analyze_parser.add_argument("--file", help="Read request text from a file")

    serve_parser = subparsers.add_parser("serve", help="Run the local web app")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", default=8000, type=int)

    args = parser.parse_args()
    if args.command == "serve":
        serve(args.host, args.port)
        return

    if args.file:
        raw_request = Path(args.file).read_text(encoding="utf-8")
    else:
        raw_request = " ".join(args.request).strip()
    if not raw_request:
        raw_request = sys.stdin.read().strip()
    if not raw_request:
        raise SystemExit("Provide request text as arguments, stdin, or --file.")
    print(json.dumps(process_request(raw_request), indent=2))


if __name__ == "__main__":
    main()
