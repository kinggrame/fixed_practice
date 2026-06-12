# 智慧眼识物相机系统 v1.1

> 边缘端 1GB 树莓派 + 阿里通义 Qwen-VL-Plus + Web 管理后台

## 项目结构

```
.
├── config.py         # 静态配置 (引脚/路径/默认策略)
├── runtime.py        # 进程内线程安全运行时配置 (前端可改)
├── oled_ui.py        # SSD1306 6 个状态屏渲染
├── image_engine.py   # ffmpeg 抓拍 + Pillow 自适应增强
├── image_store.py    # SQLite 元数据 + 按日分目录 JPEG 持久化
├── vlm_client.py     # OpenAI 兼容 VLM 客户端 (凭据热读)
├── web_server.py     # Flask REST API (后台线程)
├── main.py           # 状态机 + Web 启动
├── requirements.txt
├── templates/
│   └── index.html    # Vue3 单页应用
├── static/
│   ├── css/app.css
│   └── js/app.js
└── data/             # 运行时自动生成
    ├── images/YYYYMMDD/<uuid>.jpg
    └── wise_eye.db
```

## 硬件接线 (BCM)

| 硬件 | 引脚 | 说明 |
| --- | --- | --- |
| 红外避障 DO | GPIO 18 | 下降沿触发 (物体靠近 -> 低电平) |
| 光敏电阻 DO | GPIO 27 | **仅数字量输出**: 亮->低(0) / 暗->高(1)。阈值在模块蓝色电位器上用螺丝刀调节,软件不可改。软件只能读 DO、去抖、强制覆盖 (FORCE_DAY/FORCE_NIGHT) |
| OLED | I2C1, 0x3C | SCL=GPIO3, SDA=GPIO2 |
| USB 摄像头 | /dev/video0 | UVC 兼容 |

## 部署

```bash
sudo apt install -y python3-pip python3-pil ffmpeg i2c-tools fonts-wqy-zenhei
sudo raspi-config                # 启用 I2C
pip3 install -r requirements.txt --break-system-packages

export VLM_API_KEY="sk-xxx..."   # 阿里云百炼 (DashScope) 的 key
python3 main.py
```

启动后访问 `http://<pi-ip>:8080/` 即可进入管理后台。

## 默认 VLM: 阿里通义 Qwen-VL-Plus

- API Base: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- 模型名: `qwen-vl-plus` (可换成 `qwen3-vl-plus`)
- 协议: OpenAI 兼容,Structured Outputs (`response_format=json_object`)
- 替换为智谱 / OpenAI / Kimi 等只需在前端「运行配置」页改 3 个字段

## 前端四大面板

| 面板 | 功能 |
| --- | --- |
| **实时状态** | 当前状态机 (颜色随状态变化)、最近一次物体名/分类/延迟、累计统计、手动触发按钮 |
| **历史图库** | 缩略图网格 + 元数据 (时间/模式/状态/延迟) + 点击放大 + 删除 + 分页 + 状态过滤 |
| **运行配置** | 光敏策略 (AUTO/FORCE_DAY/FORCE_NIGHT) + 去抖窗口 + 增强档位 (AUTO/DAY/NIGHT/OFF) + 保存开关 + VLM 凭据/模型。**保存即生效,不需重启** |
| **实时日志** | 内存环形日志 (最近 500 条) + 1s 轮询 + 按级别染色 + 自动滚动/暂停 |

## REST API

```
GET  /api/state              # 系统快照
GET  /api/stats              # 统计
GET  /api/config             # 读配置
POST /api/config             # 改配置 (热生效)
GET  /api/images?page=1&per_page=20&status=success
GET  /api/images/<id>/file   # 静态图片
DEL  /api/images/<id>        # 删除一条
GET  /api/logs?tail=200
POST /api/trigger            # 手动触发
```

## 关键配置说明

| 参数 | 说明 | 典型值 |
|------|------|--------|
| `CAMERA_INPUT_FORMAT` | 摄像头像素格式。`mjpeg`(默认,内存友好) / `yuyv422`(兼容好) / 留空自协商。部分 UVC 不暴露 MJPEG 需改 yuyv422 | `mjpeg` |
| `CAPTURE_W` / `CAPTURE_H` | **必须锁小分辨率**。不指定时 uvc 常默认 1080p YUYV(单帧 4MB+),1GB Pi 直接 OOM | 640 / 480 |
| `CAPTURE_JPEG_QUALITY` | ffmpeg `-q:v` 参数,1(最高)~31(最差)。VLM 识别 5 足够且体积最小 | 5 |
| `light_policy` | AUTO(读 DO) / FORCE_DAY / FORCE_NIGHT。**阈值在硬件电位器**调节,软件只能读/去抖/覆盖 | AUTO |
| `light_debounce_ms` | DO 去抖窗口(毫秒)。多数表决防临界光线抖动 | 300 |
| `enhancement_mode` | AUTO(环境自适应) / DAY(仅降噪) / NIGHT(Gamma 0.5) / OFF(关) | AUTO |

## ffmpeg 抓拍命令参考

当前 `image_engine.py` 实际执行的命令 (等价于):

```bash
ffmpeg \
  -hide_banner -loglevel error -y \
  -f v4l2 \
  -video_size 640x480 \
  -fflags nobuffer -an \
  -input_format mjpeg \
  -i /dev/video0 \
  -frames:v 1 \
  -q:v 5 \
  -update 1 \
  /tmp/wise_eye_capture.jpg
```

如果摄像头不支持 MJPEG,设置 `export CAMERA_INPUT_FORMAT=""` 去掉 `-input_format` 参数即可。

## 内存策略 (1GB 约束)

- 抓拍 -> 读 bytes -> 删盘 -> 增强 (纯内存) -> base64 -> del,绝无长期驻留
- SQLite + 单层 WAL,小数据量
- 图像按天分目录,自动按 7 天 / 500 张淘汰
- 关闭 OLED / 关闭 Web 仍能跑 (非核心服务异常不影响主状态机)
