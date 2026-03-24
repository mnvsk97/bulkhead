from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from tools import TOOLS


def _model() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY", "test-key"),
        base_url=os.environ["OPENAI_BASE_URL"],
        temperature=0,
        streaming=False,
        disable_streaming=True,
    )


agent = create_react_agent(
    model=_model(),
    tools=TOOLS,
    prompt=(
        "You are a concise revenue operations assistant. "
        "Use the available tools to assemble report evidence before answering. "
        "When you have enough evidence, produce a compact report with sections, KPI highlights, notes, and actions."
    ),
    name="agent",
)
