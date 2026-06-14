"""
用户身份画像 (Identity.md)
========================
每次 VLM 识别成功后,将 scene 场景信息累积到 Identity.md,
后续 VLM 请求时注入该上下文,使识别更贴近用户实际环境。

Identity.md 结构:
  - 文件头: 用户画像摘要 (由系统自动推断)
  - ## 识别记录: 最近 50 条带场景的识别历史
"""

import os
import time

import config


IDENTITY_HEADER = """# 用户身份画像

> *自动由智慧眼相机维护,每次识别后更新。将识别到的场景信息累积,
> 逐步建立对用户环境与习惯的理解,使后续识别更准确、更贴近实际使用场景。*

**最后更新**: {ts}

**场景记录**: {count} 条

**当前画像摘要**:
{summary}

"""


def load() -> str:
    """读取 Identity.md 全部内容,用于注入 VLM prompt。"""
    try:
        with open(config.IDENTITY_PATH, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _build_summary(scenes: list[str]) -> str:
    """从场景记录中提取高频词,生成一句话画像。"""
    if not scenes:
        return "尚未建立用户画像。"
    from collections import Counter
    common = Counter(scenes).most_common(3)
    parts = [f"  - 常见场景: {s}" for s, _ in common]
    return "\n".join(parts)


def append(scene: str, obj_name: str, category: str, description: str):
    """追加一条识别记录到 Identity.md。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    scenes = []
    records = []

    os.makedirs(os.path.dirname(config.IDENTITY_PATH), exist_ok=True)
    try:
        with open(config.IDENTITY_PATH, "r") as f:
            content = f.read()
        parts = content.split("## 识别记录\n", 1)
        if len(parts) == 2:
            for line in parts[1].strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("- `"):
                    records.append(line)
    except FileNotFoundError:
        content = ""

    entry = f"- `{now}` **{obj_name}** ({category}) — {description}  `场景: {scene}`"
    records.insert(0, entry)
    records = records[:50]

    for r in records:
        if "场景:" in r:
            s = r.split("场景:")[-1].rstrip("`").strip()
            if s:
                scenes.append(s)

    summary = _build_summary(scenes)
    header = IDENTITY_HEADER.format(ts=ts, count=len(records), summary=summary)

    with open(config.IDENTITY_PATH, "w") as f:
        f.write(header)
        f.write("## 识别记录\n\n")
        for r in records:
            f.write(r + "\n")

    os.makedirs(os.path.dirname(config.IDENTITY_PATH), exist_ok=True)
