# Show-Harness 与现场双 Piper 的适配检查

检查日期：2026-10-12。独立 Git 仓库已克隆到本文件同目录的 `Show-Harness/`，origin 为 https://github.com/showlab/Show-Harness.git，当前 main 提交 `c3a133eb111a8100bcb64509b1ea891fdcee1532`（2026-10-10）。保留上游原始工作树，本轮仅审阅，没有安装环境、下载模型权重或启动硬件脚本。

**结论：有直接对应的 AgileX Piper 单臂/双臂实现，应该复用 Piper 分支，不需要借用 Franka 或 SO-101 的机械模型。** 已有实现不等于本机即插即用；接口、标定和模型输入契约仍需适配。

## 直接对应的代码

| 路径（相对 Show-Harness） | 已核实内容 |
| --- | --- |
| `configs/robot_piper.yaml` | Piper 零样本 VLM 控制；单臂与双臂共用配置 |
| `configs/robot_piper_ft.yaml` | Piper 微调策略，含本地服务后端与 token 交换配置 |
| `configs/site/piper_arms.yaml.example` | 每臂相机话题、桌面 Z 下限、开始/停放姿态；关节单位 rad |
| `configs/primitives_piper.yaml` | 离散移动、夹持等动作的设备语义 |
| `core/piper/piper_interface.py` | Piper ROS 话题接口、反馈新鲜度与命令订阅检查 |
| `core/piper/kinematics.py` | 六自由度 Piper DH 正运动学、DLS 逆运动学、两种固件偏置 DH 表 |
| `interpreters/piper_atomic_controller.py` | 默认 50 Hz 关节路点流，支持末端位置控制备选路径 |
| `core/piper/piper_session.py`, `dual_session.py`, `ros_camera.py` | 单/双臂状态与前视/腕部 RGB 观测 |
| `scripts/run_real.py`, `run_real_dual.py` | 零样本单臂/双臂运行入口 |
| `scripts/run_real_mvtoken.py`, `run_real_dual_mvtoken.py` | 微调动作 token 运行入口 |
| `gumi/` | 浏览器示教及单/双臂采集 |
| `docs/piper.md`, `docs/finetuned.md`, `models/README.md` | 硬件接入与模型部署契约 |

仓库未包含 Piper URDF/MJCF 资产；其 Piper 机械模型在 Python DH/IK 中。另有 Franka 实机、ManiSkill/RoboLab/LIBERO 仿真，以及 SO-101 Isaac Lab Workshop（当前为仿真）配置。这些可参考编排，不应替代本机 Piper 几何或关节限位。

## 与本机已有设备的差异

| 项目 | 上游参考配置 | alien 已验收状态 / 必须适配 |
| --- | --- | --- |
| 控制接口 | ROS Noetic + cobot_magic 的 Piper 节点 | Ubuntu 24.04，未发现 /opt/ros；现有直接 SocketCAN/官方 SDK 控制。需补 ROS 桥或实现同接口的本地 CAN 后端 |
| CAN | can_left/can_right，1 Mbps；启动脚本硬编码作者适配器序列号 | can0 左、can1 右，1 Mbps；必须用本机序列号映射，不能原样执行上游重命名脚本 |
| 左 CAN 身份 | 作者设备 | 0040004B4759530820353131 |
| 右 CAN 身份 | 作者设备 | 0039002C4759530920353131 |
| 相机 | 三台 Orbbec DaBai DC，经 astra_camera 发布前视与左右腕 RGB | 已接左腕 CC1365301R4、右腕 CC1365301HT（DaBai DC1）；目前为 SDK 采样，需连续图像接口。默认前视话题尚未接入 |
| 深度 | 当前 session 消费 RGB；RosImageCamera.read 的 depth 为 None | 我们的深度已验收，但接入标准 VLM 观测不要求先做 RGB-D 对齐。要用于定位/避障，另需对齐与标定 |
| 起始姿态 | 示例零值或作者姿态，初始化自动移到 begin/home | 本机刚验收姿态不能视为任务开始姿态；需逐臂记录，无依据自动镜像 |
| 桌面下限 | 按每臂 z_floor_m 标定；微调单臂还有顶层参考值 | 未采集本机桌面 Z 下限，不能照抄作者数值 |
| 运动学 | 通过 FK 与末端反馈比较选 DH 偏置；固定关节限位 | 固件 S-V1.8-5；需实测 DH 与末端反馈匹配，读取实际限位 |
| J6 限位 | kin JOINT_LIMITS_RAD 约 ±120° | 现有原始查询记录为 ±180°，右臂当前约 +127.555°，超出上游 IK 范围；需处理当前姿态与软件限位契约，不能径直套用 |
| 动作尺度/速度 | 常用单步 2–4 cm；ROS 关节流按注释由节点映射速度 100 | 本机只完成速度比例5%的 J6 +1°/返回；上述运行参数未验收 |

