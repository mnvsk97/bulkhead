from __future__ import annotations

import json
import os
from datetime import date

from fastmcp import FastMCP
from fastmcp.prompts import PromptResult


HOST = os.getenv("REPORTING_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("REPORTING_MCP_PORT", "8001"))
PATH = os.getenv("REPORTING_MCP_PATH", "/mcp")

mcp = FastMCP(name="agentbreak-reporting-mcp", version="0.1.0")


def _log(tool_name: str, payload: dict[str, object]) -> None:
    print(f"[reporting-mcp] {tool_name} {json.dumps(payload, sort_keys=True)}", flush=True)


@mcp.tool(name="list_report_sections", description="Return the canonical sections for a report type.")
def list_report_sections(report_type: str) -> dict[str, object]:
    sections = {
        "quarterly_business_review": [
            "Executive Summary",
            "Revenue and Growth",
            "Engagement Trends",
            "Operational Risks",
            "Recommended Actions",
        ],
        "renewal_risk_review": [
            "Account Health",
            "Usage Signals",
            "Risk Factors",
            "Open Notes",
            "Recommended Actions",
        ],
    }
    result = {
        "report_type": report_type,
        "sections": sections.get(report_type, sections["quarterly_business_review"]),
    }
    _log("list_report_sections", {"report_type": report_type})
    return result


@mcp.tool(name="fetch_kpi_snapshot", description="Return deterministic KPI data for a set of metrics.")
def fetch_kpi_snapshot(metric_names: list[str], as_of: str) -> dict[str, object]:
    values = {
        "arr": {"value": 1250000, "unit": "usd", "trend": "+12% QoQ"},
        "active_users": {"value": 18420, "unit": "users", "trend": "+8% QoQ"},
        "nrr": {"value": 118, "unit": "percent", "trend": "+3 pts QoQ"},
        "ticket_backlog": {"value": 14, "unit": "tickets", "trend": "-6 QoQ"},
        "sev1_incidents": {"value": 1, "unit": "incidents", "trend": "-2 QoQ"},
    }
    result = {
        "as_of": as_of,
        "metrics": {name: values.get(name, {"value": "unknown", "unit": "n/a", "trend": "n/a"}) for name in metric_names},
    }
    _log("fetch_kpi_snapshot", {"metric_names": metric_names, "as_of": as_of})
    return result


@mcp.tool(name="lookup_account_notes", description="Return recent qualitative notes for an account.")
def lookup_account_notes(account_id: str) -> dict[str, object]:
    notes = {
        "acct-acme": [
            "Customer expanded procurement to the EU region in February.",
            "Executive sponsor requested stronger incident communication after a January outage.",
            "CSM reports strong adoption in analytics workflows.",
        ],
        "acct-zenith": [
            "Renewal committee asked for a packaging review before the end of quarter.",
            "Support escalations are down after the January runbook cleanup.",
        ],
    }
    result = {"account_id": account_id, "notes": notes.get(account_id, notes["acct-acme"])}
    _log("lookup_account_notes", {"account_id": account_id})
    return result


@mcp.tool(name="render_report_brief", description="Return a compact report brief for an account and report type.")
def render_report_brief(account_id: str, report_type: str) -> dict[str, object]:
    result = {
        "account_id": account_id,
        "report_type": report_type,
        "brief": (
            f"{account_id} is trending positively overall with durable expansion signals, "
            "one recent reliability concern, and clear follow-up actions for success and support teams."
        ),
        "generated_on": date.today().isoformat(),
    }
    _log("render_report_brief", {"account_id": account_id, "report_type": report_type})
    return result


@mcp.resource(
    "report://playbooks/qbr-template",
    name="qbr-template",
    description="Quarterly business review outline template.",
    mime_type="text/markdown",
)
def qbr_template() -> str:
    _log("qbr_template", {})
    return "# QBR Template\n\n## Executive Summary\n## KPIs\n## Risks\n## Actions\n"


@mcp.resource(
    "report://notes/renewal-checklist",
    name="renewal-checklist",
    description="Renewal risk review checklist.",
    mime_type="text/markdown",
)
def renewal_checklist() -> str:
    _log("renewal_checklist", {})
    return "# Renewal Checklist\n\n- Confirm sponsor alignment\n- Review usage drop-offs\n- Capture open support risks\n"


@mcp.prompt(name="executive-summary", description="Render a compact executive summary prompt for an account review.")
def executive_summary(account_id: str, report_type: str) -> PromptResult:
    _log("executive_summary_prompt", {"account_id": account_id, "report_type": report_type})
    return PromptResult(
        f"Write a crisp executive summary for {account_id} using the {report_type} evidence. Focus on business outcome, risk, and next action."
    )


def main() -> None:
    print(f"[reporting-mcp] starting on http://{HOST}:{PORT}{PATH}", flush=True)
    mcp.run(transport="streamable-http", host=HOST, port=PORT, path=PATH)


if __name__ == "__main__":
    main()
