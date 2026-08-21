from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from shared.security import env_bool, parse_hosts, validate_source_url
from volcengine_proxy.utils import build_hotword_context, extract_transcript, safe_ext


load_dotenv()
logger = logging.getLogger(__name__)

VOLC_API_KEY = os.getenv("VOLC_ASR_API_KEY", "").strip()
VOLC_APP_ID = os.getenv("VOLC_APP_ID", "").strip()
VOLC_ACCESS_TOKEN = os.getenv("VOLC_ACCESS_TOKEN", "").strip()
VOLC_RESOURCE_ID = os.getenv("VOLC_ASR_RESOURCE_ID", "volc.seedasr.auc").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
PROXY_TOKEN = os.getenv("PROXY_TOKEN", "")
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "./audio_cache")).resolve()
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_MB", "500")) * 1024 * 1024
WORKER_COUNT = max(1, int(os.getenv("WORKER_COUNT", "2")))
JOB_TTL_SECONDS = max(60, int(os.getenv("JOB_TTL_SECONDS", "86400")))
DELETE_AUDIO_AFTER_JOB = env_bool(os.getenv("DELETE_AUDIO_AFTER_JOB"), True)
ALLOWED_HOSTS = parse_hosts(os.getenv("AUDIO_ALLOWED_HOSTS", ""))
ALLOW_PRIVATE_URLS = env_bool(os.getenv("ALLOW_PRIVATE_AUDIO_URLS"), False)
URL_REWRITE_FROM = os.getenv("COZE_URL_REWRITE_FROM", "")
URL_REWRITE_TO = os.getenv("COZE_URL_REWRITE_TO", "")

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
SUCCESS_CODE = "20000000"
PROCESSING_CODES = {"20000001", "20000002"}

app = FastAPI(title="Coze Volcengine ASR Proxy", version="1.0.0")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=WORKER_COUNT, thread_name_prefix="volc-asr")


class StartJobRequest(BaseModel):
    audio_url: str = Field(min_length=1)
    audio_name: str = "meeting.mp3"
    language: str = "zh-CN"
    hotwords: list[str] | str | None = None
    poll_interval_sec: int = Field(default=10, ge=2, le=60)
    max_wait_sec: int = Field(default=3600, ge=60, le=86400)


