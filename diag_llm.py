"""诊断 call_llm 返回结构 — 看 message.content / reasoning_content / choices 实际是什么。"""
from __future__ import annotations
import json
import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 autocomplete.py 里的常量
from autocomplete import DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL


def main():
    if not DEFAULT_API_KEY:
        print("⚠ VL_API_KEY 没设")
        return

    from openai import OpenAI
    client = OpenAI(api_key=DEFAULT_API_KEY, base_url=DEFAULT_BASE_URL)

    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": "你是一个 Python 脚本生成器。只输出代码。"},
            {"role": "user", "content": "输出 print('hello world') 一行代码。"},
        ],
        temperature=0.2,
        max_tokens=512,
    )

    print("=" * 60)
    print("完整响应（model_dump）:")
    print("=" * 60)
    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))

    print("=" * 60)
    print("字段检查:")
    print("=" * 60)
    print(f"  choices 长度: {len(resp.choices)}")
    if resp.choices:
        msg = resp.choices[0].message
        print(f"  message.content 类型: {type(msg.content).__name__}, 值: {repr(msg.content)[:200]}")
        print(f"  message.reasoning_content 类型: {type(getattr(msg, 'reasoning_content', None)).__name__}, "
              f"值: {repr(getattr(msg, 'reasoning_content', None))[:200]}")
        print(f"  finish_reason: {resp.choices[0].finish_reason}")
        print(f"  message 所有字段: {[f for f in dir(msg) if not f.startswith('_')]}")


if __name__ == "__main__":
    main()