上游 run_arm.sh 默认启动两臂并使能、闭合两个夹爪，配置默认自动移动到 begin/home。此次没有运行这些脚本。Z 下限等已有保护不构成双臂碰撞规划验收；需延续本机实际限幅、反馈时效和停止要求。

## 可以套用什么“模型”

1. **机械模型：**优先用现成 Piper DH/IK，不用 Franka 或五自由度 SO-101 代替；通过本机末端姿态反馈确认偏置、工具长度、实际限位后再接动作。
2. **零样本 VLM：**robot_piper.yaml 支持托管 VLM；无需先训练 Piper 专属关节轨迹。动作 token 仍必须经过本机 Piper 执行器落地，不代表模型可直接输出任意 CAN 动作。
3. **已发布微调权重：**作者提供 Qwen3.5-0.8B/2B/4B/9B 与 Gemma-4-E4B 的真实机器人 LoRA（另有仿真专用 2B）。真实策略按文档由 Franka + AgileX 单臂数据共训；可作为本机 Piper 单臂试验候选，需要对应基础模型，Git 克隆没有带下权重。2B 可作为首轮资源测试候选，但本轮未测显存、延迟或本机任务成功率。
4. **输入/方向契约：**发布的真实适配器是 v3 单臂格式，必须使用训练一致的 prompt/chat template；在 Piper 参考视角下，通过 execution_token_swap 交换 MV_FWD/MV_BACK。必须核对本机相机朝向和 wrist motion frame，不能凭字段名字猜方向。
5. **双臂策略：**代码支持零样本双臂以及 v4 微调双臂 once/twice/chain 格式；已发布的单臂权重不能直接视作联合双臂权重。双臂可先试零样本入口；若要联合微调策略，需对应格式的示教与训练。

注意文档存在契约差异：docs/piper.md 的微调例子仍写 v4 --piper，但 docs/finetuned.md 和 models/README.md 明确所有发布适配器使用 v3。复用发布权重时以实际训练契约与模型配置为准，不能盲抄前者命令。

## 建议的接入顺序（尚未执行）

先保留既有 CAN 与 Orbbec 驱动，补本地 PiperInterface/Camera 兼容后端；若选择完整上游 ROS 路线，则先独立解决 Noetic/cobot_magic 环境，不直接改当前系统控制链。先单臂只读输出与 mock 动作流程，再补真实前视相机或明确改造观测格式，逐臂记录开始/停放姿态、桌面 Z 下限和方向。验证本机限位、FK/IK 与小幅动作后，再进行单臂模型闭环；双臂联合动作另行验收。

源码复用依据上游 Apache-2.0；基础模型各自许可需随具体模型核对。原始源码保持在独立克隆中，父项目仅保存此审阅记录与固定提交信息，不将嵌套 .git 当作源码备份。

## 阅读范围与来源

阅读了上述配置、core/piper、Piper/Real 动作解释器关键路径、Piper 启动脚本、硬件与微调说明、requirements-real 和模型 README；未审查完整训练流程或执行上游测试。检查来自源码，不是本项目 Show-Harness 实机成功证据。

- [固定源码版本](https://github.com/showlab/Show-Harness/tree/c3a133eb111a8100bcb64509b1ea891fdcee1532)
- [Piper runbook](https://github.com/showlab/Show-Harness/blob/c3a133eb111a8100bcb64509b1ea891fdcee1532/docs/piper.md)
- [Piper 零样本配置](https://github.com/showlab/Show-Harness/blob/c3a133eb111a8100bcb64509b1ea891fdcee1532/configs/robot_piper.yaml)
- [Piper 运动学](https://github.com/showlab/Show-Harness/blob/c3a133eb111a8100bcb64509b1ea891fdcee1532/core/piper/kinematics.py)
- [微调契约](https://github.com/showlab/Show-Harness/blob/c3a133eb111a8100bcb64509b1ea891fdcee1532/docs/finetuned.md)
- [发布模型](https://huggingface.co/showlab/Show-Harness-VLMs)
- 本机证据：CONNECTION_STATUS.md、results/can1_supported_limit_restore.json、results/cameras/20261012-135640/validation.json。
