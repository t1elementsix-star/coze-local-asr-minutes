from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse


SUPPORTED_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


def seconds_to_hhmmss(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def guess_suffix(file_name: str, audio_url: str) -> str:
    candidate = file_name.strip() if file_name else ""
    if not candidate and audio_url:
        candidate = unquote(PurePosixPath(urlparse(audio_url).path).name)
    suffix = PurePosixPath(candidate.lower()).suffix
    return suffix if suffix in SUPPORTED_SUFFIXES else ".mp3"
