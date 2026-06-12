"""
智慧眼识物相机 - 主状态机
========================
事件驱动型端云协同,六大状态循环:

  READY  ->  CAPTURE  ->  PROCESS  ->  THINKING  ->  DISPLAY
    ^                                                       |
    |________________________  5s 防抖冷却 __________________|

  任意阶段异常  ->  ERROR  ->  (2s)  ->  READY

运行期所有策略 (光敏/增强/保存/VLM) 全部从 runtime.hub.cfg 读取,
前端修改立即生效,无需重启。

硬件:
  - 红外避障: BCM 18, 下降沿触发 (物体靠近)
  - 光敏电阻: BCM 27, 持续电平读取 (0=Day, 1=Night)
  - OLED:     I2C1, 0x3C
  - 摄像头:   /dev/video0 (UVC)
"""

import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from typing import Optional

import config
import image_engine
import image_store
import oled_ui
import runtime
import vlm_client


# ----------------------------------------------------------------------
# 1. 日志
# ----------------------------------------------------------------------
log = logging.getLogger("wise_eye")


def _setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    runtime.install_logging_handler()


# ----------------------------------------------------------------------
# 2. GPIO 抽象
# ----------------------------------------------------------------------
class GPIOBackend:
    BCM = "BCM"

    def __init__(self):
        self._ir_event = threading.Event()
        self._light_history = deque(maxlen=10)  # 用于去抖
        self._real = False
        self._setup()

    def _setup(self):
        try:
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(config.PIN_IR, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(config.PIN_LIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(
                config.PIN_IR,
                GPIO.FALLING,
                callback=lambda ch: self._ir_event.set(),
                bouncetime=200,
            )
            self._gpio = GPIO
            self._real = True
            log.info("GPIO ready on BCM %s (IR), %s (LIGHT).", config.PIN_IR, config.PIN_LIGHT)
        except Exception as e:
            log.warning("RPi.GPIO unavailable (%s); running in PC simulation.", e)
            self._gpio = None

    def wait_ir(self, timeout: float) -> bool:
        return self._ir_event.wait(timeout=timeout)

    def clear_ir(self):
        self._ir_event.clear()

    def read_ir(self) -> int:
        if self._real:
            return self._gpio.input(config.PIN_IR)
        return 1

    def read_light(self) -> int:
        """原始电平读取。"""
        if self._real:
            return self._gpio.input(config.PIN_LIGHT)
        return 0  # 仿真默认白天

    def read_light_debounced(self) -> int:
        """带去抖的稳定光敏读数。
        在 debounce_ms 窗口内多数表决,避免临界值抖动。
        """
        debounce_ms = runtime.hub.cfg.light_debounce_ms
        window = max(1, debounce_ms // 50)
        if len(self._light_history) >= window:
            self._light_history.popleft()
        self._light_history.append(self.read_light())
        # 多数表决
        return 1 if sum(self._light_history) > len(self._light_history) / 2 else 0

    def cleanup(self):
        if self._real:
            try:
                self._gpio.cleanup()
            except Exception:
                pass


# ----------------------------------------------------------------------
# 3. 决策层 (把 runtime 配置翻译成"模式")
# ----------------------------------------------------------------------
def resolve_mode(light_raw: int) -> str:
    """根据 light_policy 决策 DAY / NIGHT。"""
    policy = runtime.hub.cfg.light_policy
    if policy == "FORCE_DAY":
        return "DAY"
    if policy == "FORCE_NIGHT":
        return "NIGHT"
    # AUTO: 0=Day, 1=Night
    return "NIGHT" if light_raw == 1 else "DAY"


def resolve_enhancement(mode: str) -> str:
    """根据 enhancement_mode 决定本次实际应用的增强档位。
    增强是子动作,mode 是当前环境;用户可强制某档或关闭。
    """
    em = runtime.hub.cfg.enhancement_mode
    if em == "OFF":
        return "OFF"
    if em == "DAY":
        return "DAY"
    if em == "NIGHT":
        return "NIGHT"
    # AUTO
    return mode


def apply_enhancement(jpeg_bytes: bytes, mode: str) -> bytes:
    """实际应用增强,OFF 模式直接原图。"""
    if mode == "OFF":
        return image_engine.enhance_from_bytes(jpeg_bytes, "OFF")
    return image_engine.enhance_from_bytes(jpeg_bytes, mode)


# ----------------------------------------------------------------------
# 4. 状态机
# ----------------------------------------------------------------------
class State:
    READY = "READY"
    CAPTURE = "CAPTURE"
    PROCESS = "PROCESS"
    THINKING = "THINKING"
    DISPLAY = "DISPLAY"
    ERROR = "ERROR"


class WiseEyeStateMachine:
    def __init__(self):
        self.gpio = GPIOBackend()
        self.state = State.READY
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        self._last_ready_tick = 0.0
        self._cooldown_until = 0.0  # 时间戳,READY 中需等待此值

    def _on_signal(self, signum, frame):
        log.info("Signal %s, shutting down.", signum)
        runtime.hub.stop.set()

    # ----- 单次完整流程 -----
    def run_once(self, source: str = "IR") -> None:
        """
        完整跑一次: 测光 -> 抓拍 -> 增强 -> (可选保存) -> VLM -> 展示
        source: "IR" / "MANUAL" 仅用于日志
        """
        # 等待上一次的冷却
        now = time.time()
        if now < self._cooldown_until:
            time.sleep(self._cooldown_until - now)

        # ========== 2. CAPTURE ==========
        self.state = State.CAPTURE
        light_raw = self.gpio.read_light_debounced()
        mode = resolve_mode(light_raw)
        oled_ui.show_capture(mode)
        log.info("CAPTURE[%s] light_raw=%s mode=%s", source, light_raw, mode)
        runtime.hub.update_status(state=State.CAPTURE, mode=mode)
        time.sleep(config.CAPTURE_SETTLE_MS / 1000.0)

        # ========== 3. PROCESS ==========
        self.state = State.PROCESS
        oled_ui.show_process()
        runtime.hub.update_status(state=State.PROCESS)
        try:
            jpeg_path = image_engine.capture_frame()
            with open(jpeg_path, "rb") as f:
                raw = f.read()
            try:
                os.remove(jpeg_path)
            except OSError:
                pass
            enh_mode = resolve_enhancement(mode)
            enhanced = apply_enhancement(raw, enh_mode)
            del raw
        except Exception as e:
            log.exception("Process failed")
            self._go_error(f"Cam:{e.__class__.__name__}")
            return

        # ========== 4. THINKING (保存 + VLM) ==========
        self.state = State.THINKING
        oled_ui.show_thinking()
        runtime.hub.update_status(state=State.THINKING)

        img_id = None
        if runtime.hub.cfg.save_image:
            try:
                img_id = image_store.save(enhanced, mode)
            except Exception as e:
                log.warning("image_store.save failed: %s", e)
        runtime.hub.update_status(last_image_id=img_id)

        t0 = time.time()
        try:
            obj_name, category = vlm_client.recognize(enhanced)
            latency_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            log.exception("VLM failed")
            if img_id:
                image_store.mark_error(img_id, str(e))
            self._go_error(f"VLM:{e.__class__.__name__}")
            return
        finally:
            del enhanced

        if img_id:
            image_store.update_result(img_id, obj_name, category, latency_ms)

        # ========== 5. DISPLAY ==========
        self.state = State.DISPLAY
        oled_ui.show_result(category, obj_name)
        runtime.hub.update_status(
            state=State.DISPLAY,
            last_object_name=obj_name,
            last_category=category,
            last_latency_ms=latency_ms,
            capture_total=runtime.hub.status.capture_total + 1,
            capture_success=runtime.hub.status.capture_success + 1,
            error_msg="",
        )
        log.info("RESULT[%s] %s/%s (%dms)", source, category, obj_name, latency_ms)
        time.sleep(config.DISPLAY_HOLD_SEC)
        self._cooldown_until = time.time() + 0.5  # 短暂冷却避免重复

    # ----- ERROR 屏 -----
    def _go_error(self, msg: str):
        self.state = State.ERROR
        log.error(msg)
        oled_ui.show_error(msg)
        runtime.hub.update_status(
            state=State.ERROR,
            error_msg=msg,
            capture_error=runtime.hub.status.capture_error + 1,
        )
        time.sleep(config.ERROR_HOLD_SEC)

    # ----- 主循环 -----
    def loop(self):
        log.info("Boot sequence start.")
        image_store.init()
        oled_ui.self_test()
        time.sleep(1.0)
        oled_ui.show_ready()
        runtime.hub.update_status(state=State.READY)
        log.info("READY. Waiting for IR on GPIO %s ...", config.PIN_IR)

        # 启动 Web 后台 (失败不致命,允许在纯终端模式下运行)
        try:
            import web_server
            web_server.start_in_thread()
        except Exception as e:
            log.warning("Web server failed to start: %s", e)

        try:
            while not runtime.hub.stop.is_set():
                # READY 屏每秒刷新一次
                self._ready_tick()

                # 1) 红外物理触发
                if self.gpio.wait_ir(timeout=0.1):
                    self.gpio.clear_ir()
                    if self.gpio.read_ir() == 0:
                        self.run_once(source="IR")

                # 2) Web 手动触发
                elif runtime.hub.manual_trigger.is_set():
                    runtime.hub.manual_trigger.clear()
                    self.run_once(source="MANUAL")

                oled_ui.show_ready()
        finally:
            self.gpio.cleanup()
            log.info("Bye.")

    def _ready_tick(self):
        now = time.time()
        if now - self._last_ready_tick > 1.0:
            self._last_ready_tick = now
            oled_ui.show_ready()


# ----------------------------------------------------------------------
# 5. 入口
# ----------------------------------------------------------------------
def main():
    _setup_logging()
    sm = WiseEyeStateMachine()
    try:
        sm.loop()
    except Exception as e:
        log.exception("Fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
