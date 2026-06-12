"""
OLED 状态屏渲染
===============
- 自动探测系统内的中文字体
- 6 个状态屏: READY / CAPTURE / PROCESS / THINKING / DISPLAY / ERROR
- DISPLAY 屏用 ASCII 边框大写结果,其余用状态栏 + 短描述
- I2C 初始化失败时所有 show_* 静默 no-op,不阻塞主流程
"""

import os
import time
from PIL import ImageFont

import config


# ----------------------------------------------------------------------
# 1. OLED 设备 (惰性初始化,失败则降级为 no-op)
# ----------------------------------------------------------------------
_device = None
_font_small = None
_font_big = None
_hw_ok = True                      # 标记 OLED 硬件是否可用


def _find_font(size: int):
    for path in config.FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def get_device():
    global _device, _hw_ok
    if _device is not None:
        return _device
    if not _hw_ok:
        return None
    try:
        from luma.core.interface.serial import i2c
        from luma.core.render import canvas
        from luma.oled.device import ssd1306
        serial = i2c(port=config.OLED_I2C_PORT, address=config.OLED_I2C_ADDR)
        _device = ssd1306(serial, width=config.OLED_WIDTH, height=config.OLED_HEIGHT)
        # 在模块内绑定 canvas 引用供后续使用
        get_device.canvas = canvas
        import luma.core.render as luma_render
        get_device._luma_canvas = luma_render.canvas
    except Exception as e:
        _hw_ok = False
        _device = None
        print(f"[OLED] Hardware unavailable ({e}); OLED disabled.")
    return _device


def get_fonts():
    global _font_small, _font_big
    if _font_small is None:
        _font_small = _find_font(config.FONT_SIZE_SMALL)
    if _font_big is None:
        _font_big = _find_font(config.FONT_SIZE_BIG)
    return _font_small, _font_big


# ----------------------------------------------------------------------
# 2. 辅助: 安全渲染上下文
# ----------------------------------------------------------------------
def _gui(fn):
    """
    装饰器/安全包装: OLED 硬件不可用时静默跳过。
    内部自动获取 device + fonts,以 (dev, font_small, font_big) 传给 fn。
    """
    dev = get_device()
    if dev is None:
        return
    font_s, font_b = get_fonts()
    try:
        canvas_fn = get_device._luma_canvas  # type: ignore
        with canvas_fn(dev) as draw:
            fn(draw, font_s, font_b)
    except Exception:
        pass


def _draw_hr(draw, y):
    try:
        draw.line([(0, y), (config.OLED_WIDTH - 1, y)], fill=1)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 3. 六个状态屏
# ----------------------------------------------------------------------
def show_ready():
    def _draw(draw, fs, fb):
        draw.text((0, 2), "[ SYSTEM READY ]", font=fs, fill=1)
        _draw_hr(draw, 14)
        draw.text((0, 22), "Place an object", font=fs, fill=1)
        draw.text((0, 36), "near the camera...", font=fs, fill=1)
        draw.text((0, 52), time.strftime("%H:%M:%S"), font=fs, fill=1)
    _gui(_draw)


def show_capture(mode: str):
    def _draw(draw, fs, fb):
        draw.text((0, 2), "[ CAPTURING... ]", font=fs, fill=1)
        _draw_hr(draw, 14)
        draw.text((0, 22), f"Mode: [{mode}]", font=fs, fill=1)
        draw.text((0, 36), "Keep still for 0.5s", font=fs, fill=1)
        draw.text((0, 52), time.strftime("%H:%M:%S"), font=fs, fill=1)
    _gui(_draw)


def show_process():
    def _draw(draw, fs, fb):
        draw.text((0, 2), "[ PROCESSING ]", font=fs, fill=1)
        _draw_hr(draw, 14)
        draw.text((0, 22), "Enhancing image...", font=fs, fill=1)
        draw.text((0, 36), "Filtering noises...", font=fs, fill=1)
        draw.text((0, 52), time.strftime("%H:%M:%S"), font=fs, fill=1)
    _gui(_draw)


def show_thinking():
    def _draw(draw, fs, fb):
        draw.text((0, 2), "[ AI THINKING ]", font=fs, fill=1)
        _draw_hr(draw, 14)
        draw.text((0, 22), "Uploading Base64...", font=fs, fill=1)
        draw.text((0, 36), "Waiting for VLM...", font=fs, fill=1)
        draw.text((0, 52), time.strftime("%H:%M:%S"), font=fs, fill=1)
    _gui(_draw)


def show_result(category: str, name: str):
    def _draw(draw, fs, fb):
        draw.rectangle((0, 0, config.OLED_WIDTH - 1, config.OLED_HEIGHT - 1), outline=1)
        draw.line([(0, 16), (config.OLED_WIDTH - 1, 16)], fill=1)
        cat_text = f"[{category}]" if category else "[分类]"
        draw.text((4, 1), cat_text[:14], font=fs, fill=1)
        try:
            bbox = draw.textbbox((0, 0), name, font=fb)
            text_w = bbox[2] - bbox[0]
        except Exception:
            text_w = len(name) * config.FONT_SIZE_BIG
        x = max(2, (config.OLED_WIDTH - text_w) // 2)
        draw.text((x, 24), name, font=fb, fill=1)
    _gui(_draw)


def show_error(msg: str):
    def _draw(draw, fs, fb):
        draw.text((0, 2), "[ SYSTEM ERROR ]", font=fs, fill=1)
        _draw_hr(draw, 14)
        draw.text((0, 22), f"Err: {msg[:16]}", font=fs, fill=1)
        draw.text((0, 36), "Retrying soon...", font=fs, fill=1)
        draw.text((0, 52), time.strftime("%H:%M:%S"), font=fs, fill=1)
    _gui(_draw)


# ----------------------------------------------------------------------
# 4. 自检
# ----------------------------------------------------------------------
def self_test():
    def _draw(draw, fs, fb):
        draw.ellipse((100, 42, 120, 62), outline=1)
        draw.ellipse((105, 47, 109, 51), fill=1)
        draw.ellipse((111, 47, 115, 51), fill=1)
        draw.arc((105, 50, 115, 58), 0, 180, fill=1)
        draw.text((0, 24), "Wise Eye", font=fs, fill=1)
    _gui(_draw)
