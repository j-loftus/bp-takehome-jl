"""
Shared LLM API wrapper.

Used by classifier (Task 2.2), extractor (Task 2.3), and LLM-as-judge (Task 3.3).
All model identifiers live in MODEL_CONFIG — change one line to switch models everywhere.

Usage:
    from src.llm_client import call_llm, LLMCallError, get_token_totals, reset_token_counters
    response_text = call_llm(prompt, task="extraction")
"""

import base64
import json
import logging
import os
import re
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("llm_client")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [llm_client] [%(levelname)s] %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
# Change these values to switch models across runs.
# All callers pass a task name; the client resolves it to the model string below.

MODEL_CONFIG = {
    "classification": os.getenv("CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001"),
    "extraction":     "claude-sonnet-4-5",
    "judge":          "claude-sonnet-4-5",
    "router":         "claude-haiku-4-5-20251001",
    "sql":            "claude-sonnet-4-5",
    "synthesis":      "claude-sonnet-4-5",
}

# ---------------------------------------------------------------------------
# API client — initialized at import time
# ---------------------------------------------------------------------------

_api_key = os.environ.get("ANTHROPIC_API_KEY")
if not _api_key:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY environment variable not set. "
        "Export it before running the pipeline: export ANTHROPIC_API_KEY=sk-ant-..."
    )

_client = anthropic.Anthropic(api_key=_api_key)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class LLMCallError(Exception):
    """Raised when an LLM API call fails after retry exhaustion or on a non-retryable error."""
    pass

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [2, 5, 10]

# ---------------------------------------------------------------------------
# Token usage accumulators — reset between batch runs via reset_token_counters()
# ---------------------------------------------------------------------------

_token_totals: dict[str, dict[str, int]] = {
    "classification": {"input": 0, "output": 0},
    "extraction":     {"input": 0, "output": 0},
    "judge":          {"input": 0, "output": 0},
}


def _log_token_usage(response: anthropic.types.Message, task: str) -> None:
    usage = response.usage
    bucket = _token_totals.get(task, _token_totals["extraction"])
    bucket["input"]  += usage.input_tokens
    bucket["output"] += usage.output_tokens
    logger.debug(
        f"{task} — input: {usage.input_tokens}, output: {usage.output_tokens}"
    )


def get_token_totals() -> dict[str, dict[str, int]]:
    """Return a copy of accumulated token totals across all tasks."""
    return {task: dict(counts) for task, counts in _token_totals.items()}


def reset_token_counters() -> None:
    """Reset all token accumulators. Call at the start of each batch run."""
    for task in _token_totals:
        _token_totals[task] = {"input": 0, "output": 0}


# ---------------------------------------------------------------------------
# Primary function
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    task: str = "extraction",
    max_tokens: int = 1500,
) -> str:
    """
    Send a prompt to the Anthropic API and return the raw text response.

    Args:
        prompt:     The full prompt string to send as the user message.
        task:       Task name used to resolve the model from MODEL_CONFIG.
                    One of: 'classification', 'extraction', 'judge'.
        max_tokens: Maximum tokens in the response.

    Returns:
        Raw response text (str). Caller is responsible for JSON parsing.

    Raises:
        LLMCallError: On non-retryable failures or after retry exhaustion.
    """
    model = MODEL_CONFIG.get(task, MODEL_CONFIG["extraction"])
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            _log_token_usage(response, task)
            text = response.content[0].text
            if not text.strip():
                raise LLMCallError("LLM returned empty response")
            return text

        except anthropic.RateLimitError as e:
            last_error = e
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(f"Rate limit on attempt {attempt + 1}; retrying in {wait}s")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    f"Server error {e.status_code} on attempt {attempt + 1}; retrying in {wait}s"
                )
                time.sleep(wait)
            else:
                raise LLMCallError(f"Non-retryable API error: {e.status_code} — {e}") from e

        except anthropic.AuthenticationError as e:
            raise LLMCallError("Authentication failed — check ANTHROPIC_API_KEY") from e

    raise LLMCallError(f"LLM call failed after {MAX_RETRIES} attempts: {last_error}")


def call_llm_with_images(
    prompt: str,
    page_images: list[bytes],
    task: str = "extraction",
    max_tokens: int = 4096,
) -> str:
    """
    Send a prompt plus page images to the Anthropic API for vision-based extraction.
    Used for scanned documents where text extraction is unavailable.

    Args:
        prompt:       The full extraction prompt string (identical to the text path).
        page_images:  List of raw PNG bytes from pdf_parser.extract_page_images().
                      Images are prepended as content blocks before the prompt text.
        task:         Task name for model resolution. Use "extraction".
        max_tokens:   Default 4096 — higher than text path because vision calls may
                      produce more verbose extraction_notes across 20 pages of images.

    Returns:
        Raw response text (str). Caller is responsible for JSON parsing.

    Raises:
        LLMCallError: On non-retryable failures or after retry exhaustion.
    """
    model = MODEL_CONFIG.get(task, MODEL_CONFIG["extraction"])

    content: list[dict] = []
    for img_bytes in page_images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": prompt})

    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            _log_token_usage(response, task)
            text = response.content[0].text
            if not text.strip():
                raise LLMCallError("LLM returned empty response (vision)")
            return text

        except anthropic.RateLimitError as e:
            last_error = e
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(f"Rate limit (vision) on attempt {attempt + 1}; retrying in {wait}s")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    f"Server error {e.status_code} (vision) on attempt {attempt + 1}; retrying in {wait}s"
                )
                time.sleep(wait)
            else:
                raise LLMCallError(
                    f"Non-retryable API error (vision): {e.status_code} — {e}"
                ) from e

        except anthropic.AuthenticationError as e:
            raise LLMCallError("Authentication failed — check ANTHROPIC_API_KEY") from e

    raise LLMCallError(f"Vision LLM call failed after {MAX_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

def extract_json(text: str) -> dict:
    """
    Parse a JSON object from an LLM response, stripping markdown code fences if present.

    Handles the common case where the model wraps its output in ```json ... ```
    despite instructions not to. Falls back to locating the first '{' / last '}'
    in the raw text if no fence is found.

    Raises:
        json.JSONDecodeError: if no valid JSON object can be extracted.
    """
    # Try stripping a ```json ... ``` or ``` ... ``` fence first
    match = _FENCE_RE.search(text)
    if match:
        return json.loads(match.group(1))

    # Fall back to slicing from first '{' to last '}'
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    # Let the caller's except block handle the failure
    return json.loads(text)
