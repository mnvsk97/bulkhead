import os
from pathlib import Path

import yaml

from deepagents import create_deep_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


@tool
def get_weather(city: str) -> str:
    """Return fake weather for a city."""
    return f"It is 68F and sunny in {city}."


@tool
def get_packing_tip(city: str) -> str:
    """Return a simple packing tip for a city."""
    return f"For {city}, pack layers and walking shoes."


def load_request_count() -> int:
    raw = os.getenv("BULKHEAD_REQUEST_COUNT")
    if raw is not None:
        return max(1, int(raw))

    for directory in [Path.cwd(), *Path.cwd().parents]:
        config_path = directory / "config.yaml"
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if isinstance(data, dict) and "request_count" in data:
            return max(1, int(data["request_count"]))

    return 1


def main() -> None:
    request_count = load_request_count()
    model = ChatOpenAI(
        model="gpt-4o-mini",
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:5000/v1"),
    )
    agent = create_deep_agent(
        model=model,
        tools=[get_weather, get_packing_tip],
        system_prompt="You are concise and practical. Use tools when helpful.",
    )
    for index in range(1, request_count + 1):
        result = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "I'm going to Seattle tomorrow. Check weather, packing advice, then give me a short plan.",
                    }
                ]
            },
            config={"configurable": {"thread_id": f"demo-thread-{index}"}},
        )
        print(f"[{index}/{request_count}] {result['messages'][-1].content}")


if __name__ == "__main__":
    main()
