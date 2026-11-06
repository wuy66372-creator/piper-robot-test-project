# Show-Harness GUMI：遥操、采集、LoRA 微调与在线推理

本项目将 Show-Harness 的视觉动作模型接入本地 Piper 双臂，在同一个 GUMI 网页中完成 **手动遥操 → 成功示教采集 → LoRA 微调 → 在线推理与执行**。硬件层使用原生 SocketCAN、官方 Piper SDK 和相机 SDK，无需 ROS。

当前部署面向 Linux alien：Piper 双臂、Intel RealSense D435i 主视角、两台 Orbbec 腕部相机，以及约 16 GB 显存的 NVIDIA GPU。模型支持 Qwen3.5 0.8B / 2B / 4B、官方单臂动作 LoRA 和本项目训练的单臂 / 双臂 LoRA。

本文按 `show_harness/` 的后续更新整理。旧说明中的“仅浏览器前视”“训练录制关闭”“固定 2B 执行”和“空夹自动重开”属于历史配置；当前默认使用 D435i，开放采集与训练，支持模型切换，关闭空夹自动重开。

## 项目拉起命令

以下命令均在**仓库根目录**执行。本仓库根目录就是原 Hackathon 项目的 `robot/`，不需要再进入一层 `robot/`。

### 1. 首次恢复源码与环境

运行主机需要 Linux x86_64、`/usr/bin/python3.12`、uv、Git、g++、可用的 NVIDIA 驱动与 CUDA 运行环境。相机需配置设备权限；两路 SocketCAN 需已启用为 1 Mbps，并按设备序列号核对左右映射。安装脚本沿用已有机器缓存布局，迁移到新机器时需要逐项检查这些条件。

```bash
git clone https://github.com/wuy66372-creator/show-harness-project.git
cd show-harness-project

# 上游源码单独恢复，版本检查要求精确匹配此提交
git clone https://github.com/showlab/Show-Harness.git Show-Harness
git -C Show-Harness checkout c3a133eb111a8100bcb64509b1ea891fdcee1532

# 控制与相机环境
bash show_harness/run.sh setup
python3 cameras/setup.py

# 模型推理环境、固定版本基座与官方 LoRA
bash show_harness/models.sh setup
bash show_harness/models.sh download
bash show_harness/models.sh verify

# 需要微调时再准备独立训练环境；先完成上面的推理环境安装
bash show_harness/training_setup.sh
```

`download` 会下载锁定的多种基座与适配器，需要网络和足够磁盘空间；`verify` 不重新下载权重，但仍访问 Hugging Face 元数据。环境、SDK 和模型缓存默认在 `${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/`。训练脚本还要求 `~/.local/bin/uv` 可用。

### 2. 日常启动

先确认设备映射、相机权限与 CAN 状态；启动 GUMI 本身不会使能、回位或开合夹爪。

```bash
# 查询硬件与腕部相机，不执行运动
bash show_harness/run.sh preflight

# 启动本地模型服务：默认也可省略 qwen3_5_2b
bash show_harness/models.sh start qwen3_5_2b
bash show_harness/models.sh status

# 前台启动 GUMI：默认 D435i 主视角，网页端口 8620
bash show_harness/run.sh gumi --host 127.0.0.1 \
  --front-source realsense --front-serial 347622075687 \
  --floor-left 0.196 --floor-right 0.1815
```

`347622075687` 是既有 D435i 的序列号；更换设备应改为实际序列号，或省略 `--front-serial` 自动识别唯一一台相机。上面的左右高度值是既有现场保存的临时参考，**不是通用桌面标定值**；换工位必须重新核对并填写。命令行未显式指定高度时，代码默认两侧均为 `0.05 m`，不能把默认值当作现场标定。

主机本地打开：

- GUMI 遥操与推理：<http://127.0.0.1:8620/>
- 数据与训练工作台：<http://127.0.0.1:8620/training>
- 模型 API：`http://127.0.0.1:8000/v1`

远程电脑另开终端建立 SSH 转发，再访问同样的 localhost 网页：

```bash
# 替换为实际 SSH 用户及主机
ssh -N -L 127.0.0.1:8620:127.0.0.1:8620 robot_user@robot_host
```

既有部署曾由用户级 `gumi-local.service` 托管。若该服务已经运行，应使用已有实例，避免重复打开设备；在无动作执行时管理服务：

```bash
systemctl --user status gumi-local
journalctl --user -u gumi-local -f
systemctl --user restart gumi-local
```

这些命令仅适用于已配置该 unit 的主机，克隆仓库不会自动安装服务。停止模型服务使用 `bash show_harness/models.sh stop`。

### 3. 可选浏览器前视

```bash
bash show_harness/run.sh gumi --host 127.0.0.1 --front-source browser \
  --floor-left 0.196 --floor-right 0.1815
```

浏览器模式下，在 localhost 或 HTTPS 网页点击“使用浏览器摄像头”并授权，图像来自打开网页的电脑。默认 RealSense 模式无需浏览器摄像头授权，浏览器上传不能覆盖 D435i 主视角。相机独占诊断需先停止 GUMI，入口见 [相机与本地适配说明](show_harness/README.md)。

