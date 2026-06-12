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


# ----------------------------------------------------------------------
# 1. ffmpeg 抓拍
# ----------------------------------------------------------------------
def capture_frame(out_path: str = config.CAPTURE_PATH) -> str:
    """
    通过 ffmpeg 从 UVC 摄像头抓一帧 640x480 的 JPEG。
    关键点(针对 1GB 树莓派):
      - 锁定 -video_size 640x480: 避免摄像头默认 1080p YUYV(单帧 4MB+) 导致 OOM
      - -input_format mjpeg 可选: 部分 UVC 不暴露 MJPEG,设为空则自协商
      - -q:v 5: JPEG 质量压低,单帧约 50KB,上传带宽/内存最友好
      - -update 1: 单文件覆写,防 image2 序列模式
      - -an -fflags nobuffer -hide_banner -loglevel error: 静默、低延迟、无音轨
    """
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    cmd = [
        "ffmpeg",
        "-hide_banner",           # 不打印版本横幅
        "-loglevel", "error",     # 只输出错误
        "-y",                     # 覆盖输出
        "-f", "v4l2",
        "-video_size", f"{config.CAPTURE_W}x{config.CAPTURE_H}",
        "-fflags", "nobuffer",    # 降低缓冲延迟
        "-an",                    # 不要音频流
    ]
    # 摄像头像素格式: mjpeg(默认,体积小) / yuyv422(兼容) / 留空则自协商
    if config.CAMERA_INPUT_FORMAT:
        cmd += ["-input_format", config.CAMERA_INPUT_FORMAT]

    cmd += [
        "-i", config.CAMERA_DEVICE,
        "-frames:v", "1",
        "-q:v", str(config.CAPTURE_JPEG_QUALITY),
        "-update", "1",           # 单文件覆写,防 image2 序列模式
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
