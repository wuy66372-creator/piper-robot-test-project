# Show-Harness 本地 Piper 适配

## 2026-10-21 GUMI采集与LoRA训练

现已增加上方可编辑采集任务、2 cm录制、成功/未成功保存及 `/training` 工作台入口。录制使用推理同款20 mm控制器并拒绝3 mm持续控制，记录动作前图像与执行结果；只用人工确认成功的完整示教训练。训练页支持轨迹预览、改任务、归档/恢复、0.8B/2B/4B基座、作业日志/取消及LoRA下载。具体数据、提示词/方向契约、训练资源互斥和验证见 [COLLECTION_TRAINING.md](COLLECTION_TRAINING.md)。后文“训练录制关闭”为历史记录，以本节和上述说明为准。

## 当前主视角：Intel RealSense D435i（2026-10-21）

GUMI 默认主视角已改为 alien 本机 USB 3.2 上的 D435i，序列号 `347622075687`、固件 `5.15.1.55`。官方 Python SDK `pyrealsense2==2.56.5.9235` 安装在既有 `~/.cache/hackathon/show-harness-venv`，依赖锁已更新；官方 [v2.56.5 udev 规则](https://github.com/realsenseai/librealsense/blob/v2.56.5/config/99-realsense-libusb.rules) 安装到 `/etc/udev/rules.d/99-realsense-libusb.rules`。

默认采集 RGB 640×480 / 15 fps，不镜像；网页 `/snapshot/agentview`、`/api/webcam/frame` 兼容读取入口及模型 `agentview` 共用同一帧源。使用 SDK global/system 时间戳检查帧龄≤0.8秒，拒绝重复帧，断流/过期返回503；不以占位图替代模型观测。页面显示 D435i 状态，不申请访问端摄像头权限，浏览器上传不能覆盖主视角。两台 Orbbec 腕部相机继续使用。

```bash
# 当前项目启动命令（只读取状态、查询限位，不自动使能或运动）
bash show_harness/run.sh gumi --host 127.0.0.1 --front-source realsense --front-serial 347622075687
# 相机独占验收：先停止 GUMI，避免两个进程同时打开同一设备
~/.cache/hackathon/show-harness-venv/bin/python -B show_harness/verify_realsense.py --output results/cameras/realsense-check
# 手动恢复历史浏览器来源需显式选择
bash show_harness/run.sh gumi --front-source browser
```

本次使用用户级 `gumi-local.service` 托管，监听远端 `127.0.0.1:8620`。远程电脑通过 `ssh -N -L 127.0.0.1:8620:127.0.0.1:8620 hokei@100.65.174.28` 访问 `http://127.0.0.1:8620/`，无须浏览器摄像头授权。未配置开机自启。模型独立运行于 `127.0.0.1:8000`，本次恢复此前使用的 Qwen3.5-4B + 实机 LoRA。

最终稳定版 SDK 的 RGB 实机证据位于 `results/cameras/realsense-20261021-rgb-stable/`：3秒45帧、最大图像年龄约0.136秒。项目三路快照均返回200，4B仅推理预览成功读入D435i+左腕图像并返回合法token `MV_RIGHT`（约3.71秒），未执行动作。106项后端测试、12项模型JS测试及webcam/GUMI脚本检查通过。

深度双流在2.59.1与最终2.56.5 SDK下都重复出现 `Frame didn't arrive`，故默认 RGB 主视角不依赖深度；`--front-depth` 和验收脚本 `--depth` 保留为独立诊断开关，未验收深度或 IMU。设备固件保持5.15.1.55，未刷写固件或校准。模型原有输入是 RGB，不使用深度。相机之间无硬件同步。两臂本次启动时均为待机、六电机未使能；启动没有改变这些状态。

后文 2026-10-12 的“仅 browser_webcam”和内网直连地址是历史配置，以本节为准。

### 2026-10-21 单张深度图测试

`capture_depth_photo.py --low-power` 使用直接深度传感器队列，在424×240、6 fps、临时关闭红外投射器和全局时间戳的条件下成功获得真实Z16深度图。单位约0.001 m/计数，有效像素65.49%，有效深度中位数1.51 m。原始16位PNG、NumPy数组、0.2–2.0 m范围的伪彩色预览及报告保存于 `results/cameras/depth-photo-20261021-low-power/`。测试后已恢复相机选项（投射器=1）并重新启动RGB主视角项目，4B服务正常、运动计数为0。

标准640×480/30 fps的直接深度采集仍失败；内核日志同时记录相机所在USB集线器及其网卡/键盘整体断连重枚举，供电或链路问题为待验证原因，不归因于已确认的软件故障。低负载单帧成功不代表正常分辨率RGB+深度双流或IMU已验收，当前项目默认仍是已验证RGB配置。失败时的大体积SDK日志已压缩保存为 `results/cameras/depth-photo-20261021-1/sdk-debug.log.gz`。

当前实现绕过 ROS Noetic，复用本机 SocketCAN/官方 Piper SDK 和 Orbbec v1.10.37。上游独立克隆保持原样，固定 `c3a133eb111a8100bcb64509b1ea891fdcee1532`；适配代码与验收证据保存在父项目。运行入口拒绝使用其他上游版本。

已通过的范围：原版 PiperSession 使用本地传输连接两臂；关节 rad、末端 m/xyzw 四元数、夹爪宽度 m、速度 rad/s 和驱动状态可读；PiperAtomicController 的 joint-stream 运动学初始化通过；双腕 SDK 彩色流持续更新；通过本地接口分别执行 J6 +1°/返回。夹爪控制、通用笛卡尔动作、完整视觉策略尚未验收。

从 robot 目录运行：

```bash
# 恢复源码时，单独克隆并切到固定版本：
git clone https://github.com/showlab/Show-Harness.git Show-Harness
git -C Show-Harness checkout c3a133eb111a8100bcb64509b1ea891fdcee1532

# 机器缓存中的独立环境，不改机械臂/相机已有环境：
bash show_harness/run.sh setup
python3 cameras/setup.py
bash show_harness/run.sh test
bash show_harness/run.sh preflight

# 计划模式：连接并查询，不发运动报文
bash show_harness/run.sh jog --arm left --delta 1
# 已固定、空间腾空、空载时的显式小幅动作验收：
bash show_harness/run.sh jog --arm left --delta 1 --execute
bash show_harness/run.sh jog --arm right --delta 1 --execute
```

需要当前 can0/can1 为 UP/1 Mbps，按本机适配器序列号识别左右臂；不重命名接口。动作仅允许已使能、CAN 模式且反馈健康的机械臂。连接时只查询硬件限位，不自动 Enable、Reset、回零或闭合夹爪。与原有控制 CLI 共用进程间控制锁，两臂在同一进程内共用锁租约；同时检查来自其他控制器的运动帧。

基础 LocalPiperInterface 与 CLI commissioning 的动作范围是每臂相对于本次连接起点的各关节 ±2°（GUMI 手动控制的独立策略见下节），速度比例固定5%，反馈偏移超过3.5°拒绝继续发目标。目标须有六个有限角度且在实际查询限位内；整段目标先校验再插值，3秒去程/3秒回程及到位误差<=0.5°。不能通过连续小步无限累加；要运行模型2–4 cm的通用动作，需要在后续完成任务姿态和工作空间验收后修改这一阶段的边界，而不是反复重连规避它。连接不启动自主策略，退出保持最后有界位置目标；terminate_current_policy 也不等于物理急停。

原版 IK 固定 J6 ±120°与本机 ±180°不一致。本地连接查询每臂实际硬件限位，并以已连接各臂限位交集覆盖上游 IK 表；所有最终下发目标还由该臂实际限位再次检查。本机正运动学与反馈匹配选择 DH variant 1。该更新不改写固件限位或零点。

## 双腕观测

`cameras/orbbec_stream.cpp` 同时启动左 CC1365301R4、右 CC1365301HT 的 640×480 MJPG/15 fps 彩色流。本次 SDK 采集后 UVC /dev/video 节点不完整，因此使用原生 SDK，不依赖 V4L。Python LocalRGBCamera 仍满足上游 read() → (success, RGB uint8, None) 接口。SDK 流在临时目录保存带唯一帧号的JPEG，并原子发布文件名/主机单调到达时间；消费者拒绝超过1秒的帧及退出的发布进程。只保留约20帧，最后一个消费者释放时终止 SDK 发布并清理缓存。临时帧不是历史录像。

对应话题语义仍为 /camera_l/color/image_raw 与 /camera_r/color/image_raw，但本机没有启动 ROS。preflight 将两台同时保持运行约3秒，检查新帧推进并保存左右 JPEG；图像不是硬件同步帧对。原有 RGB-D 采样入口仍独立保留，但不要同时使用两种采集器争抢设备；实时适配层目前只消费彩色，depth 返回 None。

Orbbec 设备为两台腕部相机；当前前视为本机 D435i（见顶部当前配置）。未启用有效前视时 LocalRGBCamera 仍拒绝构造 /camera_f 观测，不用腕部或假图替代。preflight 的单臂 session 关闭其完整观测功能，腕部图单独验收，不声称完整 agentview+wrist 模型输入已经就绪。

其他 Python 入口可在加载上游模块之前绑定：

```python
import sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]  # 按调用脚本位置调整
sys.path[:0] = [str(root), str(root / 'Show-Harness'), str(root / 'show_harness')]
from local_backend import install
install()  # 默认只读，绑定单/双臂 session 的设备工厂
```

完整上游自主运行仍需前视观测（或有明确训练/提示契约的腕部专用观测改造）、每臂桌面Z下限、任务开始姿态、方向校验、夹爪与更大动作范围验收。当前上游默认自动home和夹爪命令会被适配层拒绝，不能直接启动默认机器人配置执行任务。

## 实机证据

- `results/show_harness/20261012-142222/report.json`：两臂经原版 PiperSession 连接、原版动作解释器 joint-stream 初始化成功；左右DH=1，位置误差约0.0048/0.0045 mm（这是与固件FK反馈的一致性，不是外部测量定位精度），姿态误差约0.002°以内；每腕约3秒读取27个不同帧，640×480 RGB。两臂维持使能/CAN模式/无故障。
- `results/show_harness/20261012-141703/report.json`：左臂J6目标误差0.008°，返回误差0.102°。
- `results/show_harness/20261012-141750/report.json`：右臂J6目标误差0.027°，返回误差0.079°。
- `test_backend.py`：14项检查，覆盖只读禁止发送、非有限/形状错误、累计位移、实际限位、坏目标零发送、过期反馈、其他控制器、示教不可自动接管、低速编码、禁止笛卡尔/夹爪/使能旁路、原生反馈单位转换与缺前视拒绝。原有控制工具的14项测试另行保留。

最初硬件适配验收未下载 VLM 权重、调用模型或进行抓取。后续本机模型安装与 GUMI 前视接入见 [MODELS.md](MODELS.md)；模型复用条件见 ../SHOW_HARNESS_REVIEW.md。

## GUMI 双臂手动控制（2026-10-12 更新）

当前入口 `bash show_harness/run.sh gumi` 默认绑定本机，远程通过顶部 SSH 转发访问。上游双臂页面、HTTP队列和Piper动作解释器复用，本地WristRig使用两台真实腕部图像，前视使用本机 D435i。启动不自动回位、使能机械臂或开合夹爪。

- 普通模式：选左右动作后COMMIT，每次执行一个动作对。平移 **3 mm**、旋转0.005 rad（约0.29°）、速度比例5%。
- 持续模式：勾选“持续控制模式”，按住方向按钮或键运动，松开停止；左W/S/A/D/Q/E，右I/K/J/L/U/O，也支持Z/X、N/M旋转。左右可同时按住。OS自动重复事件不会积压为指令队列。
- 夹爪：选GRASP（闭合）/RELEASE（默认打开100 mm），再COMMIT；手动、采集和单/双臂模型控制使用一致的打开目标。2026-10-22 通过只读 `ArmParamEnquiryAndConfig(4)` 查询，两臂 `0x47E` 返回 `max_range_config=100`，据此将原70 mm软件上限改为100 mm，并同步允许该范围反馈。不修改固件行程或零点，不以顶住机械硬限位来标定；尚未进行100 mm实机打开验收。启动不自动打开，不进行持续重复，不空夹后自动重开。原生CAN GripperCtrl仅控制夹爪，不联动机械臂。目标以最多1 mm/50 ms渐进更新，力矩上限0.5 N·m（SDK整数500），正常使能code=1、set_zero=0，故障时拒绝自动清除。检测反馈开度/使能/故障及收敛，闭合遇到物体可保持接触；UI的闭合标签不代表已抓到物体。

持续模式采用浏览器独占的活指令租约：每100 ms心跳、400 ms有效期，只有一个当前目标对，不积累动作。松开、窗口失焦、页面隐藏/离开或心跳过期时，中断后续路点并请求位置保持。序号拒绝晚到的旧心跳，方向改变使旧tick失效；第二个浏览器不能抢占有效租约。到限位、IK失败或反馈失效时暂停，需明确恢复。普通COMMIT不能插入正在持续执行的动作。

“暂停并保持位置”绕过浏览器串行请求队列，设置双臂中断标志并请求保持当前关节/已使能夹爪开度，不失能，不等于已验收的物理急停。“恢复控制”在中断动作结束后重同步，不补发旧动作；清空/重新同步按钮不回位。

**GUMI 已去除相对启动位置的±2°累计验收边界及整段 IK waypoint 角度门槛**。手控和模型都接受实际关节限位内的完整目标，先校验完整目标，再拆为相对上一下发目标≤1°的小步；目标领先当前实测关节≤2°，超过时等待跟随，超时拒绝。基础CLI验收接口仍保留原±2°限制。桌面未标定，以每臂本次启动的末端高度作为禁止下穿的临时保守下限。初始姿态接近关节限位，部分方向会被拒绝或截短。左右基座公共坐标未标定，不展示伪造的跨臂距离；训练录制仍关闭，不把缺前视观测保存为原版训练数据。

实机验证见 `results/show_harness/gumi-upgrade/gripper_validation.json`：左右夹爪各打开约3 mm并返回，开度反馈误差小于1 mm，驱动无故障，六关节反馈不变。页面、1 mm状态与夹爪/持续API已核对。28项后端保护测试通过；另用独立js-check-venv中的quickjs==1.19.4执行前端语法、双侧按住、单侧松开、释放、失焦和暂停拒绝检查（test_gumi_js.py）。持续断联停止时序由租约/前端测试验证，尚未做拔网物理停机实验。

机器缓存日志 ~/.cache/hackathon/gumi-local.log，环境与控制锁延续本地适配规则；服务占用控制锁时其他发送入口不能同时操作。

2026-10-12 连续控制范围修正：用户报告启动位置±2°导致持续按住被拒绝，已将 GUMI 的 TeleopRobot 改为上述路点/跟随/硬件限位保护；未放大硬件限位，也未开放自动轨迹。新增测试验证累计超过启动2°仍可正常下发、路点跳变/跟随落后/硬件越限零发送；总32项测试通过。切换时暂停原服务，不发送回位；本轮启动通过 --floor-left 0.196 --floor-right 0.1815 沿用先前观测保存的高度参考（原状态精度0.1 mm，非桌面标定）。

2026-10-12 用户确认GUMI操作良好，要求平移step改为3 mm；已同步控制器、API状态、元数据与页面。速度5%、关节路点/跟随保护及心跳停止机制保留。

2026-10-12 用户要求放宽3 mm步长触发的路点跳变保护：当时相邻关节目标上限改为1°，跟随领先2°与真实限位继续检查。33项测试通过。

2026-10-13 按用户要求移除整段 waypoint 角度拒绝门槛，包括手控1°及模型15°。完整硬件限位检查、每次下发≤1°拆分、2°实测跟随领先、暂停/故障/反馈过期和模型20秒截止继续保留。98项假硬件测试通过，覆盖1.201°手控目标、30.1°目标拆分与跟随，以及完整目标超硬限位时零发送；这些测试不代表大角度实机动作验收。

## 示教初始姿态（2026-10-12）

用户手动示教的新init已保存到 `init_pose.json`，包含左右六关节deg/rad、原生末端位置m/RPY角度/xyzw四元数、夹爪开度、原始CAN帧和捕获时间。`site_piper_arms.yaml`提供上游兼容的 `begin_pose: init`、每臂 `poses.init`；自动home/回位关闭，左右各自记录、不自动镜像。`results/show_harness/init-pose/capture.json`保存本轮原始快照；模式切换记录见同目录takeover.json。

本次左关节deg `[64.593,46.782,-19.811,-11.833,41.729,-93.950]`，位置m `[0.061115,0.099721,0.201408]`；右关节deg `[-84.593,43.778,-13.737,40.937,54.343,100.830]`，位置m `[0.056119,-0.078757,0.186969]`。这是初始任务姿态，不是新的桌面Z标定；仍使用之前的高度参考。

旧 `teaching recovery posture required` 是基础commissioning工具按官方轨迹回放示例采用的保守恢复窗口，不能作为本次用户指定的任意init姿态的通用约束。GUMI增加显式接管入口：

```bash
bash show_harness/run.sh gumi --adopt-init --floor-left 0.196 --floor-right 0.1815
```

该入口要求保存init与实测关节偏差≤0.2°、连续静止检查≤0.1°、示教记录结束、六电机已使能、无故障/其他发送者、六路实际硬件限位有效。先进入待机并检查模式回执；预置当下测量关节目标后以速度0进入CAN；再次确认模式、位姿，再设速度5%保持当前测量目标。不发送Reset、Enable、失能、清错误、夹爪、回位或示教回放指令。若固件拒绝转换，立即中止，无Reset回退。原基础CLI恢复窗仍保留，不把绕过窗口泛化到未验证设备或固件。

本机S-V1.8-5两臂实际转换成功，ctrl_mode 2→1，六电机使能/无故障，关节最大偏移左0.155°、右0.134°，小于接管检查0.2°。GUMI已恢复，默认3 mm、速度5%及持续控制保护维持。37项保护测试通过，新增覆盖任意限位内init接管、示教未结束/其他控制器拒绝、保存姿态不符零发送、固件拒绝不Reset。

参考固定官方SDK的MotionCtrl_2/JointCtrl定义；官方回放恢复窗口来源与适用范围见父级README的历史记录。显式当前位姿接管的适用证据为本机此次实测，不能视为所有Piper固件通用认证。

## 本机模型与 GUMI 前视（2026-10-12）

新增 `bash show_harness/models.sh` 入口，按 16376 MiB 显存配置 Qwen3.5 0.8B / 2B / 4B 与官方控制 LoRA，默认 2B，另含 2B 仿真 LoRA。版本、模型缓存、服务、离线图像验证与结果范围见 [MODELS.md](MODELS.md)。

GUMI 页面新增浏览器 webcam 前视启停和“仅推理预览”。相机需要用户点击并授权；使用打开网页的电脑的摄像头，前视与所选侧腕图共同输入模型。前视的采集 ticket、序号和 video mediaTime 验证拒绝过期/重复帧，页面隐藏或关闭即释放来源，模型 snapshot 缺图返回503。预览入口不发送机械臂/夹爪动作；训练录制继续关闭，视角、方向和工作空间仍需独立验收。

## 2B 真机模型执行（2026-10-12）

现已接入“2B 推理并执行一步”和“2B 连续运行”，按上游单臂 FT 的 v3 提示、图像顺序、20 mm 步长、平滑插值和 Piper 前后 token 换向执行。网页选择一侧机械臂；运行与手控互斥，暂停/断流/心跳失效会取消待执行结果。启动不自动回位或张爪。运行方法、现场参考下限、与上游差异及证据位置见 [LIVE_CONTROL.md](LIVE_CONTROL.md)，配置见 `live_piper.yaml`。


### 2026-10-12 恢复浏览器前视，保留内网服务

按用户最新决定，前视恢复为仅 `browser_webcam`：点击“使用浏览器摄像头”并授权，使用打开该网页的电脑的摄像头，可来自 Mac、alien 或其他访问端。后台不再打开 alien 内置相机，已移除本机采集模块、切换按钮和 `--front-device` 参数。停止采集、页面隐藏或帧过期后前视不可用，继续拒绝将假图提供给模型。

内网服务保留：Wi-Fi `http://10.24.72.109:8620/`，有线 `http://192.168.123.99:8620/`。内网 HTTP 可以查看两腕和已上传的前视；启用访问端 webcam 需要 localhost 或 HTTPS。Mac/其他内网电脑可运行 `ssh -N -L 8620:127.0.0.1:8620 hokei@10.24.72.109`，再打开 `http://127.0.0.1:8620/`；已有到 alien 的同端口转发可继续使用。

当前用用户级临时 systemd 服务 `gumi-local.service` 托管，终端退出不影响运行；未配置开机自动启动机器人接口。查看：`systemctl --user status gumi-local`，日志：`journalctl --user -u gumi-local`。重启：`systemctl --user restart gumi-local`（仅在没有执行动作时）。网页启动只读取机械臂状态并查询限位；实际动作入口仍要求 CAN 控制模式，不由网页启动自动接管。

此前 alien RGB 曾短时出图，随后独占 OpenCV 和浏览器采集均未恢复，根因未确定。该排障记录保留在本机缓存 `~/.cache/hackathon/gumi-verification/front-unavailable.json`，不代表当前浏览器来源已验收；恢复浏览器模式不修复 alien 相机硬件/驱动故障。相机权限规则保留以供 alien 浏览器访问，但不再有后台相机采集进程。