## GUMI 遥操

在 `/` 查看三路图像和左右臂状态，选择动作后 COMMIT，或使用持续控制。普通手控平移为 **3 mm**、旋转为 **0.005 rad**，用于摆位；模型和录制平移为 **20 mm**，两者不能混用。

| 操作 | 行为 |
| --- | --- |
| 普通 COMMIT | 执行所选左右动作对 |
| 持续控制 | 按住方向按钮 / 键运动，松开停止后续路点；左 W/S/A/D/Q/E，右 I/K/J/L/U/O |
| GRASP / RELEASE | 选择夹爪动作后 COMMIT；显式 RELEASE 请求打开，默认目标 100 mm |
| 暂停并保持位置 | 中断后续路点并请求位置保持；不失能 |
| 恢复控制 | 结束中断后重新同步，不补发旧动作、不自动回位 |

持续手控使用独占租约、100 ms 心跳和 400 ms 有效期，不积累键盘 repeat 指令。窗口失焦、页面隐藏或断连会停止后续动作。控制检查真实关节限位、反馈、故障与高度下限；完整目标校验后拆为不超过 1° 的关节小步，目标领先实测关节不超过 2°。

动作要求两臂状态满足对应控制检查：已使能、CAN 控制模式、反馈健康。网页启动不自动接管示教模式。已有姿态接管的适用条件及 `--adopt-init` 说明见 [本地适配 README](show_harness/README.md)。

夹爪开度和 GRASP 标签不能证明抓住物体。当前默认关闭基于小于 5 mm 开度的自动重开，以支持薄物体；100 mm 上限来自固件查询，完整行程尚未实机验收。网页暂停是软件位置保持，不能替代现场物理急停。

## 采集 2 cm 成功示教

1. 在 GUMI 顶部填写并保存采集任务，确认两臂与三路相机可用，点击“开始采集 · 2 cm”。
2. 默认按住方向连续采集，每个完整 20 mm 动作单独记录，未动作侧为 STILL；也可取消持续模式，选择动作后 COMMIT。夹爪动作仍需 COMMIT，旋转不录制。
3. 松开 / 换向后，当前完整 20 mm 动作结束才停止 / 换向；暂停、失焦或心跳超时可中断当前动作，中断步标记失败。
4. 完成任务后点击“完成任务并保存成功示教”；失败或中断选择“结束并保存未成功轨迹”。保存不会回位、张爪或失能。

录制与推理使用同款 20 mm 控制器：wrist frame、level orientation、20 段 × 50 ms 平滑插值。每步先保存 256×256 letterbox 主视角和左右腕图，再记录动作结果及实际位移。平移实测偏离 20 mm 超过 3 mm，或动作拒绝 / 中断，均不能作为成功步。采集期间拒绝模型执行。

数据位于 `data/teleop/rollout_NNN/`：

```text
agentview/             # 动作前主视角
wrist_left/            # 动作前左腕图
wrist_right/           # 动作前右腕图
actions.jsonl          # 动作、执行状态、实际位移与夹爪状态
metadata.json          # 任务、动作契约、相机来源、人工成功标记
visualization.mp4
terminal/              # 成功保存时的真实动作后终态三图
```

只有完整、人工确认成功、无失败步的 20 mm 示教可训练。空轨迹、未成功和服务中断后的 interrupted 轨迹不纳入训练；模型运行日志不作为人工示教。

## LoRA 微调

打开 `/training`，逐步预览轨迹、修改已结束轨迹的任务、归档 / 恢复并选择成功示教，再选择基座、单臂 / 双臂契约和训练起点。

| 契约 | 输入图像顺序 | 标签与执行约定 |
| --- | --- | --- |
| 左 / 右单臂 v3 | 主视角 → 所选腕图 | 仅使用所选侧示教；训练转换交换 FWD/BACK，推理执行边界交换一次 |
| 双臂 v4 once | 主视角 → 左腕 → 右腕 | 严格左右动作对，不交换 FWD/BACK；STILL 侧不发送动作 |

训练使用固定版 LLaMA-Factory，基座为 Qwen3.5 0.8B / 2B / 4B。约 16 GB 显存配置使用 batch=1、梯度累积 4、gradient checkpointing 和 SDPA；4B 要求 4-bit QLoRA。建议先用 0.8B / 2B 验证流程。

- **从原始基座训练**：冻结视觉编码器与投影器，训练语言 attention 的 q_proj/v_proj。
- **从上游动作 LoRA 续训**：默认选择匹配基座的单臂 v3 LoRA，保留原 rank 64、alpha 和全部目标模块，默认学习率 `2e-5`；不恢复旧优化器。双臂 v4 不静默套用单臂续训权重。
- **抓取加权**：`grasp_repeat` 默认 5，可选 1–20。含独立 GRASP token 的完整样本总计保留 N 份，双臂同时抓取不平方；不修改原始示教。当前没有自动划分独立验证集。
- **夹爪状态输入**：新训练使用动作前实测宽度与本 episode 上次成功完成的夹爪命令；每次 episode / run 从 unknown 开始。旧适配器按原契约推理，不静默改 prompt。

