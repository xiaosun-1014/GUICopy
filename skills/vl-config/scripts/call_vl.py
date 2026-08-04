#!/usr/bin/env python3
"""通用 VL 模型调用脚本。

从 vl_config.json 读取端点/模型配置，
从环境变量 VL_API_KEY 读取 API key，
支持文本对话和图片理解（多模态）。

用法：
  # 按 task 类型调用（自动拼接 system prompt）
  python call_vl.py --task sequence_list --input vl_input.json

  # 自定义 prompt + 可选图片
  python call_vl.py --prompt "分析这张图" --image screenshot.png

  # 从 stdin 读取输入
  cat vl_input.json | python call_vl.py --task series_extract --image shot.png

环境变量：
  VL_API_KEY          API key（必需）
  VL_API_BASE_URL     覆盖 vl_config.json 中的 base_url（可选）
  VL_MODEL            覆盖 vl_config.json 中的 default_model（可选）
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ── 加载 .env 文件（项目根） ──
try:
    from dotenv import load_dotenv
    from pathlib import Path
    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装，仅依赖系统环境变量


def load_config() -> dict:
    """加载 vl_config.json"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", "vl_config.json")
    # 也允许从 cwd 或 VL_CONFIG_PATH 环境变量加载
    env_path = os.environ.get("VL_CONFIG_PATH")
    if env_path:
        config_path = env_path
    elif not os.path.exists(config_path):
        config_path = "vl_config.json"  # fallback to cwd

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def encode_image(image_path: str) -> str:
    """读取图片并返回 base64 字符串"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_messages(
    config: dict,
    task: str | None,
    prompt: str | None,
    input_data: dict | None,
    image_path: str | None,
) -> tuple[list[dict], str]:
    """构建 messages 列表，返回 (messages, 使用的 model)。"""
    # 确定 model
    model = os.environ.get("VL_MODEL") or os.environ.get("LLM_MODEL") or config.get("default_model", "gpt-4o")
    if task and task in config.get("tasks", {}):
        model = config["tasks"][task].get("model", model)

    # 构建 system + user message
    system_content = ""
    user_content = ""

    if task and task in config.get("tasks", {}):
        task_cfg = config["tasks"][task]
        system_content = task_cfg.get("system_prompt", "")

    if prompt:
        user_content = prompt

    # 如果有 input_data，附加到 user message
    if input_data:
        extra = json.dumps(input_data, ensure_ascii=False, indent=2)
        if user_content:
            user_content += f"\n\n## 输入数据\n{extra}"
        else:
            user_content = extra

    # 构建 messages
    messages = []
    if system_content:
        messages.append({"role": "system", "content": system_content})

    if image_path:
        # 多模态消息
        b64 = encode_image(image_path)
        img_type = "png" if image_path.lower().endswith(".png") else "jpeg"
        content_parts = []
        if user_content:
            content_parts.append({"type": "text", "text": user_content})
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/{img_type};base64,{b64}",
                "detail": "high",
            },
        })
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_content or "请分析"})

    return messages, model


def call_api(config: dict, messages: list[dict], model: str) -> dict:
    """调用 OpenAI 兼容的 chat/completions API。"""
    api_key = os.environ.get("VL_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        print("[VL] ❌ 环境变量 VL_API_KEY / LLM_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    base_url = (
        os.environ.get("VL_BASE_URL")
        or os.environ.get("VL_API_BASE_URL")
        or config.get("base_url", "https://api.openai.com/v1")
    )
    endpoint = config.get("endpoints", {}).get("chat", "/chat/completions")
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")

    timeout = config.get("http_timeout", 120)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode("utf-8"))
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[VL] ❌ HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[VL] ❌ 请求失败: {e}", file=sys.stderr)
        sys.exit(1)


def parse_response(api_response: dict) -> str:
    """从 API 响应中提取文本内容。"""
    try:
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        print(f"[VL] ❌ 无法解析响应: {json.dumps(api_response, ensure_ascii=False)[:500]}",
              file=sys.stderr)
        sys.exit(1)


def extract_json(text: str) -> dict | list:
    """从 LLM 回复中提取 JSON（处理 ```json 围栏）。"""
    import re
    # 尝试直接 parse
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试从 markdown 围栏中提取
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试提取第一个 { 或 [
    for delim in ("{", "["):
        start = text.find(delim)
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                continue
    print(f"[VL] ⚠ 无法从回复中提取 JSON:\n{text[:500]}", file=sys.stderr)
    return {"raw": text}


def main():
    parser = argparse.ArgumentParser(description="通用 VL 模型调用")
    parser.add_argument("--task", help="任务类型（对应 vl_config.json 中的 tasks 键）")
    parser.add_argument("--prompt", help="自定义 prompt（与 --task 互斥补充）")
    parser.add_argument("--input", help="输入 JSON 文件路径")
    parser.add_argument("--image", help="图片文件路径（多模态）")
    parser.add_argument("--output", help="输出文件路径（默认 stdout）")
    parser.add_argument("--output-raw", action="store_true",
                        help="输出原始 LLM 文本而非解析后的 JSON")
    args = parser.parse_args()

    # 加载配置
    config = load_config()

    # 读取 input 数据
    input_data = None
    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            input_data = json.load(f)
    elif not sys.stdin.isatty():
        try:
            input_data = json.loads(sys.stdin.read())
        except (json.JSONDecodeError, Exception):
            pass

    # 如果是 file-interaction 模式，尝试从 vl_input_*.json 自动读取输入
    if not input_data and not args.input and not args.prompt:
        import glob
        input_files = sorted(glob.glob("vl_input_*.json"), reverse=True)
        if input_files:
            with open(input_files[0], "r", encoding="utf-8") as f:
                input_data = json.load(f)
            print(f"[VL] 自动读取输入: {input_files[0]}", file=sys.stderr)

    # 构建消息
    messages, model = build_messages(config, args.task, args.prompt, input_data, args.image)

    # 调用 API
    print(f"[VL] 调用 {model} ...", file=sys.stderr)
    start = time.monotonic()
    api_resp = call_api(config, messages, model)
    elapsed = time.monotonic() - start

    # 解析结果
    text = parse_response(api_resp)
    usage = api_resp.get("usage", {})
    print(f"[VL] ✓ 完成 ({elapsed:.1f}s, "
          f"输入{usage.get('prompt_tokens', '?')}tok "
          f"输出{usage.get('completion_tokens', '?')}tok)",
          file=sys.stderr)

    # 输出
    if args.output_raw:
        output = text
    else:
        output = extract_json(text)

    output_str = json.dumps(output, ensure_ascii=False, indent=2) if not isinstance(output, str) else output

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_str)
        print(f"[VL] 输出已保存 → {args.output}", file=sys.stderr)
    else:
        print(output_str)


if __name__ == "__main__":
    main()
