from __future__ import annotations

import json
import re
from typing import Any


SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}


def safe_ext(name: str) -> str:
    lower = (name or "").lower()
    for extension in SUPPORTED_EXTENSIONS:
        if lower.endswith(extension):
            return extension
    return ".mp3"


def build_hotword_context(hotwords: list[str] | str | None) -> str | None:
    if not hotwords:
        return None
    if isinstance(hotwords, str):
        words = [item.strip() for item in re.split(r"[,，\n;；]", hotwords) if item.strip()]
    else:
        words = [str(item).strip() for item in hotwords if str(item).strip()]
    if not words:
        return None
    return json.dumps(
        {"hotwords": [{"word": word} for word in words[:200]]},
        ensure_ascii=False,
    )


def ms_to_time(milliseconds: Any) -> str:
    try:
        seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def extract_transcript(payload: dict[str, Any]) -> tuple[str, str]:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return "", ""

    full_text = str(result.get("text") or "")
    utterances = result.get("utterances") or []
    if not isinstance(utterances, list):
        return full_text, full_text

    lines: list[str] = []
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = str(utterance.get("text") or "").strip()
        if not text:
            continue
        additions = utterance.get("additions") or {}
        speaker = (
            utterance.get("speaker")
            or utterance.get("speaker_id")
            or (additions.get("speaker") if isinstance(additions, dict) else None)
            or (additions.get("speaker_id") if isinstance(additions, dict) else None)
            or "未知说话人"
        )
        lines.append(
            f"[{ms_to_time(utterance.get('start_time'))}-"
            f"{ms_to_time(utterance.get('end_time'))}][{speaker}] {text}"
        )
    return full_text, "\n".join(lines) if lines else full_text
