# codex-image

用本机 Codex / ChatGPT 的 OAuth 登录态调用 ChatGPT 后端生成与编辑图片（GPT Image / `gpt-image-2`），**完全不需要 OpenAI API Key**。

以 Agent Skill 形式发布，核心是一个零第三方依赖的单文件 Python 脚本。

## 原理

复用 Codex CLI 已经登录好的凭据 `~/.codex/auth.json`，直接请求 ChatGPT 后端的 Codex 通道：

1. 读 `~/.codex/auth.json` 的 `access_token` / `account_id`；token 过期时用 `refresh_token` 刷新并原子回写
2. POST 到 ChatGPT 后端，请求里带一个 `image_generation` 工具
   - `responses` 通道 → `chatgpt.com/backend-api/codex/responses`（SSE 流式，默认）
   - `images` 通道 → `chatgpt.com/backend-api/codex/images/generations`（普通 JSON）
3. 从响应里取出 base64 图片写盘

> ⚠️ 这是 ChatGPT 客户端内部接口，**不是 OpenAI 官方推荐的集成方式**，可能随时变更或失效，也可能受账号与用量规则限制。

## 前提

1. 已安装 [Codex CLI](https://github.com/openai/codex)
2. 成功执行过一次 `codex login`，即 `~/.codex/auth.json` 存在且含 `access_token`

## 安装

**作为 Agent Skill**（WorkBuddy / Claude Code 等）：

```bash
git clone git@github.com:splexuan/codex-iamge-skill.git ~/.workbuddy/skills/codex-image
```

**只用脚本**（零依赖，Python 3.9+）：

```bash
python scripts/codex_image.py --prompt "一只戴墨镜的柴犬" --out dog.png
```

## 用法

```bash
# 文生图
python scripts/codex_image.py --prompt "一只戴墨镜的柴犬，摄影棚布光" --out dog.png

# 参考图生成 / 图像编辑（给了 --image 就自动切编辑模式）
python scripts/codex_image.py --prompt "改成赛博朋克夜景" --image in.png --out out.png

# 指定尺寸质量、走 Image 直连通道
python scripts/codex_image.py --prompt "..." --out x.png --channel images --size 1024x1024 --quality high

# 批量出 3 张（--out 此时当输出目录）
python scripts/codex_image.py --prompt "..." --out shots/ --n 3

# 只查看将要发出的请求体，不联网
python scripts/codex_image.py --prompt "..." --out x.png --dry-run
```

### 参数

| 参数 | 说明 |
|---|---|
| `--prompt` | 提示词，必填 |
| `--out` | 输出路径；`--n > 1` 时当目录用，自动命名 `image-1.png` |
| `--channel` | `responses`（默认）或 `images` |
| `--image` | 参考图路径，可重复传入 |
| `--n` | 生成张数，1–10（默认 1） |
| `--size` | 如 `1024x1024`、`1536x1024`、`1024x1536` |
| `--quality` | `low` / `medium` / `high` |
| `--output-format` | `png` / `jpeg` / `webp` |
| `--model` | 画图模型，默认 `gpt-image-2` |
| `--main-model` | responses 通道的调度模型（不参与画图） |
| `--auth-file` | 登录态路径，默认 `~/.codex/auth.json` |
| `--proxy` | HTTP(S) 代理，如 `http://127.0.0.1:7890` |
| `--dry-run` | 只打印请求体，不联网 |

退出码：`0` 成功 / `1` 参数或环境问题 / `2` 请求或生成失败。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 找不到 `~/.codex/auth.json` | 没装 Codex CLI 或没登录 | 安装后执行 `codex login` |
| `401 未授权` | token 过期且刷新失败 | 重新 `codex login` |
| `refresh_token_reused` | 多个客户端同时刷新，token 互相作废 | `codex logout` 后重新登录，避免多开 |
| 额度 / 频率受限 | ChatGPT 套餐用量用尽 | 换账号或等额度恢复 |
| 响应中断（`IncompleteRead`） | SSE 长连接被网络或代理掐断 | 已内置容错 + 自动重试 3 次；持续失败则加 `--proxy` |
| 连不上 `chatgpt.com` | 需要代理 | 加 `--proxy` |

## 安全与合规

- 脚本会读取 `~/.codex/auth.json` 中的 OAuth 凭据，并以 `Bearer` 发给 `chatgpt.com`——目标域名即凭据颁发方本身，不存在外传给第三方。
- **不要把 `auth.json`、API key、生成图上传到任何仓库。**
- 不要把本工具暴露到公网或做局域网共享。OpenAI 的使用条款禁止账号共享，且共享会让所有调用都消耗你账号的额度。
- 生成内容的合规责任由使用者自行承担。

## 来源与许可

协议机制整理自 [kadevin/ilab-conjure](https://github.com/kadevin/ilab-conjure) 的 `codex_image` 包，该项目以 **AGPLv3** 发布；本仓库为其独立重写的精简实现。

由于实现细节派生自 AGPLv3 项目，建议沿用 AGPLv3 以规避协议风险。

原项目并未授权其项目名称、Logo 或个人素材，此处仅引用其公开的协议实现细节。
