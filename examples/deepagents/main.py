import os

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


def main() -> None:
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
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "I'm going to Seattle tomorrow. Check weather, packing advice, then give me a short plan.",
                }
            ]
        },
        config={"configurable": {"thread_id": "demo-thread"}},
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
