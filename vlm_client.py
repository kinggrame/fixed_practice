"""
VLM 云端接口客户端
=================
- 协议: OpenAI Chat Completions 兼容
- 图像以 data URL (Base64) 形式塞进 user 消息的 image_url 字段
- 强制 JSON 输出 + 注入 Identity.md 上下文
- 内置重试、超时、Markdown 代码块剥离、字段缺失兜底
"""

import base64
import json
import re
import time
from typing import Tuple, List

import requests

import config
import identity
import runtime


# ----------------------------------------------------------------------
# 1. 工具
# ----------------------------------------------------------------------
def _b64_jpeg(jpeg_bytes: bytes) -> str:
    return base64.b64encode(jpeg_bytes).decode("ascii")


def _strip_md_codeblock(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _truncate_cn(s: str, n: int) -> str:
    if s is None:
        return ""
    return str(s)[:n]


def _inject_identity(prompt: str) -> str:
    ctx = identity.load()
    if ctx:
        prompt += f"\n\n[用户环境上下文]\n{ctx}\n"
    return prompt


def _build_single_prompt() -> str:
    return _inject_identity(config.VLM_SYSTEM_PROMPT)


def _build_batch_prompt() -> str:
    return _inject_identity(config.VLM_BATCH_PROMPT)


# ----------------------------------------------------------------------
# 2. 主调用
# ----------------------------------------------------------------------
def recognize(jpeg_bytes: bytes) -> Tuple[str, str, str, str]:
    if not jpeg_bytes:
        raise RuntimeError("empty image bytes")
    b64 = _b64_jpeg(jpeg_bytes)
    data_url = f"data:image/jpeg;base64,{b64}"

    cfg = runtime.hub.cfg
    payload = {
        "model": cfg.vlm_model,
        "temperature": config.VLM_TEMPERATURE,
        "max_tokens": config.VLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _build_single_prompt()},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "请识别图中主要物体,按 JSON Schema 输出。"},
                ],
            },
        ],
    }

    url = cfg.vlm_api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.vlm_api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1, config.MAX_API_RETRY + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=config.NET_TIMEOUT_SEC)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = _strip_md_codeblock(content)
            obj = json.loads(content)
            obj_name = _truncate_cn(obj.get("object_name", "未知"), 6)
            category = _truncate_cn(obj.get("category", "其他"), 4)
            description = _truncate_cn(obj.get("description", ""), 50)
            scene = _truncate_cn(obj.get("scene", ""), 10)
            if not obj_name:
                obj_name = "未知"
            return obj_name, category, description, scene
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.6 * attempt)

    raise RuntimeError(f"VLM failed after {config.MAX_API_RETRY} tries: {last_err}")


# ----------------------------------------------------------------------
# 3. 多图批处理
# ----------------------------------------------------------------------
def recognize_batch(jpegs: List[bytes]) -> List[Tuple[str, str, str, str]]:
    if not jpegs:
        raise RuntimeError("empty batch")
    if len(jpegs) == 1:
        obj_name, category, desc, scene = recognize(jpegs[0])
        return [(obj_name, category, desc, scene)]

    content = []
    for jpg in jpegs:
        b64 = _b64_jpeg(jpg)
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text",
                    "text": f"这里有 {len(jpegs)} 张图片,请识别每张图中的主要物体。"})

    cfg = runtime.hub.cfg
    payload = {
        "model": cfg.vlm_model,
        "temperature": config.VLM_TEMPERATURE,
        "max_tokens": config.VLM_MAX_TOKENS + 50,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _build_batch_prompt()},
            {"role": "user", "content": content},
        ],
    }

    url = cfg.vlm_api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.vlm_api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1, config.MAX_API_RETRY + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=config.NET_TIMEOUT_SEC)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            raw = _strip_md_codeblock(raw)
            root = json.loads(raw)
            results = root.get("results") or root.get("objects") or []
            if not isinstance(results, list):
                raise ValueError(f"expected array, got {type(results)}")
            out = []
            for item in results:
                name = _truncate_cn(item.get("object_name", "未知"), 6)
                cat = _truncate_cn(item.get("category", "其他"), 4)
                desc = _truncate_cn(item.get("description", ""), 50)
                scene = _truncate_cn(item.get("scene", ""), 10)
                out.append((name, cat, desc, scene))
            if not out:
                raise ValueError("empty results array")
            return out
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.6 * attempt)

    raise RuntimeError(f"batch VLM failed after {config.MAX_API_RETRY} tries: {last_err}")
