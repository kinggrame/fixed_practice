"""
智慧眼识物相机 - 主状态机
=======================
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
  - 蜂鸣器:   BCM 23, PWM 播放旋律 (兰花草/可惜不是你)
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

import config
import identity
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

    _NOTE = {
        "C4": 262, "D4": 294, "E4": 330, "F4": 349, "G4": 392, "A4": 440, "B4": 494,
        "C5": 523, "D5": 587, "E5": 659, "F5": 698, "G5": 784, "A5": 880, "B5": 988,
        "C6": 1047, "D6": 1175, "E6": 1319, "F6": 1397, "G6": 1568,
    }

    _MELODY_SUCCESS = [
        ("C5", 0.12), ("G5", 0.18),
    ]

    _MELODY_ERROR = [
        ("A4", 0.2), ("_", 0.1), ("A4", 0.3),
    ]

    def __init__(self):
        self._ir_last = 1          # 上次 IR 电平,用于检测下降沿
        self._light_history = deque(maxlen=10)
        self._real = False
        self._pwm = None
        self._setup()

    def _setup(self):
        try:
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(config.PIN_IR, GPIO.IN)
            GPIO.setup(config.PIN_LIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(config.PIN_BUZZER, GPIO.OUT)
            self._pwm = GPIO.PWM(config.PIN_BUZZER, 440)
            self._pwm.start(0)
            self._gpio = GPIO
            self._real = True
            log.info("GPIO ready: IR=%s LIGHT=%s BUZZER=%s",
                      config.PIN_IR, config.PIN_LIGHT, config.PIN_BUZZER)
        except Exception as e:
            log.warning("GPIO init failed (%s); running in PC simulation.", e)
            self._gpio = None

    def poll_ir(self) -> bool:
        """轮询检测下降沿 (1→0),返回 True 表示触发。"""
        if not self._real:
            return False
        try:
            cur = self._gpio.input(config.PIN_IR)
            if self._ir_last == 1 and cur == 0:
                self._ir_last = cur
                return True
            self._ir_last = cur
        except Exception:
            pass
        return False

    def read_ir(self) -> int:
        if self._real:
            return self._gpio.input(config.PIN_IR)
        return 1

    def read_light(self) -> int:
        if self._real:
            return self._gpio.input(config.PIN_LIGHT)
        return 0

    def read_light_debounced(self) -> int:
        debounce_ms = runtime.hub.cfg.light_debounce_ms
        window = max(1, debounce_ms // 50)
        if len(self._light_history) >= window:
            self._light_history.popleft()
        self._light_history.append(self.read_light())
        return 1 if sum(self._light_history) > len(self._light_history) / 2 else 0

    def play_melody(self, name: str):
        if not self._real or self._pwm is None:
            return
        melody = self._MELODY_SUCCESS if name == "success" else \
                 self._MELODY_ERROR if name == "error" else None
        if not melody:
            return
        threading.Thread(target=self._play, args=(melody,), daemon=True).start()

    def _play(self, melody):
        for note_name, dur in melody:
            if note_name == "_":
                try:
                    self._pwm.ChangeDutyCycle(0)
                except Exception:
                    pass
                time.sleep(dur)
                continue
            freq = self._NOTE.get(note_name, 440)
            try:
                self._pwm.ChangeFrequency(freq)
                self._pwm.ChangeDutyCycle(50)
                time.sleep(dur)
                self._pwm.ChangeDutyCycle(0)
            except Exception:
                break
            time.sleep(0.02)

    def cleanup(self):
        if self._real:
            try:
                if self._pwm:
                    self._pwm.stop()
                self._gpio.cleanup()
            except Exception:
                pass


# ----------------------------------------------------------------------
# 3. 决策层
# ----------------------------------------------------------------------
def resolve_mode(light_raw: int) -> str:
    policy = runtime.hub.cfg.light_policy
    if policy == "FORCE_DAY":
        return "DAY"
    if policy == "FORCE_NIGHT":
        return "NIGHT"
    return "NIGHT" if light_raw == 1 else "DAY"


def resolve_enhancement(mode: str) -> str:
    em = runtime.hub.cfg.enhancement_mode
    if em == "OFF":
        return "OFF"
    if em == "DAY":
        return "DAY"
    if em == "NIGHT":
        return "NIGHT"
    return mode


def apply_enhancement(jpeg_bytes: bytes, mode: str) -> bytes:
    return image_engine.enhance_from_bytes(jpeg_bytes, mode if mode != "OFF" else "OFF")


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
        self._cooldown_until = 0.0

    def _on_signal(self, signum, frame):
        log.info("Signal %s, shutting down.", signum)
        runtime.hub.stop.set()

    # ----- 1) 捕获入队 -----
    def _capture_one(self, source: str = "IR"):
        now = time.time()
        if now < self._cooldown_until:
            return

        self.state = State.CAPTURE
        light_raw = self.gpio.read_light_debounced()
        mode = resolve_mode(light_raw)
        log.info("CAPTURE[%s] light=%s mode=%s", source, light_raw, mode)
        runtime.hub.update_status(state=State.CAPTURE, mode=mode)

        for sec in range(5, 0, -1):
            oled_ui.show_countdown(sec)
            time.sleep(1)

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
            log.exception("Capture failed")
            self._go_error(f"Cam:{e.__class__.__name__}")
            return

        runtime.hub.buffer_push(enhanced, mode, light_raw)
        log.info("Pushed to buffer (size=%d).", runtime.hub.buffer_len())

    # ----- 2) 消费缓冲区 -----
    def _process_buffer(self):
        n = runtime.hub.buffer_len()
        if n == 0:
            return

        items = runtime.hub.buffer_pop(n)
        is_batch = len(items) > 1
        jpegs = [it["bytes"] for it in items]
        modes = [it["mode"] for it in items]

        self.state = State.PROCESS
        oled_ui.show_process()
        runtime.hub.update_status(state=State.PROCESS)
        log.info("PROCESS %d image(s) via %s", len(jpegs),
                 "BATCH VLM" if is_batch else "single VLM")

        self.state = State.THINKING
        oled_ui.show_thinking()
        runtime.hub.update_status(state=State.THINKING)
        t0 = time.time()

        try:
            if is_batch:
                results = vlm_client.recognize_batch(jpegs)
            else:
                r = vlm_client.recognize(jpegs[0])
                results = [r]
            latency_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            log.exception("VLM failed")
            self._go_error(f"VLM:{e.__class__.__name__}")
            for jpg in jpegs:
                del jpg
            return

        for idx, (obj_name, category, description, scene) in enumerate(results):
            img_id = None
            if runtime.hub.cfg.save_image:
                try:
                    img_id = image_store.save(jpegs[idx], modes[idx])
                except Exception as e:
                    log.warning("save failed: %s", e)
            if img_id:
                image_store.update_result(
                    img_id, obj_name, category, description, latency_ms)
            log.info("RESULT[%d/%d] %s/%s — %s  [%s]", idx + 1, len(results),
                     category, obj_name, description, scene)
            identity.append(scene, obj_name, category, description)

        oled_ui.show_result(results[-1][1], results[-1][0], results[-1][2])
        runtime.hub.update_status(
            state=State.DISPLAY,
            last_object_name=results[-1][0],
            last_category=results[-1][1],
            last_description=results[-1][2],
            last_latency_ms=latency_ms,
            capture_total=runtime.hub.status.capture_total + len(results),
            capture_success=runtime.hub.status.capture_success + len(results),
            error_msg="",
        )
        self.gpio.play_melody("success")
        for jpg in jpegs:
            del jpg
        # DISPLAY 阶段: 短轮询,新 IR 可打断
        for _ in range(int(config.DISPLAY_HOLD_SEC * 10)):
            if self.gpio.poll_ir():
                self._capture_one(source="IR")
                break
            time.sleep(0.1)
        self._cooldown_until = time.time() + 0.3

    # ----- ERROR -----
    def _go_error(self, msg: str):
        self.state = State.ERROR
        log.error(msg)
        oled_ui.show_error(msg)
        self.gpio.play_melody("error")
        runtime.hub.update_status(
            state=State.ERROR, error_msg=msg,
            capture_error=runtime.hub.status.capture_error + 1,
        )
        time.sleep(config.ERROR_HOLD_SEC)

    # ----- 主循环 -----
    def loop(self):
        log.info("Boot sequence start.")
        image_store.init()
        oled_ui.self_test()
        oled_ui.show_ready()
        time.sleep(1.0)
        runtime.hub.update_status(state=State.READY)
        log.info("READY. Buffer-driven mode; waiting for IR ...")

        try:
            import web_server
            web_server.start_in_thread()
        except Exception as e:
            log.warning("Web server failed: %s", e)

        try:
            while not runtime.hub.stop.is_set():
                if self.gpio.poll_ir():
                    self._capture_one(source="IR")

                if runtime.hub.manual_trigger.is_set():
                    runtime.hub.manual_trigger.clear()
                    self._capture_one(source="MANUAL")

                if runtime.hub.buffer_len() > 0:
                    self._process_buffer()
                    runtime.hub.update_status(state=State.READY)
        finally:
            self.gpio.cleanup()
            log.info("Bye.")


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
