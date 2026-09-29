---
name: codex-image
description: 用本机 Codex / ChatGPT 的 OAuth 登录态调用 ChatGPT 后端生成与编辑图片（GPT Image / gpt-image-2），完全不需要 OpenAI API Key。当用户提出"用 codex 生图""本机 codex 画图""不用 API key 生成图片""codex 帮我画一张""用 ChatGPT 账号出图"，或要求在已登录 Codex CLI 的机器上做文生图、参考图生成、图像编辑、批量出图时使用。也用于排查 Codex 生图链路报错（401 未授权、refresh_token 失效、额度受限、接口变更）。
agent_created: true
---

# Codex Image

## 概述

复用本机 Codex CLI 已登录的 OAuth 凭据（`~/.codex/auth.json`），直接请求 ChatGPT 后端 Codex 通道生成图片。**不需要 OpenAI API Key**，成本走用户自己的 ChatGPT/Codex 套餐额度。

这不是 OpenAI 官方推荐的集成方式——它复用的是 ChatGPT 客户端内部接口，可能随官方更新而变更、失效，或受账号与用量规则限制。仅适合个人本机使用，不要用于生产、团队部署或对外服务。

## 使用前提

1. 本机已安装 Codex CLI
2. 至少成功执行过一次 `codex login`，即 `~/.codex/auth.json` 存在且含 `tokens.access_token`

开始前先确认这两点；若文件不存在，直接告知用户需要先登录，不要尝试伪造凭据。

## 快速开始

核心脚本：`scripts/codex_image.py`，零第三方依赖，只用 Python 标准库。

```bash
# 文生图（默认走 responses 通道）
python scripts/codex_image.py --prompt "一只戴墨镜的柴犬，摄影棚布光" --out dog.png

# 参考图生成 / 图像编辑（给了 --image 就自动切换为编辑模式）
python scripts/codex_image.py --prompt "改成赛博朋克夜景" --image in.png --out out.png

# 指定尺寸与质量、走 Image 直连通道
python scripts/codex_image.py --prompt "..." --out x.png --channel images --size 1024x1024 --quality high

# 批量出 3 张（--out 此时当作输出目录）
python scripts/codex_image.py --prompt "..." --out shots/ --n 3

# 只查看将要发出的请求体，不联网
python scripts/codex_image.py --prompt "..." --out x.png --dry-run
```

先跑一次 `--dry-run` 验证请求体是否符合预期，是排查问题最快的手段。

## 通道选择

| 通道 | 参数 | 端点 | 何时用 |
|---|---|---|---|
| Codex Responses | `--channel responses`（默认） | `/backend-api/codex/responses` | 首选。SSE 流式，工具调用式出图，对复杂/长提示词更稳 |
| Codex Image 直连 | `--channel images` | `/backend-api/codex/images/...` | 需要 `n>1` 一次性多图，或 responses 通道异常时备选 |

两个通道都支持 `--size`、`--quality`、`--output-format`、参考图与编辑。
`responses` 通道的主模型（`--main-model`，默认 `gpt-5.6-luna`）只负责调度，不参与画图；真正画图的是 `--model`（默认 `gpt-image-2`）。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--prompt` | 提示词，必填 |
| `--out` | 输出路径；`--n > 1` 时当目录用，自动命名 `image-1.png` |
| `--image` | 参考图，可重复传入 |
| `--n` | 生成张数，1–10（默认 1）；大于 1 时 `--out` 视为目录 |
| `--size` | 如 `1024x1024`、`1536x1024`、`1024x1536` |
| `--quality` | `low` / `medium` / `high` |
| `--output-format` | `png` / `jpeg` / `webp`；`jpg` 自动归一为 `jpeg` |
| `--model` | 画图模型 |
| `--main-model` | responses 通道调度模型 |
| `--auth-file` | 登录态路径，默认 `~/.codex/auth.json` |
| `--proxy` | HTTP(S) 代理，如 `http://127.0.0.1:7890` |
| `--dry-run` | 只打印请求体 |

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 找不到 `~/.codex/auth.json` | 没装 Codex CLI 或没登录 | 安装后执行 `codex login` |
| `401 未授权` | access_token 过期且刷新失败 | 重新 `codex login` |
| `refresh_token_reused` / 提示已被轮换 | 多个 Codex 客户端同时刷新，token 互相打架 | `codex logout` 后重新 `codex login`，避免多开 |
| `额度/频率受限` | ChatGPT 套餐用量用尽 | 换账号或等额度恢复，这不是脚本能绕过的 |
| `SSE 流里没有 image_generation_call` | 提示词被拒答，或后端接口变更 | 先换提示词重试；仍失败则说明接口变更，需重新抓包比对 |
| 网络超时 / 连不上 `chatgpt.com` | 需要代理 | 加 `--proxy` |
| 响应中断（`IncompleteRead` / 流被截断） | SSE 长连接被网络或代理掐断 | 已内置容错：保留已收数据 + 自动重试 3 次。持续失败则换 `--proxy` 出口 |
| 生成耗时过长 | 高分辨率或高质量参数导致 | 单次请求超时 600s，可下调 `--size` / `--quality` |

## 何时不要用这个技能

- 用户只是想"生成一张图"且没有 Codex 登录态 → 直接用内置的图片生成能力，不要绕这条路
- 用户需要生产级、稳定、可对外服务的生图 → 走 OpenAI 官方 API（需 API Key）
- 用户明确要求使用其他供应商（Gemini、通义等）→ 不适用

## 参考

- `references/mechanism.md` —— 完整的协议细节：端点、请求头、请求体字段、SSE 事件结构、错误语义。需要深入调试或适配新接口时读它。
- `scripts/codex_image.py` —— 实现本体，可直接运行，也可作为代码片段嵌入其它程序。
