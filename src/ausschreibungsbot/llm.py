from langchain_openai import ChatOpenAI

from .config import Settings


def chat_model(settings: Settings, model: str, temperature: float = 0.2) -> ChatOpenAI:
    """ChatModel über OpenRouter (OpenAI-kompatible API)."""
    return ChatOpenAI(
        model=model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=temperature,
        timeout=180,
        max_retries=3,
        default_headers={
            "HTTP-Referer": "https://decocity.de",
            "X-Title": "Decocity Ausschreibungsbot",
        },
    )
