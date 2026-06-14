"""
运行时共享状态
==============
进程内线程安全的全局对象,供 main (主线程) 与 web_server (后台线程) 共享。

包含两类数据:
  1. RuntimeConfig: 可被前端热更新的策略 (光敏/增强/保存/VLM 凭据等)
  2. SystemStatus: 主循环写入的当前状态 (状态机/最近一次结果/计数器)

所有访问均通过锁保护,避免读到撕裂值。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, Deque, List, Dict, Any

import config


# ----------------------------------------------------------------------
# 1. 运行时配置 (可被前端热改)
# ----------------------------------------------------------------------
@dataclass
class RuntimeConfig:
    light_policy: str = config.DEFAULT_LIGHT_POLICY          # AUTO/FORCE_DAY/FORCE_NIGHT
    light_debounce_ms: int = config.DEFAULT_LIGHT_DEBOUNCE_MS  # DO 去抖窗口(ms)
    enhancement_mode: str = config.DEFAULT_ENHANCEMENT_MODE  # AUTO/DAY/NIGHT/OFF
    save_image: bool = config.DEFAULT_SAVE_IMAGE
    # 拍摄参数 (前端可热改,立即生效)
    capture_width: int = config.CAPTURE_W
    capture_height: int = config.CAPTURE_H
    capture_input_format: str = config.CAMERA_INPUT_FORMAT
    capture_jpeg_quality: int = config.CAPTURE_JPEG_QUALITY
    # VLM
    vlm_api_base: str = config.VLM_API_BASE
    vlm_api_key: str = config.VLM_API_KEY
    vlm_model: str = config.VLM_MODEL


# ----------------------------------------------------------------------
# 2. 系统状态 (主循环写,前端读)
# ----------------------------------------------------------------------
@dataclass
class SystemStatus:
    state: str = "INIT"                  # READY/CAPTURE/PROCESS/THINKING/DISPLAY/ERROR
    mode: str = "DAY"                    # 最近的 DAY/NIGHT
    last_object_name: str = ""
    last_category: str = ""
    last_description: str = ""           # 生动描述 (来自 VLM)
    last_image_id: Optional[str] = None
    last_latency_ms: int = 0
    last_update: float = 0.0
    error_msg: str = ""
    capture_total: int = 0
    capture_success: int = 0
    capture_error: int = 0
    # 最近 N 条日志行 (level + msg + time)
    logs: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=config.LOG_RING_SIZE))
    # 图像缓冲区 (批处理队列,list of dict)
    image_buffer: List[Dict[str, Any]] = field(default_factory=list)


# ----------------------------------------------------------------------
# 3. 访问入口
# ----------------------------------------------------------------------
class _Hub:
    def __init__(self):
        self._lock = threading.RLock()
        self.cfg = RuntimeConfig()
        self.status = SystemStatus()
        # 手动触发信号 (前端 POST /api/trigger 时置位)
        self.manual_trigger = threading.Event()
        # 停止信号
        self.stop = threading.Event()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            s = self.status
            return {
                "state": s.state,
                "mode": s.mode,
                "last_object_name": s.last_object_name,
                "last_category": s.last_category,
                "last_description": s.last_description,
                "last_image_id": s.last_image_id,
                "last_latency_ms": s.last_latency_ms,
                "last_update": s.last_update,
                "error_msg": s.error_msg,
                "capture_total": s.capture_total,
                "capture_success": s.capture_success,
                "capture_error": s.capture_error,
                "buffer_len": len(s.image_buffer),
            }

    def update_status(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.status, k):
                    setattr(self.status, k, v)
            self.status.last_update = time.time()

    def push_log(self, level: str, msg: str):
        with self._lock:
            self.status.logs.append(
                {"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
            )

    def tail_logs(self, n: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.status.logs)[-n:]

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return asdict(self.cfg)

    def patch_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k, v in data.items():
                if hasattr(self.cfg, k):
                    # 类型安全: bool/int/str 三种
                    cur = getattr(self.cfg, k)
                    if isinstance(cur, bool):
                        v = bool(v) if isinstance(v, (bool, int, str)) else cur
                        v = v in (True, 1, "1", "true", "True", "yes")
                    elif isinstance(cur, int):
                        try:
                            v = int(v)
                        except (TypeError, ValueError):
                            continue
                    else:
                        v = str(v) if v is not None else cur
                    setattr(self.cfg, k, v)
            return asdict(self.cfg)

    # ----- 缓冲区队列管理 -----
    def buffer_push(self, jpeg_bytes: bytes, mode: str, light_raw: int):
        thumb = _make_thumb(jpeg_bytes)
        with self._lock:
            q = self.status.image_buffer
            q.append({"bytes": jpeg_bytes, "mode": mode, "light_raw": light_raw,
                       "ts": time.time(), "thumb": thumb})
            if len(q) > config.BUFFER_MAX_SIZE:
                q.pop(0)

    def buffer_pop(self, count: int = 1) -> List[Dict[str, Any]]:
        with self._lock:
            q = self.status.image_buffer
            items = q[:count]
            q[:count] = []
            return items

    def buffer_snapshot(self, max_items: int = 5) -> List[Dict[str, Any]]:
        """返回缓冲区缩略信息（不含原始字节）,供前端展示。"""
        with self._lock:
            return [
                {"mode": it["mode"], "light_raw": it["light_raw"],
                 "ts": it["ts"], "thumb": it.get("thumb")}
                for it in list(self.status.image_buffer)[:max_items]
            ]

    def buffer_peek(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.status.image_buffer)

    def buffer_len(self) -> int:
        with self._lock:
            return len(self.status.image_buffer)


def _make_thumb(jpeg_bytes: bytes, max_width: int = 160) -> Optional[str]:
    """快速生成缩略图 base64 data URL,失败返回 None。"""
    try:
        from io import BytesIO
        from PIL import Image
        import base64
        with Image.open(BytesIO(jpeg_bytes)) as img:
            w = min(max_width, img.width)
            h = int(img.height * w / img.width)
            img.thumbnail((w, h), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, "JPEG", quality=60)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


hub = _Hub()


# ----------------------------------------------------------------------
# 4. 日志拦截: 把 logging 的输出同时塞进 hub
# ----------------------------------------------------------------------
class _HubHandler:
    def __init__(self, sink):
        self._sink = sink

    def write(self, record):
        try:
            msg = record.getMessage()
            self._sink.push_log(record.levelname, msg)
        except Exception:
            pass

    def flush(self):
        pass


def install_logging_handler():
    import logging
    h = logging.Handler()
    h.emit = _HubHandler(hub).write
    logging.getLogger().addHandler(h)
