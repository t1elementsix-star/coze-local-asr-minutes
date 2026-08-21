# Coze Studio 本地 ASR 与会议纪要转写

一个面向本地 Coze Studio 工作流的音频转写工具集，提供三种用法：

- `local_whisper`：使用 faster-whisper 在本机完成转写，音频无需交给第三方 ASR。
- `volcengine_proxy`：把 Coze 的音频 URL 暂存为公网可取文件，异步调用火山引擎豆包录音文件识别，并返回带时间与说话人的文本。
- `transcribe_volc_asr.py`：不经过 Coze，直接转写一个公网音频 URL，便于联调凭据和接口。

仓库不包含 Coze Studio 上游源码、模型文件、会议音频、缓存、转写结果或任何真实密钥。它作为独立服务接入已有的 [Coze Studio](https://github.com/coze-dev/coze-studio) 部署。

环境要求：Python 3.10+。本地 Whisper 模式还需要 faster-whisper 支持的 CPU 或 CUDA 运行环境。

## 目录

```text
local_whisper/          本地 faster-whisper FastAPI 服务
volcengine_proxy/       火山引擎异步代理 FastAPI 服务
shared/                 URL 安全校验等公共代码
docs/coze-workflow.md   Coze HTTP 节点配置与纪要提示词
transcribe_volc_asr.py  火山引擎命令行联调脚本
tests/                  不联网的单元测试
```

## 1. 本地 faster-whisper

适合隐私优先、可接受本机推理耗时的场景。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r local_whisper\requirements.txt
Copy-Item .env.example .env
python -m uvicorn local_whisper.app:app --host 0.0.0.0 --port 8001
```

首次请求会下载并加载模型。CPU 默认使用 `base + int8`；有 CUDA 环境时可在 `.env` 中调整 `ASR_MODEL`、`ASR_DEVICE` 和 `ASR_COMPUTE_TYPE`。

请求示例：

```powershell
$body = @{
  audio_url = "https://files.example.com/meeting.mp3"
  file_name = "meeting.mp3"
  language = "zh"
  enable_timestamps = $true
  return_segments = $true
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8001/asr/transcribe `
  -ContentType application/json `
  -Body $body
```

本地模式当前不做说话人分离；传入 `enable_speaker_diarization=true` 会明确返回 400，避免产生虚假的说话人标签。

## 2. 火山引擎异步代理

适合长会议、需要火山引擎说话人信息，或本机算力不足的场景。火山引擎必须能访问 `PUBLIC_BASE_URL`，因此需要 HTTPS 反向代理、隧道或公网部署。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r volcengine_proxy\requirements.txt
Copy-Item .env.example .env
python -m uvicorn volcengine_proxy.app:app --host 0.0.0.0 --port 8002
```

`.env` 至少配置：

```dotenv
# 新版控制台使用 API Key
VOLC_ASR_API_KEY=your_api_key

# 旧版控制台则留空上面的值，同时填写这两个值
VOLC_APP_ID=
VOLC_ACCESS_TOKEN=

VOLC_ASR_RESOURCE_ID=volc.seedasr.auc
PUBLIC_BASE_URL=https://asr-proxy.example.com
PROXY_TOKEN=replace-with-a-long-random-string
AUDIO_ALLOWED_HOSTS=your-coze-file-host.example.com
```

创建任务：

```powershell
$headers = @{ "X-Proxy-Token" = "replace-with-a-long-random-string" }
$body = @{
  audio_url = "https://files.example.com/meeting.mp3"
  audio_name = "meeting.mp3"
  language = "zh-CN"
  hotwords = @("公司名", "产品名")
} | ConvertTo-Json

$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8002/asr/jobs `
  -Headers $headers `
  -ContentType application/json `
  -Body $body

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8002/asr/jobs/$($job.job_id)" `
  -Headers $headers
```

状态依次为 `queued`、`downloading`、`submitted`、`processing`，最终为 `succeeded` 或 `failed`。默认响应不含庞大的 `raw_result`；排查时可加 `?include_raw=true`。

代理兼容火山引擎新版 `X-Api-Key` 和旧版 `App ID + Access Token` 鉴权。默认资源 ID 为录音文件识别模型 2.0 的 `volc.seedasr.auc`，请以控制台实际开通项为准。

## 3. 独立火山引擎脚本

在 `.env` 配好凭据、`AUDIO_URL` 和资源 ID 后运行：

```powershell
python -m pip install -r volcengine_proxy\requirements.txt
python transcribe_volc_asr.py
```

成功后会生成：

- `outputs/transcript.json`
- `outputs/transcript.txt`
- `outputs/transcript_with_timestamps.txt`

这些输出已被 `.gitignore` 排除。

## Coze 工作流

完整 HTTP 节点参数、轮询方式和会议纪要提示词见 [docs/coze-workflow.md](docs/coze-workflow.md)。推荐流程：

```text
音频输入 → ASR HTTP 节点 → 状态判断/轮询 → 纪要 LLM 节点 → Markdown 输出
```

## 安全说明

- `.env`、音频、缓存、切片和转写结果均不会提交；提交前仍应执行 `git status` 复核。
- 两个服务会限制下载大小，并默认拒绝私网、回环和保留地址，降低 SSRF 风险。
- 用 `AUDIO_ALLOWED_HOSTS` 限制可下载域名。仅在可信内网需要读取 Coze 私网文件时设置 `ALLOW_PRIVATE_AUDIO_URLS=true`。
- 火山代理要求 `X-Proxy-Token`；未配置令牌时创建和查询任务都会返回 503。
- 代理只适合单实例或联调：任务状态位于内存，重启会丢失。生产环境应接入 Redis/数据库、持久化队列、访问日志脱敏、速率限制和监控。

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q local_whisper shared volcengine_proxy transcribe_volc_asr.py
python -m pytest
```

GitHub Actions 会在每次 push 和 pull request 时执行语法检查与单元测试。
