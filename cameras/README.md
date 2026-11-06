# 双腕 DaBai DC1 RGB-D 采集

已在 alien 同时启动两台 Orbbec DaBai DC1（RD1001）：左腕 CC1365301R4 / can0，右腕 CC1365301HT / can1。右腕序列号由用户现场确认，映射保存在 mapping.json。按序列号绑定，不依赖枚举顺序或 /dev/video 编号。

使用官方 legacy Orbbec SDK v1.10.37，DC1 的深度端为 OpenNI 协议。SDK v2 不适用于本次设备。SDK 下载与解压留在机器本地缓存，setup.py 记录本次官方发布包的复现 SHA256。

从 robot 目录运行（需要 g++ 和 Python）：

```bash
python3 cameras/setup.py
bash cameras/run.sh
# 或指定新的绝对输出目录：
bash cameras/run.sh "$PWD/results/cameras/my_capture"
```

入口同时启动两台相机，采样约 10 秒，确认双路彩色/深度帧数及最新帧不超过 500 ms，再保存各相机最后一帧和 capture.json，随后关闭采集。不是常驻视频服务。输出目录必须为新目录，不覆盖既有采集。彩色是 640×480 MJPG，深度请求 640×400 Y11，SDK 解包为小端 uint16（当前单位 1 mm，零值无效）。两路均设 15 fps。

分析环境与机械臂环境独立：

```bash
uv venv "${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/camera-venv"
uv pip install --python "${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/camera-venv/bin/python" -r cameras/requirements.txt
"${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/camera-venv/bin/python" cameras/analyze_capture.py results/cameras/20261012-135640
```

分析保存 validation.json、毫米浮点深度 .npy、0–2000 mm 灰度预览（超出范围截断，仅供显示）。JPEG、原始 uint16 数据、出厂内参仍保留。

设备权限（本机已安装并重新加载）：

```bash
sudo install -m 0644 cameras/70-hackathon-orbbec.rules /etc/udev/rules.d/70-hackathon-orbbec.rules
sudo udevadm control --reload-rules
```

规则只授权厂商 2bc5 的 0557/0657 接口给 plugdev 和本地活动用户。hokei 已在 plugdev 组；当前连接使用临时用户 ACL。重插后由 udev 规则授权，实际重插尚未验收。

2026-10-12 13:56 实机证据：results/cameras/20261012-135640。两路深度实测约 15.06 fps，彩色约 14.95 fps；左腕有效深度像素 55.92%，右腕 63.15%，当前场景距离中位数分别 349/422 mm。无效像素与当前视野/量程相关，不代表全场景精度验收。

彩色设备时间戳观察到异常尺度，不能直接用于同步。保存 SDK 原始/系统时间戳，并以主机单调到达时间统计帧率与新鲜度。尚未验证设备时钟、硬件同步、彩色与深度对齐或腕部外参。保存的 RGB 与深度是各自最后一帧，并非同步配对；不能直接按彩色像素索引深度。采集不发送机械臂运动报文。

官方来源：[SDK v1.10.37 发布](https://github.com/orbbec/OrbbecSDK/releases/tag/v1.10.37)，以及发布包内 README、include/libobsensor API 头文件和示例。
