# AppNeta -> Google Chat bridge with Gemini AI triage summaries.
#
# DISCLAIMER: This code is provided "as is", for illustration purposes only,
# without warranty of any kind. It is not an official product, receives no
# support or updates, and must be reviewed and tested before any production use.
# See README.md and LICENSE.

import html
import json
import os
import urllib.request
from datetime import datetime, timezone

import functions_framework
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration (see README.md for every variable)
# Required values have no default: the function fails to start if they are missing.
# ---------------------------------------------------------------------------
GCHAT_WEBHOOK_URL = os.environ["GCHAT_WEBHOOK_URL"]   # Secret Manager recommended
EXPECTED_SECRET = os.environ["WEBHOOK_SECRET"]        # Secret Manager recommended
GCP_PROJECT = os.environ["GCP_PROJECT"]
GCP_REGION = os.environ.get("GCP_REGION", "global")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Optional quick links shown on the card (leave unset to hide them)
GRAFANA_URL = os.environ.get("GRAFANA_URL", "")   # e.g. https://<your-grafana>/d/<dashboard-uid>
ITSM_URL = os.environ.get("ITSM_URL", "")         # e.g. https://<your-instance>.service-now.com/incident.do?sys_id=-1

ai_client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_REGION)


def generate_ai_triage(payload: dict) -> str:
    """Uses Gemini to turn sanitized AppNeta alarm fields into a short root cause and recommendations."""
    prompt = f"""
You are a NOC/SRE engineer analyzing a network alarm payload from AppNeta.
Provide a concise root cause and 2 quick, actionable recommendations.

CRITICAL RULES:
1. Do NOT use Markdown (no **, no ##, no raw - bullets).
2. Format using HTML ONLY:
<b>Root Cause:</b> [1 sentence explanation]<br>
<b>Recommendations:</b><br>
&bull; [Action 1: concise step]<br>
&bull; [Action 2: concise step]
3. Keep total output under 50 words.

Sanitized Alarm Payload:
{json.dumps(payload, indent=2)}
"""
    config = types.GenerateContentConfig(temperature=0.2, max_output_tokens=1024)
    if GEMINI_MODEL.startswith("gemini-2.5"):
        # No "thinking" needed for a short summary: lower cost and latency.
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)

    try:
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=config
        )
        text = (response.text or "").strip()

        # Convert any residual Markdown bold to HTML
        while "**" in text:
            text = text.replace("**", "<b>", 1).replace("**", "</b>", 1)

        # Google Chat cards use <br> for line breaks
        text = text.replace("\n", "<br>")
        while "<br><br>" in text:
            text = text.replace("<br><br>", "<br>")
        return text or "AI Triage unavailable."
    except Exception as e:
        print(f"Error calling Gemini: {e}")      # details go to Cloud Logging only
        return "AI Triage unavailable."


def link_widget(text: str, icon: str) -> dict:
    return {
        "decoratedText": {
            "text": text,
            "startIcon": {"materialIcon": {"name": icon, "fill": True, "weight": 300, "grade": -25}},
        }
    }


@functions_framework.http
def webhook_handler(request):
    # 1. Security: shared-secret header configured on the AppNeta connector
    if request.headers.get("X-AppNeta-Token") != EXPECTED_SECRET:
        print("Blocked: unauthorized request.")
        return ("Unauthorized", 401)

    try:
        payload = request.get_json(silent=True) or {}

        # 2. Extract AppNeta alarm fields
        alarm_id = str(payload.get("alarmId", "default_thread"))
        state = payload.get("state", "UNKNOWN")                 # RAISED / CLEARED
        severity = payload.get("alarmSeverity", "UNKNOWN")
        rule = payload.get("rule", "N/A")
        description = payload.get("description", "No description provided.")
        item_name = payload.get("itemName", "N/A")
        provider_link = payload.get("providerLink", "https://pm.appneta.com")
        org_id = payload.get("orgId", "N/A")
        policy_group = payload.get("monitoringPolicyGroupName", "AppNeta")
        target = payload.get("target", "N/A")
        tags = payload.get("tags", [])

        raw_time = payload.get("eventTime") or payload.get("timestamp") or payload.get("time")
        time_str = str(raw_time) if raw_time else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        hosting_tag = next(
            (t.get("value") for t in tags if isinstance(t, dict) and t.get("category") == "Hosting"),
            "Default",
        )
        tenant_str = f"Org {org_id} ({hosting_tag})"
        target_url = target if str(target).startswith("http") else f"https://{target}"

        # 3. Send ONLY non-sensitive technical fields to Gemini
        ai_payload = {
            "rule": rule,
            "severity": severity,
            "state": state,
            "impacted_service": item_name,
            "target": target,
            "description": description,
            "policy_group": policy_group,
            "tags": tags,
        }

        # Call Gemini only when an alarm is raised
        if state == "RAISED":
            ai_insights = generate_ai_triage(ai_payload)
        else:
            ai_insights = "Alarm cleared. No immediate action required."

        # 🚨 CRITICAL, ⚠️ other severities, ✅ cleared
        if state == "RAISED":
            title_prefix = "🚨" if severity == "CRITICAL" else "⚠️"
        else:
            title_prefix = "✅"
        card_title = f"{title_prefix} {policy_group} Alert: {rule}"

        # 4. Build the card (payload values are HTML-escaped)
        esc = lambda v: html.escape(str(v))
        summary_html = (
            f"<b>Impacted:</b> {esc(item_name)}<br>"
            f"<b>Message:</b> {esc(description)}<br>"
            f"<b>Status:</b> {esc(state)} &bull; <b>Severity:</b> {esc(severity)} "
            f"&bull; <b>Tenant:</b> {esc(tenant_str)}<br>"
            f"<b>Time:</b> {esc(time_str)}"
        )
        if ITSM_URL:
            summary_html += f" &bull; <b>Ticket:</b> <a href='{ITSM_URL}'>Create</a>"

        links = [
            link_widget(f"<a href='{provider_link}'>Triage Inspector (AppNeta Alarm)</a>", "query_stats"),
            link_widget(f"<b>Target:</b> <a href='{target_url}'>{esc(target)}</a>", "cell_tower"),
        ]
        if GRAFANA_URL:
            sep = "&" if "?" in GRAFANA_URL else "?"
            links.append(link_widget(
                f"<a href='{GRAFANA_URL}{sep}var-org={org_id}'>Grafana Network & Experience Overview</a>",
                "dashboard",
            ))

        gchat_payload = {
            "thread": {"threadKey": alarm_id},   # same alarm -> same Chat thread
            "cardsV2": [{
                "cardId": f"appneta-{alarm_id}",
                "card": {
                    "header": {"title": card_title},
                    "sections": [
                        {"header": "Summary", "widgets": [{"textParagraph": {"text": summary_html}}]},
                        {"header": "🤖 AI Triage Insights", "widgets": [{"textParagraph": {"text": ai_insights}}]},
                        {"header": "Actions & Quick Links", "collapsible": False, "widgets": links},
                    ],
                },
            }],
        }

        # 5. Post to Google Chat, replying in the alarm's thread when it exists
        url = GCHAT_WEBHOOK_URL
        if "messageReplyOption" not in url:
            url += ("&" if "?" in url else "?") + "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        req = urllib.request.Request(
            url,
            data=json.dumps(gchat_payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        with urllib.request.urlopen(req, timeout=10):
            return ("Message sent to Google Chat", 200)

    except Exception as e:
        print(f"Error in webhook_handler: {e}")   # details go to Cloud Logging only
        return ("Internal error", 500)
