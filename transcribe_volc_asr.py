from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from volcengine_proxy.utils import extract_transcript, safe_ext


SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
SUCCESS_CODE = "20000000"
PROCESSING_CODES = {"20000001", "20000002"}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量：{name}")
    return value


def auth_headers(task_id: str, *, sequence: bool = False, log_id: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": os.getenv("VOLC_ASR_RESOURCE_ID", "volc.seedasr.auc"),
        "X-Api-Request-Id": task_id,
    }
    api_key = os.getenv("VOLC_ASR_API_KEY", "").strip()
    app_id = os.getenv("VOLC_APP_ID", "").strip()
    access_token = os.getenv("VOLC_ACCESS_TOKEN", "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
    elif app_id and access_token:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token
    else:
        raise ValueError("请配置 VOLC_ASR_API_KEY，或同时配置 VOLC_APP_ID 与 VOLC_ACCESS_TOKEN")
    if sequence:
        headers["X-Api-Sequence"] = "-1"
    if log_id:
        headers["X-Tt-Logid"] = log_id
    return headers


def response_fields(response: requests.Response) -> tuple[str, str]:
    code = response.headers.get("X-Api-Status-Code", "")
    message = response.headers.get("X-Api-Message", "")
    if not code and response.content:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                code = str(payload.get("code", ""))
                message = message or str(payload.get("message", ""))
        except ValueError:
            pass
    if not response.ok and not code:
        code = f"HTTP_{response.status_code}"
    return code, message


def save_outputs(payload: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    full_text, timed_text = extract_transcript(payload)
    (output_dir / "transcript.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "transcript.txt").write_text(full_text, encoding="utf-8")
    (output_dir / "transcript_with_timestamps.txt").write_text(timed_text, encoding="utf-8")


def main() -> int:
    load_dotenv()
    try:
        audio_url = required("AUDIO_URL")
        poll_interval = max(2, int(os.getenv("POLL_INTERVAL_SECONDS", "10")))
        max_wait = max(60, int(os.getenv("MAX_WAIT_SECONDS", "10800")))
        output_dir = Path(os.getenv("OUTPUT_DIR", "outputs"))
        task_id = str(uuid.uuid4())
        submit_headers = auth_headers(task_id, sequence=True)
    except (ValueError, TypeError) as exc:
        print(exc, file=sys.stderr)
        return 2

    filename = PurePosixPath(urlparse(audio_url).path).name
    body = {
        "user": {"uid": os.getenv("VOLC_APP_ID", "volc-asr-cli")},
        "audio": {"url": audio_url, "format": safe_ext(filename).lstrip(".")},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "enable_speaker_info": True,
            "show_utterances": True,
        },
    }

    try:
        response = requests.post(
            SUBMIT_URL,
            headers=submit_headers,
            json=body,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        print(f"提交请求异常：{exc}", file=sys.stderr)
        return 1

    code, message = response_fields(response)
    if code != SUCCESS_CODE:
        print(f"提交失败：{code} {message}", file=sys.stderr)
        return 1

    log_id = response.headers.get("X-Tt-Logid", "")
    print(f"任务已提交，request_id={task_id}")
    deadline = time.time() + max_wait

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            response = requests.post(
                QUERY_URL,
                headers=auth_headers(task_id, log_id=log_id),
                json={},
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            print(f"查询请求异常：{exc}", file=sys.stderr)
            return 1

        code, message = response_fields(response)
        if code == SUCCESS_CODE:
            try:
                payload = response.json()
            except ValueError:
                print("查询成功但响应不是有效 JSON", file=sys.stderr)
                return 1
            save_outputs(payload, output_dir)
            print(f"转写完成，结果已保存到：{output_dir.resolve()}")
            return 0
        if code in PROCESSING_CODES:
            print(f"任务处理中（status={code}）……")
            continue
        print(f"查询失败：{code} {message}", file=sys.stderr)
        return 1

    print("查询超时，请稍后使用相同任务 ID 排查服务端日志。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止轮询。", file=sys.stderr)
        raise SystemExit(130)
