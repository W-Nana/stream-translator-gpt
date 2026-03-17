# SSE API

本文档描述 `stream-translator-gpt` 内置 SSE 服务的接口格式。

## 启用方式

```bash
stream-translator-gpt <URL> --sse_port 18000
```

可选参数：

- `--sse_host`，默认 `127.0.0.1`
- `--sse_port`，启用 SSE 所需
- `--sse_path`，默认 `/events`

默认事件端点：

```text
http://127.0.0.1:18000/events
```

健康检查端点：

```text
http://127.0.0.1:18000/healthz
```

## 协议说明

- 协议：HTTP/1.1 + Server-Sent Events
- 响应头：
  - `Content-Type: text/event-stream; charset=utf-8`
  - `Cache-Control: no-cache, no-transform`
  - `Connection: keep-alive`
  - `Access-Control-Allow-Origin: *`
- 服务端会定期发送注释行作为 keep-alive：

```text
: keep-alive
```

- 每条事件包含：
  - `id`
  - `event`
  - `data`

标准格式示例：

```text
id: 12
event: result
data: {"task_id":3,"output_stage":"transcript",...}
```

## 事件类型

当前有两类业务事件：

- `lifecycle`
- `result`

### lifecycle

用于描述服务状态变化。

字段：

- `status`: `ready` | `started` | `stopped`
- `message`: 状态说明，可能为 `null`
- `timestamp`: UTC ISO 8601 时间字符串

示例：

```json
{
  "status": "ready",
  "message": "SSE endpoint available at http://127.0.0.1:18000/events",
  "timestamp": "2026-03-18T12:34:56.789Z"
}
```

状态含义：

- `ready`: SSE server 已启动并可接受连接
- `started`: 音频处理流水线已开始工作
- `stopped`: 程序结束，流水线停止

### result

用于输出转录或翻译结果。

字段：

- `task_id`: 同一段音频的稳定 ID
- `output_stage`: `transcript` | `translation` | `complete`
- `timestamp`: 事件发送时间，UTC ISO 8601
- `time_range.start`: 音频片段开始秒数
- `time_range.end`: 音频片段结束秒数
- `time_range.start_text`: 格式化开始时间
- `time_range.end_text`: 格式化结束时间
- `transcript`: 转录文本，可能为 `null`
- `translation`: 翻译文本，可能为 `null`
- `translation_failed`: 翻译是否失败
- `output_whisper_result`: 当前运行参数是否允许输出转录文本
- `output_timestamps`: 当前运行参数是否允许输出时间戳
- `display_text`: 按当前输出阶段和参数拼好的显示文本

#### output_stage 说明

- `transcript`
  - 转录先到达时发送
  - 通常只有 `transcript`
  - 用于降低体感延迟
- `translation`
  - 翻译完成后发送
  - 与前面的 `transcript` 使用同一个 `task_id`
  - 客户端应按 `task_id` 将它追加到已有 UI 项
- `complete`
  - 未启用翻译时的单次结果
  - 或兼容旧流程时的完整结果

#### transcript 示例

```json
{
  "task_id": 7,
  "output_stage": "transcript",
  "timestamp": "2026-03-18T12:35:01.120Z",
  "time_range": {
    "start": 12.3,
    "end": 17.8,
    "start_text": "00:00:12,3",
    "end_text": "00:00:17,8"
  },
  "transcript": "こんにちは、みなさん。",
  "translation": null,
  "translation_failed": false,
  "output_whisper_result": true,
  "output_timestamps": true,
  "display_text": "00:00:12,3 --> 00:00:17,8\nこんにちは、みなさん。"
}
```

#### translation 示例

```json
{
  "task_id": 7,
  "output_stage": "translation",
  "timestamp": "2026-03-18T12:35:02.904Z",
  "time_range": {
    "start": 12.3,
    "end": 17.8,
    "start_text": "00:00:12,3",
    "end_text": "00:00:17,8"
  },
  "transcript": "こんにちは、みなさん。",
  "translation": "大家好。",
  "translation_failed": false,
  "output_whisper_result": true,
  "output_timestamps": true,
  "display_text": "00:00:12,3 --> 00:00:17,8\n大家好。"
}
```

#### complete 示例

```json
{
  "task_id": 8,
  "output_stage": "complete",
  "timestamp": "2026-03-18T12:35:05.001Z",
  "time_range": {
    "start": 18.0,
    "end": 21.4,
    "start_text": "00:00:18,0",
    "end_text": "00:00:21,4"
  },
  "transcript": "hello",
  "translation": null,
  "translation_failed": false,
  "output_whisper_result": true,
  "output_timestamps": false,
  "display_text": "hello"
}
```

## 客户端处理建议

- 用 `task_id` 作为主键合并同一段音频的多个事件
- 收到 `transcript` 时立即显示
- 收到 `translation` 时追加到同一条记录
- 不要假设 `translation` 一定存在
- `translation_failed=true` 时可保留 transcript，并标记翻译失败
- `display_text` 适合直接展示
- 如果要做结构化 UI，优先使用 `transcript`、`translation`、`time_range`、`output_stage`

## 健康检查

请求：

```bash
curl http://127.0.0.1:18000/healthz
```

响应示例：

```json
{
  "status": "ok",
  "clients": 2,
  "path": "/events"
}
```

字段：

- `status`: 固定为 `ok`
- `clients`: 当前已连接的 SSE 客户端数量
- `path`: 当前事件路径

## 参考客户端

- 浏览器示例：[examples/sse_client.html](/mnt/HDD/AI/stream-translator-gpt/examples/sse_client.html)
- Python 示例：[examples/sse_client.py](/mnt/HDD/AI/stream-translator-gpt/examples/sse_client.py)
