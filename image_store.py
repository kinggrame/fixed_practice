"""
图像持久化存储
==============
- SQLite 存元数据 (id, 时间戳, 模式, VLM 结果, 延迟, 文件名, 状态)
- JPEG 字节按 YYYYMMDD 子目录落盘
- 启动 / 每次写入后做一次清理 (按保留天数与最大数量淘汰)
- 写入是阻塞的,但 JPEG 写入毫秒级,对 1GB Pi 无压力
"""

import os
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

import config


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


# ----------------------------------------------------------------------
# 1. 初始化
# ----------------------------------------------------------------------
def init():
    os.makedirs(config.IMAGE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with _lock:
        global _conn
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id            TEXT PRIMARY KEY,
                timestamp     INTEGER NOT NULL,
                mode          TEXT NOT NULL,
                object_name   TEXT,
                category      TEXT,
                filename      TEXT NOT NULL,
                file_size     INTEGER,
                latency_ms    INTEGER,
                status        TEXT NOT NULL,
                error_msg     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_timestamp ON captures(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_status    ON captures(status);
            """
        )
    _cleanup()


# ----------------------------------------------------------------------
# 2. 写操作
# ----------------------------------------------------------------------
def save(jpeg_bytes: bytes, mode: str) -> str:
    """
    保存一张增强后的 JPEG,生成 uuid 文件名,默认 status='pending'。
    稍后由 main 调用 update_result() 写入 VLM 返回结果。
    返回 image_id。
    """
    img_id = uuid.uuid4().hex
    day = time.strftime("%Y%m%d")
    sub_dir = os.path.join(config.IMAGE_DIR, day)
    os.makedirs(sub_dir, exist_ok=True)
    fname = f"{img_id}.jpg"
    fpath = os.path.join(sub_dir, fname)
    with open(fpath, "wb") as f:
        f.write(jpeg_bytes)
    size = len(jpeg_bytes)
    with _lock:
        _conn.execute(
            "INSERT INTO captures(id, timestamp, mode, filename, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (img_id, int(time.time() * 1000), mode, fpath, size, "pending"),
        )
    _cleanup()
    return img_id


def update_result(img_id: str, obj_name: str, category: str, latency_ms: int):
    with _lock:
        _conn.execute(
            "UPDATE captures SET object_name=?, category=?, latency_ms=?, status=? "
            "WHERE id=?",
            (obj_name, category, latency_ms, "success", img_id),
        )


def mark_error(img_id: str, msg: str):
    with _lock:
        _conn.execute(
            "UPDATE captures SET status=?, error_msg=? WHERE id=?",
            ("error", msg[:200], img_id),
        )


def delete(img_id: str) -> bool:
    """删除一条记录及对应文件。"""
    with _lock:
        row = _conn.execute("SELECT filename FROM captures WHERE id=?", (img_id,)).fetchone()
        if not row:
            return False
        fpath = row[0]
        _conn.execute("DELETE FROM captures WHERE id=?", (img_id,))
    try:
        if fpath and os.path.exists(fpath):
            os.remove(fpath)
    except OSError:
        pass
    return True


# ----------------------------------------------------------------------
# 3. 读操作
# ----------------------------------------------------------------------
def get_path(img_id: str) -> Optional[str]:
    with _lock:
        row = _conn.execute("SELECT filename FROM captures WHERE id=?", (img_id,)).fetchone()
    return row[0] if row else None


def list_images(page: int = 1, per_page: int = 20, status: Optional[str] = None) -> dict:
    page = max(1, page)
    per_page = max(1, min(100, per_page))
    offset = (page - 1) * per_page
    where = "WHERE status=?" if status else ""
    params = (status, per_page, offset) if status else (per_page, offset)
    with _lock:
        rows = _conn.execute(
            f"SELECT id, timestamp, mode, object_name, category, file_size, latency_ms, status "
            f"FROM captures {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        total = _conn.execute(
            f"SELECT COUNT(*) FROM captures {where}",
            (status,) if status else (),
        ).fetchone()[0]
    items = [
        {
            "id": r[0],
            "timestamp": r[1],
            "mode": r[2],
            "object_name": r[3] or "",
            "category": r[4] or "",
            "file_size": r[5],
            "latency_ms": r[6],
            "status": r[7],
            "url": f"/api/images/{r[0]}/file",
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


def stats() -> dict:
    with _lock:
        total = _conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        succ = _conn.execute("SELECT COUNT(*) FROM captures WHERE status='success'").fetchone()[0]
        err = _conn.execute("SELECT COUNT(*) FROM captures WHERE status='error'").fetchone()[0]
    return {"total": total, "success": succ, "error": err}


# ----------------------------------------------------------------------
# 4. 清理策略
# ----------------------------------------------------------------------
def _cleanup():
    """按天数与最大数量裁剪,只在持锁外的轻量时机调用。"""
    try:
        with _lock:
            # 1) 超龄
            cutoff = int((time.time() - config.IMAGE_RETENTION_DAYS * 86400) * 1000)
            old = _conn.execute(
                "SELECT id, filename FROM captures WHERE timestamp<?",
                (cutoff,),
            ).fetchall()
            for oid, fpath in old:
                _conn.execute("DELETE FROM captures WHERE id=?", (oid,))
                _try_remove(fpath)
            # 2) 超量: 保留最新的 N 条
            over = _conn.execute(
                "SELECT id, filename FROM captures ORDER BY timestamp DESC "
                "LIMIT -1 OFFSET ?",
                (config.IMAGE_RETENTION_MAX,),
            ).fetchall()
            for oid, fpath in over:
                _conn.execute("DELETE FROM captures WHERE id=?", (oid,))
                _try_remove(fpath)
    except Exception:
        pass


def _try_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
