from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from local_whisper.utils import guess_suffix, seconds_to_hhmmss
from shared.security import env_bool, parse_hosts, validate_source_url


load_dotenv()
logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("ASR_MODEL", "base")
DEVICE = os.getenv("ASR_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8")
MAX_AUDIO_BYTES = int(os.getenv("ASR_MAX_AUDIO_MB", "200")) * 1024 * 1024
MAX_CONCURRENCY = max(1, int(os.getenv("ASR_MAX_CONCURRENCY", "1")))
ALLOWED_HOSTS = parse_hosts(os.getenv("AUDIO_ALLOWED_HOSTS", ""))
ALLOW_PRIVATE_URLS = env_bool(os.getenv("ALLOW_PRIVATE_AUDIO_URLS"), False)

app = FastAPI(title="Local ASR Service for Coze Studio", version="1.0.0")
_model: Any = None
_model_lock = threading.Lock()
_transcribe_slots = asyncio.Semaphore(MAX_CONCURRENCY)


class TranscribeRequest(BaseModel):
    audio_url: str = Field(min_length=1)
    file_id: str = ""
    file_name: str = "audio.mp3"
    language: str | None = "zh"
    task: str = Field(default="transcribe", pattern="^(transcribe|translate)$")
    enable_timestamps: bool = True
    enable_speaker_diarization: bool = False
    return_segments: bool = True


def get_model() -> Any:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                logger.info(
                    "Loading faster-whisper model %s (device=%s, compute_type=%s)",
                    MODEL_NAME,
                    DEVICE,
                    COMPUTE_TYPE,
                )
                _model = WhisperModel(
                    MODEL_NAME,
                    device=DEVICE,
                    compute_type=COMPUTE_TYPE,
                )
    return _model


async def download_audio(audio_url: str, file_name: str, directory: Path) -> Path:
    path = directory / f"input{guess_suffix(file_name, audio_url)}"
    timeout = httpx.Timeout(300.0, connect=30.0)
    downloaded = 0
    current_url = audio_url

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(6):
                try:
                    validate_source_url(
                        current_url,
                        allowed_hosts=ALLOWED_HOSTS,
                        allow_private=ALLOW_PRIVATE_URLS,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(status_code=502, detail="音频地址重定向无效")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_length = int(response.headers.get("content-length", "0") or 0)
                    if content_length > MAX_AUDIO_BYTES:
                        raise HTTPException(status_code=413, detail="音频超过大小限制")
                    with path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_AUDIO_BYTES:
                                raise HTTPException(status_code=413, detail="音频超过大小限制")
                            handle.write(chunk)
                    break
            else:
                raise HTTPException(status_code=502, detail="音频地址重定向次数过多")
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Audio download failed: %s", exc)
        raise HTTPException(status_code=502, detail="下载音频失败") from exc

    if downloaded == 0:
        raise HTTPException(status_code=400, detail="音频文件为空")
    return path


def transcribe_file(path: Path, request: TranscribeRequest) -> dict[str, Any]:
    segments_generator, info = get_model().transcribe(
        str(path),
        language=request.language or None,
        task=request.task,
        vad_filter=True,
        beam_size=5,
    )

    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for index, segment in enumerate(segments_generator, start=1):
        text = (segment.text or "").strip()
        if not text:
            continue
        item: dict[str, Any] = {
            "id": f"S{index:04d}",
            "speaker": "Speaker Unknown",
            "text": text,
        }
        if request.enable_timestamps:
            item.update(
                start=seconds_to_hhmmss(segment.start),
                end=seconds_to_hhmmss(segment.end),
            )
        segments.append(item)
        text_parts.append(text)

    return {
        "language": getattr(info, "language", request.language or ""),
        "duration_sec": int(getattr(info, "duration", 0) or 0),
        "source_file_name": request.file_name,
        "asr_engine": f"faster-whisper-{MODEL_NAME}",
        "segment_count": len(segments),
        "full_text": "\n".join(text_parts),
        "segments": segments if request.return_segments else [],
        "error_message": "",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "model_loaded": _model is not None,
    }


@app.post("/asr/transcribe")
async def transcribe(request: TranscribeRequest) -> dict[str, Any]:
    if request.enable_speaker_diarization:
        raise HTTPException(status_code=400, detail="本地模式暂不支持说话人分离")

    with tempfile.TemporaryDirectory(prefix="coze_asr_") as directory:
        path = await download_audio(request.audio_url, request.file_name, Path(directory))
        try:
            async with _transcribe_slots:
                return await asyncio.to_thread(transcribe_file, path, request)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("ASR transcription failed")
            raise HTTPException(status_code=500, detail="ASR 转写失败") from exc
