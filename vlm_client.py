"""
VLM 云端接口客户端
=================
- 协议: OpenAI Chat Completions 兼容 (兼容智谱 / 通义 / 月之暗面 / OpenAI 等)
- 图像以 data URL (Base64) 形式塞进 user 消息的 image_url 字段
- 强制 JSON 输出: 顶层 response_format={"type": "json_object"} + system 提示
- 内置重试、超时、Markdown 代码块剥离、字段缺失兜底
"""

import base64
import json
import re
import time
from typing import Tuple

import requests

import config
import runtime


# ----------------------------------------------------------------------
# 1. 工具
# ----------------------------------------------------------------------
def _b64_jpeg(jpeg_bytes: bytes) -> str:
    return base64.b64encode(jpeg_bytes).decode("ascii")


def _strip_md_codeblock(text: str) -> str:
    """
    某些模型不严格遵守 'no markdown' 规则,会返回 ```json ... ``` 。
    这里用正则剥掉外层代码块围栏,只留原始 JSON 文本。
    """
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _truncate_cn(s: str, n: int) -> str:
    """保留前 n 个字符,中文按字符计数(等价于 substr 行为)。"""
    if s is None:
        return ""
    return str(s)[:n]


# ----------------------------------------------------------------------
# 2. 主调用
# ----------------------------------------------------------------------
def recognize(jpeg_bytes: bytes) -> Tuple[str, str]:
    """
    输入: 增强后的 JPEG 字节
    输出: (object_name, category) 两个字符串
    抛出: RuntimeError 表示失败(由 main 捕获进入 ERROR 屏)
    """
    if not jpeg_bytes:
        raise RuntimeError("empty image bytes")

    b64 = _b64_jpeg(jpeg_bytes)
    data_url = f"data:image/jpeg;base64,{b64}"

    # 关键: API 凭据 / 模型名每次调用都从 runtime 读,使前端热改立刻生效
    cfg = runtime.hub.cfg
    payload = {
        "model": cfg.vlm_model,
        "temperature": config.VLM_TEMPERATURE,
        "max_tokens": config.VLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": config.VLM_SYSTEM_PROMPT},
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
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=config.NET_TIMEOUT_SEC,
            )
            if resp.status_code != 200:
                snippet = resp.text[:200]
                raise RuntimeError(f"HTTP {resp.status_code}: {snippet}")

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = _strip_md_codeblock(content)

            obj = json.loads(content)
            obj_name = _truncate_cn(obj.get("object_name", "未知"), 4)
            category = _truncate_cn(obj.get("category", "其他"), 4)
            if not obj_name:
                obj_name = "未知"
            return obj_name, category

        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            # 短暂退避后重试
            time.sleep(0.6 * attempt)

    raise RuntimeError(f"VLM failed after {config.MAX_API_RETRY} tries: {last_err}")
