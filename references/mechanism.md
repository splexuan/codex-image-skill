# Codex 生图协议细节

实现来源：对 [ilab-conjure](https://github.com/kadevin/ilab-conjure) 的 `codex_image` 包逆向整理。
用于深入调试或官方接口变更后的适配。

## 1. 认证

登录态文件：`~/.codex/auth.json`

```json
{
  "tokens": {
    "access_token": "eyJ...",
    "refresh_token": "...",
    "id_token": "eyJ...",
    "account_id": "..."
  },
  "last_refresh": "2026-09-29T09:00:00+00:00"
}
```

- `access_token` 直接作 `Authorization: Bearer` 使用
- `account_id` 放进 `Chatgpt-Account-Id` 请求头；若缺失可退回从 `id_token` 的 JWT claim 解析
  （claim 路径 `https://api.openai.com/auth` → `chatgpt_account_id`）
- 顶层对象未必有 `tokens` 包裹，实现里做 `payload.get("tokens", payload)` 兜底

刷新：

```
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

client_id=app_EMoamEEZ73f0CkXaXp7hrann
&grant_type=refresh_token
&refresh_token=<refresh_token>
&scope=openid profile email
```

- `app_EMoamEEZ73f0CkXaXp7hrann` 是 Codex CLI 的公开 client id
- 刷新成功后必须**回写 auth.json**（保留原有其它字段），权限 0600，用临时文件 + 原子替换
- refresh_token 会轮换：若返回 `refresh_token_reused`，说明有别的进程先刷过了，需重新登录

## 2. 端点

| 用途 | 方法 | URL |
|---|---|---|
| Responses 通道 | POST | `https://chatgpt.com/backend-api/codex/responses` |
| Image 直连生图 | POST | `https://chatgpt.com/backend-api/codex/images/generations` |
| Image 直连编辑 | POST | `https://chatgpt.com/backend-api/codex/images/edits` |

## 3. 公共请求头

```
Authorization: Bearer <access_token>
Chatgpt-Account-Id: <account_id>        # 有才带
Originator: codex-tui
User-Agent: codex-tui/0.118.0 (Mac OS 26.3.1; arm64) Codex Desktop
Connection: Keep-Alive
Session_id: <uuid4>
X-Client-Request-Id: <uuid4>
```

`Originator` 与 `User-Agent` 是后端识别客户端身份的关键；两者必须同时存在且匹配 Codex 客户端的形态。

`Accept` 与 `Content-Type` 按通道区分：

- Responses：`Accept: text/event-stream`，`Content-Type: application/json`
- Image 直连：`Accept: application/json`；生图用 `application/json`，编辑用 `multipart/form-data`

## 4. Responses 通道

### 请求体

```json
{
  "instructions": "",
  "stream": true,
  "reasoning": {"effort": "medium", "summary": "auto"},
  "parallel_tool_calls": true,
  "include": ["reasoning.encrypted_content"],
  "model": "gpt-5.6-luna",
  "store": false,
  "tool_choice": {"type": "image_generation"},
  "input": [
    {"type": "message", "role": "user", "content": [
      {"type": "input_text", "text": "<提示词>"},
      {"type": "input_image", "image_url": "data:image/png;base64,..."}
    ]}
  ],
  "tools": [
    {"type": "image_generation", "action": "generate",
     "model": "gpt-image-2", "output_format": "png",
     "size": "1024x1024", "quality": "high"}
  ]
}
```

要点：

- `stream: true` 是必须的，响应是 SSE
- `model` 是调度模型，**不参与画图**；画图模型在 `tools[0].model`
- `tool_choice` 必须强制指向 `image_generation`，否则模型可能只回文本
- 有参考图时 `tools[0].action` 改成 `"edit"`，并且 `input_image` 用 data URL
- 可选字段：`size`、`quality`、`background`、`moderation`、`output_compression`、`partial_images`
- 掩膜用 `tools[0].input_image_mask = {"image_url": "<data url>"}`
- 可选联网搜索：`tools` 头部插入 `{"type":"web_search","search_context_size":"low"}`，
  同时 `tool_choice` 改为 `"required"`、`parallel_tool_calls` 改为 `false`

### 响应（SSE）

逐行解析 `data: ` 前缀的 JSON 事件：

| 事件类型 | 用途 |
|---|---|
| `response.output_item.done` | 单个输出项完成，按 `output_index` 缓存 `item` |
| `response.completed` | 终态，`response.output` 是完整输出数组 |
| `response.failed` / `response.incomplete` | 失败终态，需报错 |
| `error` | 流内错误 |

图片在输出数组中 `type == "image_generation_call"` 的项里，字段 `result` 是 base64 图片。
该 item 还带 `revised_prompt`、`output_format`、`size`、`background`、`quality`。

用量信息在 `response.tool_usage.image_gen` 或 `response.usage`。

若 `response.completed` 的 `output` 为空，用缓存的 `output_item.done` 按 index 重建。
找不到 `image_generation_call` 时先看 output 里的 `message` 项文本——通常是拒答原因。

## 5. Image 直连通道

### 生图（JSON）

```json
{"model": "gpt-image-2", "prompt": "...", "n": 1, "output_format": "png",
 "size": "1024x1024", "quality": "high"}
```

可选：`background`、`moderation`、`output_compression`、`input_fidelity`
（`input_fidelity` 对 `gpt-image-2` 不支持，需跳过）。

### 编辑（multipart/form-data）

同名字段走 form，图片走文件部分：

- 表单字段：上面 JSON 里除 `images` / `mask` 外的所有键
- 文件字段：`image`（每个参考图一个，文件名 `image-1.png`…）、`mask`（可选）

参考图必须是 base64 data URL，需先解码成二进制再塞进 multipart。

### 响应

标准 OpenAI images 形态：

```json
{"data": [{"b64_json": "...", "revised_prompt": "..."}], "usage": {...}}
```

也可能返回 `url` 而非 `b64_json`，此时需自行下载。

## 6. 错误语义

| HTTP | 含义 |
|---|---|
| 401 | token 失效；有 refresh_token 就先刷新再重试一次 |
| 403 | 账号无权使用该后端接口 |
| 429 / body 含 `usage_limit` | 套餐额度用尽或频率超限 |
| 200 但 SSE 无图片项 | 提示词被拒答，或接口结构变更 |

重试策略：401 → 刷新 token → 重试一次（仅一次，避免刷爆 refresh_token）。
多账号轮换（原项目支持）在本技能中未实现。

## 7. 已知风险

- 这是 ChatGPT 客户端内部接口，**官方随时可能变更字段或收紧权限**
- 若返回结构变化，先跑 `--dry-run` 确认请求体，再抓实际响应比对本文档
- 不要提交 `auth.json`、API key、输入图、生成结果到版本库
