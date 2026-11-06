# 本机控制模型与浏览器前视

本机为 RTX 4090 Laptop GPU，显存 16376 MiB。选择官方 Apache-2.0 的 Qwen3.5 BF16 基座与 [Show-Harness 官方 LoRA](https://huggingface.co/showlab/Show-Harness-VLMs)：2B 为默认，4B 为较大备选，0.8B 为轻量备选。2B 同时注册真机和仿真两个独立 LoRA。9B 的 BF16 权重本身超过 16 GB；Gemma 需要另行接受模型许可，InternVL 暂无这个发布仓库中的配套控制 LoRA，因此本轮未下载它们。

模型与适配器版本固定在 `models.lock.json`，下载后校验官方 LFS SHA-256。模型、推理环境与 CUDA 缓存留在本机 `~/.cache/hackathon/`；不复用 SSD 上的 Mac 环境。安装环境为 Linux x86_64 / Python 3.12，vLLM 0.24.0、Transformers 5.12.1、HF Hub 1.17.0；完整依赖锁定在 `requirements-vllm.lock.txt`。

```bash
bash show_harness/models.sh setup
bash show_harness/models.sh download
bash show_harness/models.sh verify                # 只校验本地文件，不重新下载权重
bash show_harness/models.sh serve                 # 默认 2B，前台运行
# 其他模型：先结束自己的模型服务，再启动其中一个
bash show_harness/models.sh serve qwen3_5_4b
bash show_harness/models.sh serve qwen3_5_0_8b
# 后台服务与切换（stop 仅结束这个辅助入口创建的模型进程组）
bash show_harness/models.sh start                 # 默认 2B
bash show_harness/models.sh stop
bash show_harness/models.sh start qwen3_5_4b
```

服务仅监听 `127.0.0.1:8000`。设置 4096 上下文、一次一个请求、最多两张图、禁视频、512 MiB KV cache、eager 模式；采用上游的 `qwen3_5_nothink.jinja`，不插入额外 think 段。客户端按上游 `robot_piper_ft.yaml` 设置 `max_tokens: 256`、`reasoning_cot: false` 和 `enable_thinking: false`，温度为 0。服务器启动离线读取已下载的文件，切换模型不会下载权重。

启动脚本设置环境内 NVIDIA cu13 的编译器路径，单请求贪心解码使用 vLLM 原生采样器，避免 FlashInfer sampling JIT 假定存在 `/usr/local/cuda`。不修改系统 CUDA 或机械臂/相机环境。

2B 服务中的客户端模型名：

- `qwen3_5_2b_showharness_ft`：真实 Franka/Piper 单臂训练语料。
- `qwen3_5_2b_showharness_sim`：RoboLab/ManiSkill 仿真训练语料。

0.8B/4B 分别使用 `qwen3_5_0_8b_showharness_ft` / `qwen3_5_4b_showharness_ft`。适配器不能跨基座替换。`vlm_local.yaml` 是可供 Show-Harness 合并的模型连接配置，不是硬件启动配置；真实 Piper 的执行边界需要交换 MV_FWD/MV_BACK，仿真无需交换。所有发布适配器使用 `prompts/v3/`，顺序为 agentview 图、所选侧 wrist 图、提示文本。

## GUMI 前视 webcam

前视只使用 **打开网页的电脑的浏览器 webcam**，后台不打开 alien 的内置相机。内网页面仍为 `http://10.24.72.109:8620/`，启动命令为 `bash show_harness/run.sh gumi --host 0.0.0.0`。直接用内网 HTTP 可查看腕部和已上传的前视；要启用访问端摄像头，需从 localhost/127.0.0.1 或 HTTPS 打开页面。

点击“使用浏览器摄像头”并授权，将相机对准机械臂和工作区；输入不镜像、不替代腕部图像、不申请麦克风。alien 本机直接打开 `http://127.0.0.1:8620/`；Mac 或其他内网电脑可使用 SSH 转发，再在该电脑打开同一 localhost 地址：

```bash
ssh -N -L 8620:127.0.0.1:8620 hokei@10.24.72.109
```

浏览器 API 需要 localhost/127.0.0.1 或 HTTPS 和用户授权，见 [getUserMedia 文档](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)。未授权、关闭、页面隐藏或视频停止后前视不可用。网页借助 [requestVideoFrameCallback](https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback) 上传新视频帧，约 10 fps、640×480 JPEG；一次只上传一帧，不积压、不重传旧图。通过 `presentedFrames`（缺失时使用视频帧回调计数）判断画面推进，兼容直播摄像头重复报告 `mediaTime` 的浏览器。

2026-10-12 修正“预览闪烁几次后前视相机连接中断：Fetch is aborted”。实测 SSH 转发下行约 0.9 MB/s，三路 MJPEG 超出带宽，两腕套接字各积压约 2 MB，上传回执被排在视频数据后而触发原来的 1 秒超时。页面现以 `/snapshot/<view>` 单帧轮询显示相机，每视图同时只有一个请求、最高约 15 fps，慢链路丢帧而不排队；启用 webcam 时前视格直接显示本地 video，避免再下载同一幅图像。相机 HTTP 请求超时调整为 3 秒，单次失败保持相机开启，约 0.5 秒后换 ticket、采新帧重试；状态栏显示连续失败次数。停止或重启相机时，会话代次检查会丢弃先前尚未完成的编码、上传及续期回调，防止旧会话覆盖新会话。

后端帧龄上限仍为 **0.8 秒**，独立于 3 秒 HTTP 回执超时。采集 ticket 的时间覆盖浏览器绘制、JPEG 编码、上传与服务端解码，并检查会话、递增序号/帧计数、未倒退的 `mediaTime` 和图像大小。相同来源续期时可保留原帧，但不延长其原有帧龄；超龄立即不可用于模型。仅保留最新一帧于进程内存，拒绝另一浏览器接替仍有效的来源。本地 video 可见不代表后端图像有效，以状态栏及下述元数据为准。帧龄是浏览器视频帧的年龄上限，并非硬件曝光时间；前视与两腕没有硬件同步。

带宽回归检查使用本机 0.9 MB/s 共享 FIFO 代理：旧页面模式 12 秒内 5 次上传有 4 次超时；改为单帧轮询与本地前视预览后，116 次上传全部接受，最大回执延迟 0.105 秒，两腕各约 10 fps。这是带宽模拟证据，不是 Mac 端浏览器实测。

- `GET /api/state`：`front_camera_available` 与 `front_camera` 元数据。
- `GET /api/webcam/frame` 或 `/snapshot/agentview`：有效时返回 JPEG、序号和帧年龄上限；失效返回 503，不把页面占位图交给模型。
- `LocalRGBCamera('/camera_f/color/image_raw')`：读该 HTTP 来源，符合原版 `(success, RGB uint8, None)` 接口，未启用时构造失败。

网页填写任务，选择左/右臂和**当前已启动的模型**，点击“仅推理预览”。`POST /api/model/preview` 读取新鲜前视与该侧腕部图，按原版处理为 256×256，调用上游 VLMClient/MvTokenController，返回动作 token；该入口不入动作队列、不调用 controller.step、不发送机械臂或夹爪指令。

GUMI 同时已接入“2B 推理并执行一步”“2B 连续运行”和“停止模型并保持”，实机入口固定使用 2B 真机 LoRA，由 `live_piper.yaml` 与 `model_control.py` 对齐上游单臂策略。每次从当前位置开始，读取前视和所选腕部图像，按原始 token 维护历史，在执行边界交换 MV_FWD/MV_BACK，再交给本机机器人工作线程。操作方法、20 mm 步长、停止条件及日志见 [实机推理操作说明](LIVE_CONTROL.md)。

发布 LoRA 为单臂策略，一次控制所选的一侧；相机视角、方向及现场工作区仍需结合实机检查。下述安装和保存图片验证仅证明模型接口可用，实际运动及任务结果以实机日志和现场验收为准。

## 验证

```bash
bash show_harness/models.sh status
bash show_harness/models.sh check --profile qwen3_5_2b
bash show_harness/models.sh check --profile qwen3_5_2b_sim
# 启动对应服务器后测试备选
bash show_harness/models.sh check --profile qwen3_5_4b
bash show_harness/models.sh check --profile qwen3_5_0_8b
```

离线检查使用 [官方训练数据](https://huggingface.co/datasets/showlab/Show-Harness-Data) 中各一对 Franka、Piper、仿真图，版本、样本、图片 SHA-256 留在 `results/show_harness/models/samples/sources.json`。检查未经 guided decoding 约束的原始输出是否严格为合法 token，记录响应、延迟与显存；这是安装和模型接口证据，不能据训练样本评估泛化成功率。实际测量结果保存在 `results/show_harness/models/`。

2026-10-12 本机实测：2B 真机 LoRA 两对官方图片各调用两次，均输出严格的 `MV_DOWN`；17:29 这次重启后的首个请求耗时 3.544 秒，之后请求 0.135–0.159 秒。将输出上限对齐为 256 后再次四次调用均通过，耗时 0.131–0.144 秒，GPU 总显存占用 5725 MiB（含桌面及进程开销）；报告为 `results/show_harness/models/20261012-172905-qwen3_5_2b.json` 和 `20261012-172938-qwen3_5_2b.json`。2B 仿真 LoRA 的两次离线调用也通过，报告为 `20261012-162141-qwen3_5_2b_sim.json`。这些延迟来自保存图片及已缓存重复请求，不代表新鲜摄像头闭环速度。

0.8B、2B、4B 基座及四个 LoRA 全部完成下载、文件大小与官方 LFS SHA-256 校验，回执为 `results/show_harness/models/20261012-download-receipt.json`；环境和验收范围另记于 `20261012-installation.json`。0.8B 已成功启动并注册 LoRA，显存采样最高 3038 MiB，尚未完成图片推理验收；4B 已通过文件检查与启动命令 dry-run，尚未进行 GPU 推理验收。当前服务保持 2B 真机 LoRA 就绪，实机测试期间不切换备选模型。
