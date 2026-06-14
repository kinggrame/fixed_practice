"""
Flask Web 管理后台
==================
- 路由: /api/* 提供 REST 接口
- 静态: /static/* 与 / 直接返回前端 SPA
- 运行: 在后台守护线程中跑 Flask,主线程继续状态机
- 安全: 仅绑定局域网,本项目不内置鉴权 (树莓派本地小工具,适合反代或防火墙兜底)
"""

import logging
import os
import threading
import time
from typing import Any, Dict

from flask import (
    Flask, jsonify, request, send_from_directory, send_file, abort
)

import config
import identity
import image_store
import runtime

log = logging.getLogger("wise_eye.web")


# ----------------------------------------------------------------------
# 1. Flask App Factory
# ----------------------------------------------------------------------
def create_app() -> Flask:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        static_folder=os.path.join(base_dir, "static"),
        template_folder=os.path.join(base_dir, "templates"),
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB 上限

    # ---- 错误格式统一 ----
    @app.errorhandler(400)
    def _bad(e):
        return jsonify({"ok": False, "err": "bad request"}), 400

    @app.errorhandler(404)
    def _nf(e):
        return jsonify({"ok": False, "err": "not found"}), 404

    @app.errorhandler(500)
    def _ie(e):
        log.exception("internal error")
        return jsonify({"ok": False, "err": "internal"}), 500

    # ---- 页面 ----
    @app.route("/")
    def index():
        return send_from_directory(app.template_folder, "index.html")

    @app.route("/static/<path:p>")
    def static_file(p):
        return send_from_directory(app.static_folder, p)

    # ---- API: 实时状态 ----
    @app.route("/api/state")
    def api_state():
        s = runtime.hub.snapshot()
        s["stats"] = image_store.stats()
        s["ts"] = time.time()
        return jsonify({"ok": True, "data": s})

    # ---- API: 配置 GET/POST ----
    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if request.method == "GET":
            return jsonify({"ok": True, "data": runtime.hub.get_config()})
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            abort(400)
        new_cfg = runtime.hub.patch_config(body)
        log.info("config patched: %s", body)
        return jsonify({"ok": True, "data": new_cfg})

    # ---- API: 图库 ----
    @app.route("/api/images")
    def api_images():
        try:
            page = int(request.args.get("page", 1))
            per_page = int(request.args.get("per_page", 20))
            status = request.args.get("status") or None
        except ValueError:
            abort(400)
        return jsonify({"ok": True, "data": image_store.list_images(page, per_page, status)})

    @app.route("/api/images/<img_id>/file")
    def api_image_file(img_id):
        path = image_store.get_path(img_id)
        if not path or not os.path.exists(path):
            abort(404)
        return send_file(path, mimetype="image/jpeg", conditional=True)

    @app.route("/api/images/<img_id>", methods=["DELETE"])
    def api_image_delete(img_id):
        ok = image_store.delete(img_id)
        return jsonify({"ok": ok})

    # ---- API: 日志 ----
    @app.route("/api/logs")
    def api_logs():
        try:
            n = int(request.args.get("tail", 100))
        except ValueError:
            n = 100
        return jsonify({"ok": True, "data": runtime.hub.tail_logs(n)})

    # ---- API: 手动触发 (始终入队,不阻塞) ----
    @app.route("/api/trigger", methods=["POST"])
    def api_trigger():
        runtime.hub.manual_trigger.set()
        return jsonify({"ok": True, "msg": "triggered (enqueued)"})

    # ---- API: 缓冲区缩略图快照 ----
    @app.route("/api/buffer")
    def api_buffer():
        items = runtime.hub.buffer_snapshot()
        return jsonify({"ok": True, "data": items})

    # ---- API: 身份画像 ----
    @app.route("/api/identity")
    def api_identity():
        content = identity.load()
        return jsonify({"ok": True, "data": content or "# 用户身份画像\n\n尚未建立画像,等待首次识别。"})

    # ---- API: 简易统计 ----
    @app.route("/api/stats")
    def api_stats():
        s = runtime.hub.snapshot()
        s["store"] = image_store.stats()
        s["ts"] = time.time()
        return jsonify({"ok": True, "data": s})

    return app


# ----------------------------------------------------------------------
# 2. 后台线程启动
# ----------------------------------------------------------------------
def start_in_thread() -> threading.Thread:
    """启动 Flask 在守护线程,主线程继续状态机。"""
    app = create_app()
    t = threading.Thread(
        target=lambda: app.run(
            host=config.WEB_HOST,
            port=config.WEB_PORT,
            threaded=True,
            use_reloader=False,
            debug=False,
        ),
        name="wise-eye-web",
        daemon=True,
    )
    t.start()
    log.info("Web UI: http://%s:%s/", config.WEB_HOST, config.WEB_PORT)
    return t


if __name__ == "__main__":
    # 单独跑这个文件可以仅启动 web (用于 PC 上做 UI 调试)
    import image_store
    image_store.init()
    create_app().run(host=config.WEB_HOST, port=config.WEB_PORT, threaded=True, debug=True)
