import unittest
from unittest.mock import patch

from agency_agent import analyze_request, process_request


def extraction(**overrides):
    payload = {
        "company_type": "logistics",
        "is_spam": False,
        "spam_reason": None,
        "request_summary": "Logistics company wants to automate support and order-status emails.",
        "automation_goals": [
            "email triage and support automation",
            "order-status automation",
        ],
        "budget_amount": 3000,
        "budget_currency": "EUR",
        "timeline": "this month",
        "urgency": "high",
        "integrations": ["email inbox", "order management system"],
        "reply_draft": "Hi, thanks for sharing this. I would start with order-status and common support emails.",
    }
    payload.update(overrides)
    return payload


def review(**overrides):
    payload = {
        "status": "approved",
        "notes": ["Reply is grounded and natural."],
        "corrected_reply": "Hi, thanks for sharing this. I would start with order-status and common support emails.",
    }
    payload.update(overrides)
    return payload


class AgencyAgentTests(unittest.TestCase):
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"})
    @patch("agency_agent.call_openai_json")
    def test_logistics_email_order_status_lead_is_hot(self, mock_openai):
        mock_openai.side_effect = [extraction(), review()]
        request = (
            "We are a logistics company. We receive many client emails and want to "
            "automate support and order-status questions. Budget around EUR 3000. "
            "Need something this month."
        )

        analysis = analyze_request(request)

        self.assertEqual(analysis.analysis_source, "openai:test-model")
        self.assertEqual(analysis.company_type, "logistics")
        self.assertEqual(analysis.budget_amount, 3000)
        self.assertEqual(analysis.budget_currency, "EUR")
        self.assertEqual(analysis.timeline, "this month")
        self.assertEqual(analysis.urgency, "high")
        self.assertIn("email triage and support automation", analysis.automation_goals)
        self.assertIn("order-status automation", analysis.automation_goals)
        self.assertEqual(analysis.lead_class, "hot")
        self.assertGreaterEqual(analysis.score, 75)
        self.assertIn("discovery call", analysis.recommended_action.lower())
        self.assertIn("order-status", analysis.reply_draft.lower())
        self.assertEqual(mock_openai.call_count, 2)
        drafter_prompt = mock_openai.call_args_list[0].args[0]
        reviewer_prompt = mock_openai.call_args_list[1].args[0]
        self.assertIn("must start with a simple greeting", drafter_prompt)
        self.assertIn("Use 'we' and 'our' consistently", drafter_prompt)
        self.assertIn("must start with 'Hi,' or 'Hello,'", reviewer_prompt)
        self.assertIn("do not mix 'I' and 'we'", reviewer_prompt)

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agency_agent.call_openai_json")
    def test_low_budget_vague_lead_is_cold(self, mock_openai):
        mock_openai.side_effect = [
            extraction(
                company_type="unknown",
                request_summary="Vague automation request with low budget.",
                automation_goals=["business-process automation"],
                budget_amount=500,
                budget_currency="EUR",
                timeline="flexible",
                urgency="low",
                integrations=[],
                reply_draft="Hi, thanks for reaching out. A narrower first step would help scope this.",
            ),
            review(corrected_reply="Hi, thanks for reaching out. A narrower first step would help scope this."),
        ]

        analysis = analyze_request("Need some automation. Budget EUR 500. No rush.")

        self.assertEqual(analysis.budget_amount, 500)
        self.assertEqual(analysis.urgency, "low")
        self.assertEqual(analysis.lead_class, "cold")

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agency_agent.call_openai_json")
    def test_clear_lead_with_missing_budget_is_warm(self, mock_openai):
        mock_openai.side_effect = [
            extraction(
                company_type="real estate",
                request_summary="Real estate agency wants property replies, buyer qualification, and viewing booking.",
                automation_goals=["lead qualification", "appointment scheduling"],
                budget_amount=None,
                budget_currency=None,
                timeline="not specified",
                urgency="medium",
                integrations=["email inbox", "calendar"],
                reply_draft="Hi, this sounds like a focused first workflow for buyer qualification and viewings.",
            ),
            review(corrected_reply="Hi, this sounds like a focused first workflow for buyer qualification and viewings."),
        ]

        analysis = analyze_request(
            "We are a small real estate agency and want an AI assistant that can "
            "answer property availability questions, qualify buyers, and book "
            "viewing appointments. We use Outlook and Google Calendar. Not sure "
            "about budget yet, but we want to understand what is possible."
        )

        self.assertEqual(analysis.company_type, "real estate")
        self.assertIsNone(analysis.budget_amount)
        self.assertEqual(analysis.lead_class, "warm")
        self.assertGreaterEqual(analysis.score, 50)

    @patch.dict(
        "os.environ",
        {"OPENAI_API_KEY": "test-key", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
    )
    @patch("agency_agent.call_openai_json")
    def test_spam_gets_ai_reply_and_skips_telegram(self, mock_openai):
        mock_openai.return_value = extraction(
            company_type="unknown",
            is_spam=True,
            spam_reason="Promotional link spam.",
            request_summary="Spam intake: promotional link spam.",
            automation_goals=["needs discovery"],
            budget_amount=None,
            budget_currency=None,
            timeline="not specified",
            urgency="medium",
            integrations=[],
            reply_draft=(
                "Hi, your message was marked as spam. If this is a genuine business request, "
                "please reply with your company, automation need, budget, and timeline."
            ),
        )

        result = process_request(
            "Limited time offer! Buy followers and guaranteed traffic now. "
            "Visit https://example.com https://promo.example.com https://deal.example.com"
        )

        analysis = result["analysis"]
        self.assertTrue(analysis["is_spam"])
        self.assertEqual(analysis["lead_class"], "spam")
        self.assertEqual(analysis["score"], 0)
        self.assertIn("marked as spam", analysis["reply_draft"])
        self.assertEqual(result["telegram"]["mode"], "skipped")
        self.assertEqual(mock_openai.call_count, 1)

    @patch.dict("os.environ", {"OPENAI_API_KEY": ""})
    def test_openai_key_is_required(self):
        with self.assertRaises(RuntimeError):
            analyze_request("We need support automation.")


if __name__ == "__main__":
    unittest.main()
