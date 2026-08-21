import socket

import pytest

from local_whisper.utils import guess_suffix, seconds_to_hhmmss
from shared.security import env_bool, parse_hosts, validate_source_url
from volcengine_proxy.utils import (
    build_hotword_context,
    extract_transcript,
    ms_to_time,
    safe_ext,
)


def test_local_time_and_suffix_helpers() -> None:
    assert seconds_to_hhmmss(3661.9) == "01:01:01"
    assert guess_suffix("recording.M4A", "") == ".m4a"
    assert guess_suffix("", "https://example.com/a%20b.wav?download=1") == ".wav"
    assert guess_suffix("unknown.bin", "") == ".mp3"


def test_proxy_helpers() -> None:
    assert safe_ext("meeting.OGG") == ".ogg"
    assert ms_to_time(3_661_000) == "01:01:01"
    context = build_hotword_context("Alpha，Beta; Gamma")
    assert context is not None
    assert "Alpha" in context and "Gamma" in context


def test_extract_transcript_with_speaker() -> None:
    full, timed = extract_transcript(
        {
            "result": {
                "text": "你好。",
                "utterances": [
                    {
                        "start_time": 1000,
                        "end_time": 2500,
                        "speaker_id": "1",
                        "text": "你好。",
                    }
                ],
            }
        }
    )
    assert full == "你好。"
    assert timed == "[00:01-00:02][1] 你好。"


def test_security_parsing() -> None:
    assert parse_hosts(" A.example.com, b.example.com ") == {
        "a.example.com",
        "b.example.com",
    }
    assert env_bool("TRUE") is True
    assert env_bool(None) is False


def test_validate_source_url_rejects_private_literal() -> None:
    with pytest.raises(ValueError, match="非公网"):
        validate_source_url("http://127.0.0.1/audio.mp3")


def test_validate_source_url_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    assert (
        validate_source_url(
            "https://files.example.com/meeting.mp3",
            allowed_hosts={"files.example.com"},
        )
        == "https://files.example.com/meeting.mp3"
    )
    with pytest.raises(ValueError, match="AUDIO_ALLOWED_HOSTS"):
        validate_source_url(
            "https://other.example.com/meeting.mp3",
            allowed_hosts={"files.example.com"},
        )
