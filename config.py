"""
智慧眼识物相机系统 - 全局配置
=============================
集中管理: GPIO 引脚、ffmpeg 路径、OLED I2C 地址、字体路径、
图像参数、VLM API 端点、防抖冷却时间、Web 服务端口。
所有参数可被环境变量覆盖,便于在 1GB 树莓派上不修改源码即可调参。

运行时可改参数 (见 runtime.py): 增强模式、光敏策略、VLM 等。
"""

import os

# ----------------------------------------------------------------------
# 1. GPIO 引脚配置 (BCM 编码)
# ----------------------------------------------------------------------
PIN_IR = int(os.getenv("PIN_IR", "18"))       # 红外避障 DO
PIN_LIGHT = int(os.getenv("PIN_LIGHT", "27")) # 光敏电阻 DO
# OLED 使用 I2C1 (SCL=GPIO3, SDA=GPIO2), 见 oled_ui.py

# ----------------------------------------------------------------------
# 2. OLED 屏幕
# ----------------------------------------------------------------------
OLED_I2C_PORT = int(os.getenv("OLED_I2C_PORT", "1"))
OLED_I2C_ADDR = int(os.getenv("OLED_I2C_ADDR", "0x3C"), 16)
OLED_WIDTH = 128
OLED_HEIGHT = 64

# 自动探测系统内的中文字体,优先顺序如下
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
]
FONT_SIZE_SMALL = 11   # 状态屏小字
FONT_SIZE_BIG = 18     # 结果屏大字

# ----------------------------------------------------------------------
# 3. 图像工程
# ----------------------------------------------------------------------
CAMERA_DEVICE = os.getenv("CAMERA_DEVICE", "/dev/video0")
# 锁小分辨率: 1GB 树莓派上 1080p YUYV 单帧 ≈ 4MB,会触发 OOM
CAPTURE_W = int(os.getenv("CAPTURE_W", "640"))
CAPTURE_H = int(os.getenv("CAPTURE_H", "480"))
# 摄像头像素格式: mjpeg(默认,体积小) / yuyv422(兼容性最好) / 留空则由 ffmpeg 自协商
CAMERA_INPUT_FORMAT = os.getenv("CAMERA_INPUT_FORMAT", "mjpeg")
# JPEG 质量: 1(最高)~31(最低),VLM 场景 5 已足够且体积最小
CAPTURE_JPEG_QUALITY = int(os.getenv("CAPTURE_JPEG_QUALITY", "5"))
CAPTURE_PATH = os.getenv("CAPTURE_PATH", "/tmp/wise_eye_capture.jpg")

# Gamma 调亮 (仅黑夜模式使用)
NIGHT_GAMMA = float(os.getenv("NIGHT_GAMMA", "0.5"))

# ----------------------------------------------------------------------
# 4. 状态机时序
# ----------------------------------------------------------------------
CAPTURE_SETTLE_MS = 500      # 抓拍前稳定时间 (ms)
DISPLAY_HOLD_SEC = 5.0       # 结果屏展示时间 (防抖冷却)
ERROR_HOLD_SEC = 2.0         # 错误屏展示时间
NET_TIMEOUT_SEC = 20         # VLM 网络超时
MAX_API_RETRY = 2            # 失败重试次数

# ----------------------------------------------------------------------
# 5. 持久化存储 (前端图库 + 元数据)
# ----------------------------------------------------------------------
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
IMAGE_DIR = os.path.join(DATA_DIR, "images")
DB_PATH = os.path.join(DATA_DIR, "wise_eye.db")

# 清理策略: 超龄或超量自动淘汰
IMAGE_RETENTION_DAYS = int(os.getenv("IMAGE_RETENTION_DAYS", "7"))
IMAGE_RETENTION_MAX = int(os.getenv("IMAGE_RETENTION_MAX", "500"))

# ----------------------------------------------------------------------
# 6. VLM 云端接口 (默认阿里通义 Qwen-VL-Plus, OpenAI 兼容协议)
# ----------------------------------------------------------------------
# 也可换成: 智谱 GLM-4V / OpenAI gpt-4o-mini / 月之暗面 等兼容服务
VLM_API_BASE = os.getenv("VLM_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
VLM_API_KEY = os.getenv("VLM_API_KEY", "PUT-YOUR-DASHSCOPE-KEY-HERE")
VLM_MODEL = os.getenv("VLM_MODEL", "qwen-vl-plus")
VLM_TEMPERATURE = 0.0
VLM_MAX_TOKENS = 80

VLM_SYSTEM_PROMPT = """You are a structured image recognition backend for an embedded system.
Analyze the user-provided image and identify the primary object.

[STRICT OUTPUT RULES]
1. Reply ONLY with a valid JSON object.
2. Do NOT wrap the JSON in markdown code blocks.
3. Keep the content brief and precise.

[JSON SCHEMA]
{
  "object_name": "1-4 Chinese characters representing the object name",
  "category": "1-4 Chinese characters representing the category (e.g. 水果, 数码, 日用品)"
}
"""

# ----------------------------------------------------------------------
# 7. Web 服务
# ----------------------------------------------------------------------
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8081"))

# ----------------------------------------------------------------------
# 8. 日志
# ----------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_RING_SIZE = 500          # 内存环形日志条数,供 /api/logs 查询

# ----------------------------------------------------------------------
# 9. 默认运行时配置 (可被前端覆盖,见 runtime.py)
# ----------------------------------------------------------------------
DEFAULT_ENHANCEMENT_MODE = os.getenv("DEFAULT_ENHANCEMENT_MODE", "AUTO")   # AUTO/DAY/NIGHT/OFF
DEFAULT_LIGHT_POLICY = os.getenv("DEFAULT_LIGHT_POLICY", "AUTO")          # AUTO/FORCE_DAY/FORCE_NIGHT
# 光敏 DO 去抖窗口: 只有数字量输出,阈值在硬件电位器上调;这里只做多数表决防抖
DEFAULT_LIGHT_DEBOUNCE_MS = int(os.getenv("DEFAULT_LIGHT_DEBOUNCE_MS", "300"))
DEFAULT_SAVE_IMAGE = os.getenv("DEFAULT_SAVE_IMAGE", "1") == "1"
