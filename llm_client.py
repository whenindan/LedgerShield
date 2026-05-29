"""Centralized OpenAI API wrapper for ACASO."""

import logging
from pydantic import BaseModel
import openai
from openai import OpenAI
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

load_dotenv()

logger = logging.getLogger(__name__)

USAGE_TRACKER: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
}

_client = OpenAI()


@retry(
    wait=wait_exponential(min=1, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
    reraise=True,
)
def extract_structured(
    prompt: str,
    response_model: type[BaseModel],
    model: str = "gpt-4o-mini",
    feature_name: str = "unknown",
) -> BaseModel:
    """Call the OpenAI API and parse the response into a Pydantic model.

    Uses ``client.beta.chat.completions.parse`` with ``response_format`` set
    to ``response_model`` so the model returns JSON that is automatically
    validated against the schema.

    Args:
        prompt: The user-facing prompt text to send as the sole user message.
        response_model: A Pydantic ``BaseModel`` subclass that defines the
            expected response schema.
        model: OpenAI model identifier. Defaults to ``"gpt-4o-mini"``.
        feature_name: Human-readable label for the calling feature, used in
            log output for traceability.

    Returns:
        A validated instance of ``response_model`` populated with the model's
        parsed response.

    Raises:
        openai.RateLimitError: When the API returns a 429 rate-limit response
            and all retry attempts are exhausted.
        openai.APIConnectionError: When a network-level connection failure
            persists after all retry attempts.
        openai.APIStatusError: When the API returns any other non-success HTTP
            status code.
    """
    response = _client.beta.chat.completions.parse(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=response_model,
    )

    usage = response.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0

    USAGE_TRACKER["prompt_tokens"] += prompt_tokens
    USAGE_TRACKER["completion_tokens"] += completion_tokens

    logger.info(
        "feature=%s model=%s prompt_tokens=%d completion_tokens=%d",
        feature_name,
        model,
        prompt_tokens,
        completion_tokens,
    )

    return response.choices[0].message.parsed


@retry(
    wait=wait_exponential(min=1, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
    reraise=True,
)
def generate_text(
    prompt: str,
    model: str = "gpt-4o-mini",
    feature_name: str = "unknown",
) -> str:
    """Call the OpenAI API and return the plain-text response.

    Sends a single user message and returns the raw string content of the
    first choice. Suitable for freeform generation tasks such as drafting
    emails or summaries where a structured schema is not needed.

    Args:
        prompt: The user-facing prompt text to send as the sole user message.
        model: OpenAI model identifier. Defaults to ``"gpt-4o-mini"``.
        feature_name: Human-readable label for the calling feature, used in
            log output for traceability.

    Returns:
        The string content of the model's response message.

    Raises:
        openai.RateLimitError: When the API returns a 429 rate-limit response
            and all retry attempts are exhausted.
        openai.APIConnectionError: When a network-level connection failure
            persists after all retry attempts.
        openai.APIStatusError: When the API returns any other non-success HTTP
            status code.
    """
    response = _client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )

    usage = response.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0

    USAGE_TRACKER["prompt_tokens"] += prompt_tokens
    USAGE_TRACKER["completion_tokens"] += completion_tokens

    logger.info(
        "feature=%s model=%s prompt_tokens=%d completion_tokens=%d",
        feature_name,
        model,
        prompt_tokens,
        completion_tokens,
    )

    return response.choices[0].message.content


def get_usage_summary() -> dict:
    """Return cumulative token usage and an estimated cost in USD.

    Cost is estimated using gpt-4o-mini public rates:
    - $0.00015 per 1 000 prompt tokens
    - $0.00060 per 1 000 completion tokens

    Note that gpt-4o calls cost approximately 10× more; this estimate will
    under-count sessions that mix models.

    Returns:
        A dict with keys ``prompt_tokens``, ``completion_tokens``, and
        ``estimated_cost_usd`` (float, rounded to 6 decimal places).
    """
    prompt_tokens = USAGE_TRACKER["prompt_tokens"]
    completion_tokens = USAGE_TRACKER["completion_tokens"]
    estimated_cost = (prompt_tokens / 1000 * 0.00015) + (
        completion_tokens / 1000 * 0.0006
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    }