开始训练会复制不可变数据快照、暂停控制、卸载本项目管理的推理服务，再启动独立训练进程。训练期间拒绝采集、机器人动作、推理和模型切换。可查看日志、loss / step，取消并保留检查点。结束或取消后恢复原模型，控制继续暂停，新 LoRA 需人工选择和加载。

作业保存在 `data/lora_jobs/job_<时间>_<编号>/`，包含数据快照、`dataset/`、`train.yaml`、`train.log`、`job.json` 和 `adapter/`。`gumi_contract.json` 记录步长、提示版本、方向及夹爪输入契约；`sampling.json` 记录加权前后统计。完成作业可下载 `adapter.zip`，训练使用本地数据，不上传数据或启用 W&B。

## 在线推理与执行

1. 在 GUMI 选择官方模型或通过完整性 / 基座 / 契约校验的自训 LoRA，点击“加载所选模型”。训练页也提供“在 GUMI 中选择此 LoRA”入口。
2. 等待加载完成，确认三路相机状态，选择与适配器匹配的左 / 右单臂或“双臂 · v4 once”，填写任务。
3. 先点“仅推理预览”，检查原始 token 和执行方向；该入口不发送机械臂或夹爪动作。
4. 恢复控制后，执行一步或连续运行。连续运行默认最多 20 步，可设 1–150 步；每步重新采图、推理并执行。
5. 用“停止模型并保持”或“暂停并保持位置”结束运行。恢复连接或加载完成均不会自动续跑。

模型切换会暂停控制、结束旧运行并释放旧服务；加载期间禁止动作与新推理。单臂使用主视角与所选腕图、v3 提示和最近移动历史，仅在执行边界交换 MV_FWD / MV_BACK；双臂 v4 使用三图并行执行动作对，只有 `DONE DONE` 结束。新 state-aware 双臂契约同时按上游约定记录等待侧 STILL 历史。

非法 token、混合 DONE / 运动、图像过期、推理后姿态变化、心跳失效、反馈或限位错误都会拒绝 / 停止执行。被工作空间截断且实际位移不足 1 mm 时停止，避免重复零位移动作。模型输出 DONE 只表示模型报告完成，任务成功需要现场确认。

默认主视角为 D435i RGB 640×480 / 15 fps，不镜像；主视角帧龄上限 0.8 秒，两腕拒绝过期帧，不用占位图替代模型观测。三路图像没有硬件同步，默认推理不依赖深度或 IMU。

运行日志位于 `results/show_harness/live/`，保留输入图像、提示、原始 token、执行映射、反馈和停止原因。它们用于排查执行与方向问题，不等于成功率评测。

## 恢复与验证边界

仓库保存适配代码、精选证据及部分模型归档，不包含完整上游源码、全部原始示教、环境缓存和所有权重。四个约 519.5 MB 的 rank-64 权重仍在 alien；仅克隆仓库不能恢复这些自训模型，需按 [模型归档清单](MODEL_ARCHIVE.md) 取回并核对哈希。

```bash
# 固定上游与环境准备完成后运行回归测试
bash show_harness/run.sh test

# 模型已启动且对应 profile 匹配时，检查保存图片推理
bash show_harness/models.sh check --profile qwen3_5_2b
```

既有证据包括 Piper 接口和小幅关节运动、三路 RGB、0.8B / 2B / 4B 图像推理，以及隔离合成数据的 0.8B 训练、续训和双臂加载链路。它们不能证明新任务抓取成功率或 2B / 4B 微调已实测。2026-10-22 归档检查有一项 HTTP 连接复用测试间歇报错，详情见下方 checkpoint；不能将历史测试描述为全部通过。

## 详细文档

| 文档 | 内容 |
| --- | --- |
| [本地适配与 GUMI](show_harness/README.md) | 相机、CAN、姿态接管、遥操与硬件证据 |
| [采集与训练](show_harness/COLLECTION_TRAINING.md) | 数据格式、训练契约、GRASP 加权、夹爪状态、自训加载 |
| [模型安装](show_harness/MODELS.md) | 权重、依赖、服务与历史浏览器前视配置 |
| [模型切换](show_harness/MODEL_SWITCH.md) | 后台切换、加载状态与互斥 |
| [在线执行](show_harness/LIVE_CONTROL.md) | 单臂执行路径、停止条件与日志；历史相机 / 自动释放配置以本文后续更新为准 |
| [方向审计](show_harness/DIRECTION_AUDIT.md) | 单臂 FWD/BACK 映射与零位移停止 |
| [薄物体夹持](show_harness/GRIPPER_THIN_OBJECTS.md) | 关闭默认空夹自动重开 |
| [归档检查](show_harness/GIT_CHECKPOINT_20261022.md) | 测试实际范围与已知间歇失败 |
| [模型归档边界](MODEL_ARCHIVE.md) | 已入库和需从 alien 恢复的模型文件 |
