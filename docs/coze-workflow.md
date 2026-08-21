# Coze Studio 工作流接入

这个仓库负责把音频转成结构化文本；会议摘要、决策项和待办项由 Coze Studio 的大模型节点生成。

## 方案 A：本地 faster-whisper

在 HTTP 节点中发送：

```http
POST http://host.docker.internal:8001/asr/transcribe
Content-Type: application/json
```

```json
{
  "audio_url": "{{input.audio_url}}",
  "file_name": "{{input.file_name}}",
  "language": "zh",
  "enable_timestamps": true,
  "return_segments": true
}
```

读取响应中的 `full_text` 和 `segments`。Docker Desktop 中的 Coze Studio 访问宿主机服务时，通常使用 `host.docker.internal`，而不是 `127.0.0.1`。

## 方案 B：火山引擎异步代理

第一个 HTTP 节点创建任务：

```http
POST https://your-proxy.example.com/asr/jobs
Content-Type: application/json
X-Proxy-Token: {{secrets.proxy_token}}
```

```json
{
  "audio_url": "{{input.audio_url}}",
  "audio_name": "{{input.file_name}}",
  "language": "zh-CN",
  "hotwords": ["公司名", "产品名"],
  "poll_interval_sec": 10,
  "max_wait_sec": 3600
}
```

保存返回的 `job_id`，再用循环和 HTTP 节点查询：

```http
GET https://your-proxy.example.com/asr/jobs/{{job_id}}
X-Proxy-Token: {{secrets.proxy_token}}
```

当 `status` 为 `succeeded` 时读取 `transcript_full` 或 `transcript_with_time`；为 `failed` 时读取 `error`。代理任务保存在内存中，服务重启后不会恢复，生产环境应替换为数据库和持久化任务队列。

## 纪要提示词模板

```text
你是严谨的会议纪要助手。仅依据下面的转写文本输出，不补充未出现的事实。

按以下结构输出 Markdown：
1. 会议主题与结论
2. 关键讨论（按议题归类）
3. 已确认决策
4. 待办事项（负责人、事项、截止时间；未提及则写“待确认”）
5. 风险与待澄清问题

转写文本：
{{transcript_with_time}}
```

## 网络与安全

- 不要把 `PROXY_TOKEN` 或火山引擎凭据写入 Coze 普通变量或日志，使用密钥变量。
- Internet 暴露的代理应启用 HTTPS，并限制来源、速率和最大请求体。
- `AUDIO_ALLOWED_HOSTS` 应设置为实际文件域名。只有在可信内网确有需要时才把 `ALLOW_PRIVATE_AUDIO_URLS` 改为 `true`。
- `/audio/{filename}` 必须能被火山引擎访问，因此不能整体放在登录页之后；文件名使用随机 128 位标识，并在任务结束后默认删除。
