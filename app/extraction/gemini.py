"""
Calling the vision model.

A port of the "network" part of `src/extraction/gemini.ts`. Two traits of the
original are kept because they solve real problems:

 - **Capability probing.** Not all Gemini models accept `responseJsonSchema` or
   `thinkingConfig`, and they refuse with a 400 without saying so clearly. We try
   the combinations from cheapest to most tolerant, then remember the one that
   worked for that model.
 - **`thinkingBudget: 0`.** Reading a receipt does not require chain-of-thought;
   disabling it cuts several seconds of latency.

The error messages are written for the user: they travel through the API as-is
all the way to the scanning screen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from .. import config
from .normalize import normalize_extraction
from .prompt import PROMPT, RESPONSE_SCHEMA
from .types import ExtractionResult

logger = logging.getLogger("splitticket.gemini")


class ExtractionError(Exception):
    """Scan failure, already phrased for the user."""

    def __init__(self, reason: str, retryable: bool, code: str = "extraction_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class ModelCaps:
    structured: bool
    thinking_config: bool


# From most desirable to most tolerant: structured output first, thinking
# disabled first.
CAPS_PROBE_ORDER: tuple[ModelCaps, ...] = (
    ModelCaps(structured=True, thinking_config=False),
    ModelCaps(structured=False, thinking_config=False),
    ModelCaps(structured=True, thinking_config=True),
    ModelCaps(structured=False, thinking_config=True),
)

_caps_cache: dict[str, ModelCaps] = {}


def clear_caps_cache() -> None:
    _caps_cache.clear()


def message_for_status(status: int) -> str:
    if status == 400:
        return "Request refused. Check the API key and the model name in the settings."
    if status in (401, 403):
        return "The API key was refused. Check it in the settings."
    if status == 404:
        return "This model cannot be found. Check its name in the settings."
    if status == 429:
        return "Quota reached. Try again in a moment."
    if status >= 500:
        return "The service is temporarily unavailable."
    return "The scan did not succeed."


def _status_of(error: Exception) -> int | None:
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return code
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _is_retryable_400(error: Exception) -> bool:
    """
    A 400 caused by an unsupported capability deserves another try with other
    options; a 400 caused by the key does not — retrying would only repeat the
    refusal.
    """
    if _status_of(error) != 400:
        return False
    message = str(error).lower()
    return not any(marker in message for marker in ("api key", "api_key", "apikey"))


def _wrap(error: Exception) -> ExtractionError:
    if isinstance(error, genai_errors.APIError):
        status = _status_of(error) or 502
        detail = str(error).strip()
        base = message_for_status(status)
        return ExtractionError(
            f"{base} {detail}".strip() if detail else base, retryable=status >= 500
        )
    return ExtractionError(
        "The scanning service could not be reached.", retryable=True
    )


def _config(caps: ModelCaps) -> genai_types.GenerateContentConfig:
    options: dict[str, object] = {"temperature": 0, "max_output_tokens": 4096}
    if caps.structured:
        options["response_mime_type"] = "application/json"
        options["response_json_schema"] = RESPONSE_SCHEMA
    if caps.thinking_config:
        options["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
    return genai_types.GenerateContentConfig(**options)


def extract(image: bytes, mime_type: str, api_key: str, model: str) -> ExtractionResult:
    """Receipt photo → sanitised extraction. Raises `ExtractionError` on failure."""
    if api_key.strip() == "":
        raise ExtractionError(
            "No Gemini key is available. Enter your own in the settings.",
            retryable=False,
            code="no_gemini_key",
        )

    client = genai.Client(
        api_key=api_key.strip(),
        http_options=genai_types.HttpOptions(timeout=int(config.GEMINI_TIMEOUT_SECONDS * 1000)),
    )
    contents = [
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(text=PROMPT),
                genai_types.Part.from_bytes(data=image, mime_type=mime_type),
            ],
        )
    ]

    def attempt(caps: ModelCaps) -> str:
        response = client.models.generate_content(
            model=model, contents=contents, config=_config(caps)
        )
        return response.text or ""

    text: str | None = None
    cached = _caps_cache.get(model)
    if cached is not None:
        try:
            text = attempt(cached)
        except Exception as error:  # noqa: BLE001 — we either retry or convert
            if _is_retryable_400(error):
                logger.info("stale capabilities for %s, probing again", model)
                _caps_cache.pop(model, None)
            else:
                raise _wrap(error) from error

    if text is None:
        last_error: Exception | None = None
        for caps in CAPS_PROBE_ORDER:
            try:
                text = attempt(caps)
                _caps_cache[model] = caps
                break
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not _is_retryable_400(error):
                    break
        if text is None:
            raise _wrap(last_error or RuntimeError("no attempt made"))

    if text.strip() == "":
        raise ExtractionError("The receipt could not be read from this photo.", retryable=True)

    try:
        parsed = json.loads(text)
    except ValueError:
        # Some models wrap the JSON in a Markdown fence despite the instruction:
        # we try to strip it before giving up.
        stripped = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            parsed = json.loads(stripped)
        except ValueError as error:
            raise ExtractionError(
                "The service's response was unusable.", retryable=True
            ) from error

    return normalize_extraction(parsed)


# ── Listing models ────────────────────────────────────────────────────────────

MULTIMODAL_PREFIXES = ("gemini-1.5", "gemini-2", "gemini-3", "gemini-flash", "gemini-pro")


def list_models(api_key: str) -> list[dict[str, str]]:
    """Models usable to read a receipt, with the key provided."""
    if api_key.strip() == "":
        raise ExtractionError(
            "No Gemini key is available.", retryable=False, code="no_gemini_key"
        )
    try:
        client = genai.Client(api_key=api_key.strip())
        found: list[dict[str, str]] = []
        for entry in client.models.list():
            name = (entry.name or "").removeprefix("models/")
            if name == "" or not name.startswith(MULTIMODAL_PREFIXES):
                continue
            actions = entry.supported_actions
            if actions is not None and "generateContent" not in actions:
                continue
            found.append({"name": name, "displayName": entry.display_name or name})
        return sorted(found, key=lambda entry: entry["name"])
    except ExtractionError:
        raise
    except Exception as error:  # noqa: BLE001
        raise _wrap(error) from error
