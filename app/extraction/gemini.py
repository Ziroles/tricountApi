"""
Appel du modèle de vision.

Portage de la partie « réseau » de `src/extraction/gemini.ts`. Deux traits de
l'original sont conservés parce qu'ils règlent de vrais problèmes :

 - **Le sondage des capacités.** Les modèles Gemini n'acceptent pas tous
   `responseJsonSchema` ni `thinkingConfig`, et refusent en 400 sans le dire
   clairement. On essaie les combinaisons dans l'ordre du moins cher au plus
   tolérant, puis on retient celle qui a marché pour ce modèle.
 - **`thinkingBudget: 0`.** Lire un ticket ne demande pas de réflexion en
   chaîne ; la désactiver coupe plusieurs secondes de latence.

Les messages d'erreur sont en français et destinés à l'utilisateur : ils
traversent l'API telle quelle jusqu'à l'écran de lecture.
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
    """Échec de lecture, déjà formulé pour l'utilisateur."""

    def __init__(self, reason: str, retryable: bool, code: str = "extraction_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class ModelCaps:
    structured: bool
    thinking_config: bool


# Du plus souhaitable au plus tolérant : sortie structurée d'abord, réflexion
# désactivée d'abord.
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
        return "Requête refusée. Vérifiez la clé API et le nom du modèle dans les réglages."
    if status in (401, 403):
        return "La clé API a été refusée. Vérifiez-la dans les réglages."
    if status == 404:
        return "Ce modèle est introuvable. Vérifiez son nom dans les réglages."
    if status == 429:
        return "Quota atteint. Réessayez dans un moment."
    if status >= 500:
        return "Le service est momentanément indisponible."
    return "La lecture n'a pas abouti."


def _status_of(error: Exception) -> int | None:
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return code
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _is_retryable_400(error: Exception) -> bool:
    """
    Un 400 causé par une capacité non supportée mérite un nouvel essai avec
    d'autres options ; un 400 causé par la clef, non — réessayer ne ferait que
    répéter le refus.
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
        "Le service de lecture n'a pas pu être joint.", retryable=True
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
    """Photo de ticket → extraction assainie. Lève `ExtractionError` en cas d'échec."""
    if api_key.strip() == "":
        raise ExtractionError(
            "Aucune clé Gemini n'est disponible. Renseignez la vôtre dans les réglages.",
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
        except Exception as error:  # noqa: BLE001 — on retente ou on convertit
            if _is_retryable_400(error):
                logger.info("capacités périmées pour %s, nouveau sondage", model)
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
            raise _wrap(last_error or RuntimeError("aucune tentative"))

    if text.strip() == "":
        raise ExtractionError("Le ticket n'a pas pu être lu sur cette photo.", retryable=True)

    try:
        parsed = json.loads(text)
    except ValueError:
        # Certains modèles encadrent le JSON d'une clôture Markdown malgré la
        # consigne : on tente de le dégager avant d'abandonner.
        stripped = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            parsed = json.loads(stripped)
        except ValueError as error:
            raise ExtractionError(
                "La réponse du service était inexploitable.", retryable=True
            ) from error

    return normalize_extraction(parsed)


# ── Liste des modèles ─────────────────────────────────────────────────────────

MULTIMODAL_PREFIXES = ("gemini-1.5", "gemini-2", "gemini-3", "gemini-flash", "gemini-pro")


def list_models(api_key: str) -> list[dict[str, str]]:
    """Modèles utilisables pour lire un ticket, avec la clef fournie."""
    if api_key.strip() == "":
        raise ExtractionError(
            "Aucune clé Gemini n'est disponible.", retryable=False, code="no_gemini_key"
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
