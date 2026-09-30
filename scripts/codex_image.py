#!/usr/bin/env python3
"""用本机 Codex / ChatGPT OAuth 登录态调用 ChatGPT 后端生图 —— 零第三方依赖。

不需要 OpenAI API Key：复用 Codex CLI 已经写好的 ~/.codex/auth.json。

原理三步：
  1. 读 ~/.codex/auth.json 的 access_token / account_id；过期用 refresh_token 刷
  2. POST 到 ChatGPT 后端 Codex 通道，带一个 image_generation 工具
       responses 通道 → https://chatgpt.com/backend-api/codex/responses        (SSE 流)
       images   通道 → https://chatgpt.com/backend-api/codex/images/generations (JSON)
  3. 从返回里取 base64 图片写盘

用法：
  python codex_image.py --prompt "一只戴墨镜的柴犬" --out dog.png
  python codex_image.py --prompt "改成赛博朋克风" --image in.png --out out.png
  python codex_image.py --prompt "..." --out x.png --channel images --size 1024x1024
  python codex_image.py --prompt "..." --out shots/ --n 3
  python codex_image.py --prompt "..." --out x.png --dry-run     # 只看请求体，不联网

退出码：0 成功 / 1 参数或环境问题 / 2 请求或生成失败
"""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量（取自 ilab-conjure 的 codex_image 包）
# ---------------------------------------------------------------------------

AUTH_PATH = Path.home() / ".codex" / "auth.json"

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = f"{CODEX_BASE}/responses"
IMAGES_GENERATIONS_URL = f"{CODEX_BASE}/images/generations"
IMAGES_EDITS_URL = f"{CODEX_BASE}/images/edits"

# 让后端把你认成官方 Codex 客户端的关键请求头
CODEX_ORIGINATOR = "codex-tui"
CODEX_USER_AGENT = "codex-tui/0.118.0 (Mac OS 26.3.1; arm64) Codex Desktop"

DEFAULT_MAIN_MODEL = "gpt-5.6-luna"
DEFAULT_IMAGE_MODEL = "gpt-image-2"

REQUEST_TIMEOUT = 600
RESPONSES_ATTEMPTS = 3     # SSE 流被截断时的最大尝试次数
MAX_IMAGES_PER_RUN = 10    # 单次运行最多生成张数（每次请求都消耗账号额度）
ALLOWED_OUTPUT_FORMATS = ("png", "jpeg", "webp")
MAX_RESPONSE_BYTES = 320 * 1024 * 1024   # 响应体上限，防止异常服务端把内存撑爆