def check_token(value: str | None) -> None:
    if not PROXY_TOKEN:
        raise HTTPException(status_code=503, detail="PROXY_TOKEN 未配置")
    if not value or not secrets.compare_digest(value, PROXY_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid X-Proxy-Token")


def update_job(job_id: str, **values: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def get_job_copy(job_id: str, include_raw: bool = False) -> dict[str, Any] | None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        result = dict(job)
    if not include_raw:
        result.pop("raw_result", None)
    return result


def prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        expired = [
            job_id
            for job_id, job in jobs.items()
            if job.get("status") in {"succeeded", "failed"}
            and float(job.get("finished_at", 0)) < cutoff
        ]
        for job_id in expired:
            jobs.pop(job_id, None)


def apply_url_rewrite(url: str) -> str:
    if URL_REWRITE_FROM and URL_REWRITE_TO and url.startswith(URL_REWRITE_FROM):
        return URL_REWRITE_TO + url[len(URL_REWRITE_FROM) :]
    return url


def download_audio(source_url: str, audio_name: str) -> tuple[Path, str]:
    source_url = apply_url_rewrite(source_url)
    extension = safe_ext(audio_name)
    filename = f"{uuid.uuid4().hex}{extension}"
    path = AUDIO_DIR / filename
    downloaded = 0
    current_url = source_url

    try:
        for _ in range(6):
            try:
                validate_source_url(
                    current_url,
                    allowed_hosts=ALLOWED_HOSTS,
                    allow_private=ALLOW_PRIVATE_URLS,
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc

            with requests.get(
                current_url,
                stream=True,
                timeout=(15, 300),
                allow_redirects=False,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("音频地址重定向无效")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_length = int(response.headers.get("content-length", "0") or 0)
                if content_length > MAX_AUDIO_BYTES:
                    raise RuntimeError("音频超过大小限制")
                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_AUDIO_BYTES:
                            raise RuntimeError("音频超过大小限制")
                        handle.write(chunk)
                break
        else:
            raise RuntimeError("音频地址重定向次数过多")
    except Exception:
        path.unlink(missing_ok=True)
        raise

    if downloaded == 0:
        path.unlink(missing_ok=True)
        raise RuntimeError("音频文件为空")
    return path, filename


def auth_headers(task_id: str, *, sequence: bool = False, log_id: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": VOLC_RESOURCE_ID,
        "X-Api-Request-Id": task_id,
    }
    if VOLC_API_KEY:
        headers["X-Api-Key"] = VOLC_API_KEY
    elif VOLC_APP_ID and VOLC_ACCESS_TOKEN:
        headers["X-Api-App-Key"] = VOLC_APP_ID
        headers["X-Api-Access-Key"] = VOLC_ACCESS_TOKEN
    else:
        raise RuntimeError("请配置 VOLC_ASR_API_KEY，或同时配置 VOLC_APP_ID 与 VOLC_ACCESS_TOKEN")
    if sequence:
        headers["X-Api-Sequence"] = "-1"
    if log_id:
        headers["X-Tt-Logid"] = log_id
    return headers


def parse_response(response: requests.Response) -> tuple[str, str, dict[str, Any]]:
    code = response.headers.get("X-Api-Status-Code", "")
    message = response.headers.get("X-Api-Message", "")
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if not code and isinstance(payload, dict):
        code = str(payload.get("code", ""))
    if not message and isinstance(payload, dict):
        message = str(payload.get("message", ""))
    if not response.ok and not code:
        code = f"HTTP_{response.status_code}"
    return code, message, payload if isinstance(payload, dict) else {}


def submit_asr(public_audio_url: str, task_id: str, request: StartJobRequest) -> tuple[str, str, str]:
    request_config: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "enable_speaker_info": True,
        "show_utterances": True,
    }
    context = build_hotword_context(request.hotwords)
    if context:
        request_config["corpus"] = {"context": context}

    body = {
        "user": {"uid": VOLC_APP_ID or "coze-asr-proxy"},
        "audio": {
            "url": public_audio_url,
            "format": safe_ext(request.audio_name).lstrip("."),
            "language": request.language,
        },
        "request": request_config,
    }
    response = requests.post(
        SUBMIT_URL,
        headers=auth_headers(task_id, sequence=True),
        json=body,
        timeout=(10, 60),
    )
    code, message, _ = parse_response(response)
    return code, message, response.headers.get("X-Tt-Logid", "")


def query_asr(task_id: str, log_id: str) -> tuple[str, str, dict[str, Any]]:
    response = requests.post(
        QUERY_URL,
        headers=auth_headers(task_id, log_id=log_id),
        json={},
        timeout=(10, 60),
    )
    return parse_response(response)


def run_job(job_id: str, request: StartJobRequest) -> None:
    audio_path: Path | None = None
    try:
        if not PUBLIC_BASE_URL:
            raise RuntimeError("PUBLIC_BASE_URL 未配置")
        update_job(job_id, status="downloading")
        audio_path, filename = download_audio(request.audio_url, request.audio_name)
        public_audio_url = f"{PUBLIC_BASE_URL}/audio/{filename}"

        task_id = str(uuid.uuid4())
        update_job(job_id, status="submitted", volc_task_id=task_id)
        code, message, log_id = submit_asr(public_audio_url, task_id, request)
        update_job(job_id, submit_code=code, submit_message=message, volc_log_id=log_id)
        if code != SUCCESS_CODE:
            raise RuntimeError(f"ASR submit failed: {code} {message}")

        update_job(job_id, status="processing")
        deadline = time.time() + request.max_wait_sec
        while time.time() < deadline:
            code, message, payload = query_asr(task_id, log_id)
            update_job(
                job_id,
                last_query_code=code,
                last_query_message=message,
                last_query_at=time.time(),
            )
            if code == SUCCESS_CODE:
                full_text, transcript_with_time = extract_transcript(payload)
                update_job(
                    job_id,
                    status="succeeded",
                    transcript_full=full_text,
                    transcript_with_time=transcript_with_time,
                    raw_result=payload,
                    finished_at=time.time(),
                )
                return
            if code in PROCESSING_CODES:
                time.sleep(request.poll_interval_sec)
                continue
            raise RuntimeError(f"ASR query failed: {code} {message}")
        raise TimeoutError("ASR query timeout")
    except Exception as exc:
        logger.exception("ASR job %s failed", job_id)
        update_job(job_id, status="failed", error=str(exc), finished_at=time.time())
    finally:
        if DELETE_AUDIO_AFTER_JOB and audio_path is not None:
            audio_path.unlink(missing_ok=True)


@app.get("/health")
def health() -> dict[str, Any]:
    auth_configured = bool(VOLC_API_KEY or (VOLC_APP_ID and VOLC_ACCESS_TOKEN))
    return {
        "ok": True,
        "auth_configured": auth_configured,
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
        "proxy_token_configured": bool(PROXY_TOKEN),
        "resource_id": VOLC_RESOURCE_ID,
    }


@app.post("/asr/jobs")
def create_job(
    request: StartJobRequest,
    x_proxy_token: str | None = Header(default=None),
) -> dict[str, str]:
    check_token(x_proxy_token)
    prune_jobs()
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"job_id": job_id, "status": "queued", "created_at": time.time()}
    executor.submit(run_job, job_id, request)
    return {"job_id": job_id, "status": "queued"}


@app.get("/asr/jobs/{job_id}")
def get_job(
    job_id: str,
    include_raw: bool = False,
    x_proxy_token: str | None = Header(default=None),
) -> dict[str, Any]:
    check_token(x_proxy_token)
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job = get_job_copy(job_id, include_raw=include_raw)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job


@app.get("/audio/{filename}")
def get_audio(filename: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9]{32}\.(mp3|wav|ogg|m4a)", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = AUDIO_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found")
    media_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
    }
    return FileResponse(path, media_type=media_types[path.suffix])
