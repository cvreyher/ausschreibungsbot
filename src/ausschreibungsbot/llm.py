from langchain_openrouter import ChatOpenRouter

from .config import Settings


def chat_model(
    settings: Settings, model: str, temperature: float | None = None, reasoning_effort: str | None = None
) -> ChatOpenRouter:
    """ChatModel über OpenRouter.

    ChatOpenRouter gibt den Denkprozess von Reasoning-Modellen (z.B. Kimi) bei Tool-Aufrufen
    korrekt an das Modell zurück – mit dem generischen ChatOpenAI gehen diese verloren.
    temperature=None: Modell-Standard (Reasoning-Modelle erlauben oft keine eigene Temperatur).
    """
    return ChatOpenRouter(
        model=model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=temperature,
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        timeout=180_000,  # Millisekunden
        max_retries=3,
        app_url="https://decocity.de",
        app_title="Decocity Ausschreibungsbot",
    )
