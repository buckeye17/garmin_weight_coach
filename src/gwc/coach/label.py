"""Reading a Nutrition Facts label from a photo, for products a barcode lookup can't find.

Claude copies the label's per-serving numbers; the user reviews them, then the
food is saved as their own (and linked to the barcode, if one was scanned).
"""

import re
from typing import Any

from gwc.coach import prompts
from gwc.coach.jobs import Job
from gwc.coach.llm import ImageInput, LLMError, LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.nutrition.nutrients import NUTRIENT_KEYS

MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_DATA_URL = re.compile(r"^data:(image/[\w.+-]+);base64,(.+)$", re.DOTALL)

_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "name": _NULLABLE_STRING,
        "brand": _NULLABLE_STRING,
        "serving_label": _NULLABLE_STRING,
        "serving_grams": _NULLABLE_NUMBER,
        "per_serving": {
            "type": "object",
            "properties": {k: _NULLABLE_NUMBER for k in NUTRIENT_KEYS},
            "required": NUTRIENT_KEYS,
            "additionalProperties": False,
        },
        "message": _NULLABLE_STRING,
    },
    "required": [
        "readable",
        "name",
        "brand",
        "serving_label",
        "serving_grams",
        "per_serving",
        "message",
    ],
    "additionalProperties": False,
}


def image_from_data_url(data_url: str) -> ImageInput:
    match = _DATA_URL.match(data_url or "")
    if not match or match.group(1) not in MEDIA_TYPES:
        raise LLMError("That file isn't a photo Claude can read (use JPEG or PNG).")
    return ImageInput(media_type=match.group(1), data=match.group(2))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def clean_label(data: dict[str, Any]) -> dict[str, Any]:
    """Claude's answer with numbers checked and text trimmed."""
    per = data.get("per_serving") or {}
    grams = _number(data.get("serving_grams"))
    return {
        "readable": bool(data.get("readable")),
        "name": (data.get("name") or "").strip() or None,
        "brand": (data.get("brand") or "").strip() or None,
        "serving_label": (data.get("serving_label") or "").strip() or None,
        "serving_grams": grams or None,
        "per_serving": {k: _number(per.get(k)) for k in NUTRIENT_KEYS},
        "message": (data.get("message") or "").strip() or None,
    }


def read_label(data_url: str, job: Job | None = None) -> dict[str, Any]:
    """Run Claude over a label photo (a data: URL); returns `clean_label` fields plus "model"."""
    image = image_from_data_url(data_url)
    req = LLMRequest(
        system=prompts.LABEL_READER,
        messages=[{"role": "user", "content": "Read this Nutrition Facts label."}],
        config=feature_config("parse"),
        output_schema=LABEL_SCHEMA,
        on_activity=job.on_activity if job else None,
        images=[image],
    )
    if job:
        job.on_activity("Reading the label…")
    result = get_backend().run(req)
    return {**clean_label(result.data or {}), "model": result.model}
