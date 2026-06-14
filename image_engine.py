"""
图像工程
========
- ffmpeg 抓拍 UVC 单帧 JPEG
- Pillow 自适应增强: 白天(高斯降噪) / 黑夜(高斯 + Gamma 调亮)
- 处理结果覆盖回原文件,即用即用,避免内存堆积
- 不引入 OpenCV,严控 1GB 树莓派内存峰值
"""

import os
import subprocess

from PIL import Image, ImageFilter

import config
import runtime


# ----------------------------------------------------------------------
# 1. ffmpeg 抓拍
# ----------------------------------------------------------------------
def capture_frame(out_path: str = config.CAPTURE_PATH) -> str:
    """
    通过 ffmpeg 从 UVC 摄像头抓一帧 JPEG。

    默认命令等价于 (用户确认的):
      ffmpeg -hide_banner -loglevel error -y -f v4l2
             -i /dev/video0 -frames:v 1 -update 1 <out_path>

    可选参数 (通过环境变量启用):
      - CAPTURE_W/H > 0    -> 追加 -video_size WxH
      - CAMERA_INPUT_FORMAT -> 追加 -input_format <fmt>
      - CAPTURE_JPEG_QUALITY > 0 -> 追加 -q:v <n>
    """
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "v4l2",
        "-fflags", "nobuffer",
        "-an",
    ]
    # 从运行时读取拍摄参数 (前端热改立即生效)
    cp = runtime.hub.cfg
    if cp.capture_width > 0 and cp.capture_height > 0:
        cmd += ["-video_size", f"{cp.capture_width}x{cp.capture_height}"]
    if cp.capture_input_format:
        cmd += ["-input_format", cp.capture_input_format]

    cmd += [
        "-i", config.CAMERA_DEVICE,
        "-frames:v", "1",
    ]
    if cp.capture_jpeg_quality > 0:
        cmd += ["-q:v", str(cp.capture_jpeg_quality)]
    cmd += [
        "-update", "1",
        out_path,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if result.returncode != 0 or not os.path.exists(out_path):
        err = result.stderr.decode("utf-8", errors="ignore")[-200:]
        raise RuntimeError(f"ffmpeg capture failed: {err}")
    return out_path


# ----------------------------------------------------------------------
# 2. 图像增强
# ----------------------------------------------------------------------
def _build_gamma_lut(gamma: float):
    """生成 256 长度的 gamma 查表 (uint8),只算一次常驻。"""
    return [int(((i / 255.0) ** gamma) * 255 + 0.5) for i in range(256)]


_GAMMA_LUT_NIGHT = _build_gamma_lut(config.NIGHT_GAMMA)


def enhance(path: str, mode: str) -> str:
    """
    加载图片 -> 按 mode 增强 -> 覆盖落盘。
    mode: 'DAY' / 'NIGHT' / 'OFF'
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        if mode != "OFF":
            img = img.filter(ImageFilter.GaussianBlur(radius=1))
            if mode == "NIGHT":
                img = img.point(_GAMMA_LUT_NIGHT * 3)
        img.save(path, format="JPEG", quality=80, optimize=False)
    return path


def enhance_from_bytes(jpeg_bytes: bytes, mode: str) -> bytes:
    """
    不落盘的纯内存版本。
    mode: 'DAY' / 'NIGHT' / 'OFF'
    OFF 模式仅转 RGB + 重编码 JPEG,跳过一切图像处理。
    """
    from io import BytesIO
    with Image.open(BytesIO(jpeg_bytes)) as img:
        img = img.convert("RGB")
        if mode != "OFF":
            img = img.filter(ImageFilter.GaussianBlur(radius=1))
            if mode == "NIGHT":
                img = img.point(_GAMMA_LUT_NIGHT * 3)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=False)
    return buf.getvalue()