def fail(message: str, code: int = 2) -> "None":
    print(f"错误: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 1. 登录态
# ---------------------------------------------------------------------------

def load_auth(auth_file: Path) -> dict:
    if not auth_file.is_file():
        fail(
            f"找不到 {auth_file}\n"
            "需要先安装 Codex CLI 并执行一次 `codex login`，登录态会写入该文件。",
            code=1,
        )
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        fail(f"读取 {auth_file} 失败：{exc}", code=1)

    tokens = payload.get("tokens", payload)
    auth = {
        "access_token": str(tokens.get("access_token") or ""),
        "refresh_token": str(tokens.get("refresh_token") or ""),
        "account_id": str(tokens.get("account_id") or ""),
        "raw": payload,
    }
    if not auth["access_token"]:
        fail(f"{auth_file} 里没有 access_token，请重新执行 `codex login`。", code=1)
    return auth


def refresh_access_token(auth: dict, auth_file: Path) -> dict:
    if not auth["refresh_token"]:
        fail("access_token 已失效，且 auth.json 里没有 refresh_token。请重新执行 `codex login`。")

    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": auth["refresh_token"],
        "scope": "openid profile email",
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            token_payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if "refresh_token_reused" in detail or "already been used" in detail:
            fail(
                "refresh_token 已被其它进程轮换过，当前这份作废了。\n"
                "请执行 `codex logout` 后再 `codex login`（注意别同时开着多个 Codex 客户端）。"
            )
        fail(f"刷新 Codex token 失败：HTTP {exc.code}: {detail[:500]}")
    except OSError as exc:
        fail(f"刷新 Codex token 时网络异常：{exc}")

    access_token = str(token_payload.get("access_token") or auth["access_token"])
    refresh_token = str(token_payload.get("refresh_token") or auth["refresh_token"])

    raw = auth["raw"]
    tokens = dict(raw.get("tokens", {}))
    tokens.update({"access_token": access_token, "refresh_token": refresh_token})
    raw["tokens"] = tokens
    tmp = auth_file.with_suffix(auth_file.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        os.replace(tmp, auth_file)
        try:
            os.chmod(auth_file, 0o600)
        except OSError:
            pass
    except OSError as exc:
        fail(f"新 token 无法写回 {auth_file}：{exc}（原文件未被破坏）")

    auth.update(access_token=access_token, refresh_token=refresh_token)
    return auth


# ---------------------------------------------------------------------------
# 2. 请求
# ---------------------------------------------------------------------------

def codex_headers(auth: dict, *, accept: str, content_type: str) -> dict:
    headers = {
        "Content-Type": content_type,
        "Authorization": f"Bearer {auth['access_token']}",
        "Accept": accept,
        "Connection": "Keep-Alive",
        "Originator": CODEX_ORIGINATOR,
        "User-Agent": CODEX_USER_AGENT,
        "Session_id": str(uuid.uuid4()),
        "X-Client-Request-Id": str(uuid.uuid4()),
    }
    if auth["account_id"]:
        headers["Chatgpt-Account-Id"] = auth["account_id"]
    return headers


def explain_http_error(status: int, body_text: str) -> str:
    low = body_text.lower()
    if status == 401:
        return "401 未授权：token 失效且刷新未成功。请重新执行 `codex login`。"
    if status == 403:
        return f"403 被拒绝：账号可能无权使用该后端接口。\n{body_text[:400]}"
    if status == 429 or "usage_limit" in low or "rate limit" in low:
        return f"额度/频率受限（ChatGPT 套餐用量用尽或过快）。\n{body_text[:600]}"
    return f"HTTP {status}: {body_text[:800]}"


def read_full(resp, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """容错读取响应体。

    SSE 响应走 chunked 传输，网络抖动或代理掐连接时 http.client 会抛
    IncompleteRead。此时已收到的数据往往已包含完整事件，保留下来继续解析，
    而不是整个请求直接失败。

    同时设总字节上限：万一服务端行为异常持续吐数据，不至于把内存撑爆。
    """
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"响应体超过 {max_bytes // (1024 * 1024)}MB 上限，已中止。")
            chunks.append(chunk)
    except http.client.IncompleteRead as exc:
        if exc.partial:
            chunks.append(exc.partial)
    except (TimeoutError, OSError):
        pass
    return b"".join(chunks)


def post_with_auth_retry(*, url: str, auth: dict, auth_file: Path,
                         accept: str, content_type: str, body: bytes,
                         opener: urllib.request.OpenerDirector) -> bytes:
    def once(headers: dict) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.status, read_full(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, read_full(exc)
        except urllib.error.URLError as exc:
            fail(f"网络错误：{exc.reason}（如需代理请加 --proxy）")

    headers = codex_headers(auth, accept=accept, content_type=content_type)
    status, data = once(headers)

    if status == 401 and auth.get("refresh_token"):
        auth = refresh_access_token(auth, auth_file)
        headers = codex_headers(auth, accept=accept, content_type=content_type)
        status, data = once(headers)

    if not (200 <= status < 300):
        fail(explain_http_error(status, data.decode("utf-8", errors="replace")))
    return data


# ---------------------------------------------------------------------------
# 3a. responses 通道
# ---------------------------------------------------------------------------

def build_responses_payload(*, prompt, image_model, main_model,
                            size, quality, output_format, images) -> dict:
    tool: dict = {
        "type": "image_generation",
        "action": "edit" if images else "generate",
        "model": image_model,
        "output_format": output_format,
    }
    for key, value in (("size", size), ("quality", quality)):
        if value:
            tool[key] = value

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    content.extend({"type": "input_image", "image_url": url} for url in images)

    return {
        "instructions": "",
        "stream": True,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
        "model": main_model,
        "store": False,
        "tool_choice": {"type": "image_generation"},
        "input": [{"type": "message", "role": "user", "content": content}],
        "tools": [tool],
    }


class GenerationFailed(RuntimeError):
    """流被截断或事件缺失导致的失败，值得重试。"""


def parse_responses_sse(body: bytes) -> tuple[str, dict]:
    by_index: dict[int, dict] = {}

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == b"[DONE]":
            continue
        try:
            event = json.loads(chunk.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        if etype in ("error", "response.failed", "response.incomplete"):
            fail(f"生成失败：{json.dumps(event, ensure_ascii=False)[:800]}")

        if etype == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                by_index[event.get("output_index", len(by_index))] = item
        elif etype == "response.completed":
            response = event.get("response") or {}
            output = response.get("output") or [by_index[i] for i in sorted(by_index)]
            for item in output:
                if isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result"):
                    return item["result"], item
            if response.get("usage"):
                print(f"  usage: {json.dumps(response['usage'], ensure_ascii=False)[:200]}", file=sys.stderr)

    raise GenerationFailed(
        "SSE 流里没有 image_generation_call —— 流被截断，或后端接口已变更。"
    )


# ---------------------------------------------------------------------------
# 3b. images 通道
# ---------------------------------------------------------------------------

def build_images_payload(*, prompt, image_model, size, quality, output_format, n) -> dict:
    payload: dict = {
        "model": image_model,
        "prompt": prompt,
        "n": n,
        "output_format": output_format,
    }
    for key, value in (("size", size), ("quality", quality)):
        if value:
            payload[key] = value
    return payload


def parse_images_json(body: bytes, opener: urllib.request.OpenerDirector) -> list[tuple[str, dict]]:
    if not body.strip():
        fail("images 通道返回了空响应体（可能被网关或代理拦截）。")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = body[:300].decode("utf-8", errors="replace")
        fail(f"images 通道返回的不是有效 JSON（{type(exc).__name__}）：{preview}")

    if not isinstance(payload, dict):
        fail(f"images 通道返回了非对象 JSON：{str(payload)[:200]}")

    if isinstance(payload.get("error"), dict):
        fail(f"生成失败：{payload['error']}")

    data = payload.get("data")
    if not isinstance(data, list):
        fail(f"响应里没有 data 数组：{json.dumps(payload, ensure_ascii=False)[:300]}")

    results: list[tuple[str, dict]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            results.append((str(item["b64_json"]), item))
        elif item.get("url"):
            try:
                with opener.open(item["url"], timeout=REQUEST_TIMEOUT) as resp:
                    results.append((base64.b64encode(read_full(resp)).decode("ascii"), item))
            except (urllib.error.URLError, OSError) as exc:
                fail(f"下载结果图失败：{exc}")
    if not results:
        fail("响应里没有图片数据（data 为空，或每项既无 b64_json 也无 url）。")
    return results


def decode_and_write(b64: str, out_path: Path) -> Path:
    """解码 base64 并写盘，把两类常见故障翻译成人话。

    validate=True 能揪出被截断或掺了非法字符的 base64 —— 否则会静默写出损坏的图片。
    先剔除空白字符，因为标准 base64 允许换行，而严格校验不接受。
    """
    try:
        raw = base64.b64decode("".join(str(b64).split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        fail(f"返回的图片 base64 无法解码（{exc}）。响应可能被截断，可重试一次。")
    if not raw:
        fail("返回的图片数据为空。")

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
    except OSError as exc:
        fail(f"写入 {out_path} 失败：{exc}")
    return out_path


def build_multipart(fields: dict, files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----codexskill" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    for name, filename, ctype, data in files:
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
        ).encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def to_data_url(path: str) -> tuple[str, str, bytes, str]:
    file_path = Path(path)
    if not file_path.is_file():
        fail(f"参考图不存在：{path}", code=1)
    mime = mimetypes.guess_type(file_path.name)[0] or "image/png"
    if not mime.startswith("image/"):
        mime = "image/png"
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        fail(f"读取参考图 {path} 失败：{exc}", code=1)
    if not data:
        fail(f"参考图 {path} 是空文件。", code=1)
    return (
        f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        mime,
        data,
        file_path.name,
    )


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def resolve_outputs(out: str, count: int) -> list[Path]:
    """out 是文件路径（count==1）或目录（count>1）。"""
    target = Path(out)
    if count == 1:
        return [target]
    target.mkdir(parents=True, exist_ok=True)
    return [target / f"image-{i}" for i in range(1, count + 1)]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="用本机 Codex / ChatGPT OAuth 登录态生图（无需 API Key）"
    )
    parser.add_argument("--prompt", required=True, help="提示词")
    parser.add_argument("--out", required=True,
                        help="输出文件；--n > 1 时视为输出目录，自动命名 image-1.png…")
    parser.add_argument("--channel", choices=("responses", "images"), default="responses",
                        help="responses=Codex Responses（默认，整图更稳）；images=Image 直连")
    parser.add_argument("--image", action="append", default=[],
                        help="参考图路径，可重复；给了就自动变编辑模式")
    parser.add_argument("--n", type=int, default=1, help="生成张数，默认 1")
    parser.add_argument("--size", help="如 1024x1024 / 1536x1024 / 1024x1536")
    parser.add_argument("--quality", help="如 low / medium / high")
    parser.add_argument("--output-format", default="png", help="png / jpeg / webp")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL, help="画图模型，默认 gpt-image-2")
    parser.add_argument("--main-model", default=DEFAULT_MAIN_MODEL,
                        help="responses 通道的调度模型（不参与画图）")
    parser.add_argument("--auth-file", default=str(AUTH_PATH))
    parser.add_argument("--proxy", help="HTTP(S) 代理，如 http://127.0.0.1:7890")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不联网、不读登录态")
    args = parser.parse_args()

    if not args.prompt.strip():
        fail("--prompt 不能为空。", code=1)
    if args.n < 1:
        fail("--n 必须 >= 1。", code=1)
    if args.n > MAX_IMAGES_PER_RUN:
        fail(
            f"--n 最多 {MAX_IMAGES_PER_RUN}（每次请求都消耗账号额度，一次烧太多容易触发限流）。",
            code=1,
        )

    # 归一输出格式：jpg 收成 jpeg（后端只认 jpeg），其余不支持的直接拒绝
    fmt = args.output_format.strip().lower().lstrip(".")
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        fail(
            f"--output-format 只支持 {'/'.join(ALLOWED_OUTPUT_FORMATS)}，收到 {args.output_format!r}。",
            code=1,
        )
    args.output_format = fmt

    auth_file = Path(args.auth_file)
    data_urls, raw_files = [], []
    for path in args.image:
        url, mime, data, name = to_data_url(path)
        data_urls.append(url)
        raw_files.append((mime, data, name))

    # ---- dry-run ----
    if args.dry_run:
        if args.channel == "responses":
            payload = build_responses_payload(
                prompt=args.prompt, image_model=args.model, main_model=args.main_model,
                size=args.size, quality=args.quality,
                output_format=args.output_format, images=data_urls,
            )
            shown = json.loads(json.dumps(payload))
            for part in shown["input"][0]["content"]:
                if part["type"] == "input_image":
                    part["image_url"] = "<data:image/...base64 省略>"
            print(f"POST {RESPONSES_URL}")
            print(json.dumps(shown, ensure_ascii=False, indent=2))
        else:
            payload = build_images_payload(
                prompt=args.prompt, image_model=args.model, size=args.size,
                quality=args.quality, output_format=args.output_format, n=args.n,
            )
            target = IMAGES_EDITS_URL if raw_files else IMAGES_GENERATIONS_URL
            kind = "multipart/form-data" if raw_files else "application/json"
            print(f"POST {target}  ({kind})")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未联网。去掉 --dry-run 即真实生成。")
        return

    # ---- 真实生成 ----
    auth = load_auth(auth_file)
    opener = build_opener(args.proxy)
    ext = args.output_format
    if args.n > 1:
        outputs = [p.with_suffix(f".{ext}") for p in resolve_outputs(args.out, args.n)]
    else:
        out_path = Path(args.out)
        # 单图时若扩展名与 --output-format 不一致（如 out.png + jpeg），
        # 文件内容其实是 jpeg，扩展名必须跟着改，否则部分查看器打不开。
        if out_path.suffix:
            want = {"jpeg": {"jpg", "jpeg"}}.get(ext, {ext})
            if out_path.suffix.lower().lstrip(".") not in want:
                out_path = out_path.with_suffix(f".{'jpg' if ext == 'jpeg' else ext}")
        outputs = [out_path]

    saved: list[Path] = []
    for index in range(args.n):
        print(f"[{index + 1}/{args.n}] 请求中…", file=sys.stderr)

        if args.channel == "responses":
            payload = build_responses_payload(
                prompt=args.prompt, image_model=args.model, main_model=args.main_model,
                size=args.size, quality=args.quality,
                output_format=args.output_format, images=data_urls,
            )
            body = json.dumps(payload).encode("utf-8")
            images_out = None
            for attempt in range(1, RESPONSES_ATTEMPTS + 1):
                data = post_with_auth_retry(
                    url=RESPONSES_URL, auth=auth, auth_file=auth_file,
                    accept="text/event-stream", content_type="application/json",
                    body=body, opener=opener,
                )
                try:
                    b64, meta = parse_responses_sse(data)
                except GenerationFailed as exc:
                    if attempt >= RESPONSES_ATTEMPTS:
                        fail(f"{exc}（已尝试 {attempt} 次）")
                    print(f"  响应不完整，重试 {attempt}/{RESPONSES_ATTEMPTS - 1}…", file=sys.stderr)
                    continue
                images_out = [(b64, meta)]
                break
            if images_out is None:
                fail("响应始终不完整，放弃。")
            round_outputs = [outputs[index]]
        else:
            payload = build_images_payload(
                prompt=args.prompt, image_model=args.model, size=args.size,
                quality=args.quality, output_format=args.output_format,
                n=args.n if args.n == 1 else 1,
            )
            if raw_files:
                files = [
                    ("image",
                     f"image-{i}{mimetypes.guess_extension(mime) or '.png'}",
                     mime, data)
                    for i, (mime, data, _name) in enumerate(raw_files, start=1)
                ]
                body, content_type = build_multipart(payload, files)
                url = IMAGES_EDITS_URL
            else:
                body = json.dumps(payload).encode("utf-8")
                content_type = "application/json"
                url = IMAGES_GENERATIONS_URL
            data = post_with_auth_retry(
                url=url, auth=auth, auth_file=auth_file,
                accept="application/json", content_type=content_type,
                body=body, opener=opener,
            )
            images_out = parse_images_json(data, opener)
            if len(images_out) == 1:
                round_outputs = [outputs[index]]
            else:
                # 一次返回多张：按 --out 派生 -1/-2 后缀，避免互相覆盖
                base = Path(args.out)
                suffix = base.suffix or f".{ext}"
                round_outputs = [
                    base.parent / f"{base.stem}-{i}{suffix}"
                    for i in range(1, len(images_out) + 1)
                ]

        for (b64, _meta), out_path in zip(images_out, round_outputs):
            decode_and_write(b64, out_path)
            saved.append(out_path)
            print(f"  saved {out_path} ({out_path.stat().st_size} bytes)", file=sys.stderr)

    print("\n完成：")
    for path in saved:
        print(f"  {path}")


if __name__ == "__main__":
    main()
