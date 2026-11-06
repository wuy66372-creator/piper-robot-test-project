# AGILE X Piper 本机接口

运行主机为 Linux alien。用户已确认 `can0` 是左臂；2026-10-12 13:12 两路六关节使能位均为 true。具体 Piper 产品变体仍未从铭牌确认。

**13:44 最新状态：左臂 can0、右臂 can1 均已成功由 CAN 独立接管，复原到配置限位内，并分别通过 J6 +1°/返回的实机关节反馈验收；两臂保持使能、CAN 控制，无主控故障。**

## 环境与入口

源码路径按脚本位置解析。官方 Piper SDK 固定到提交 `c9e8a28174e71eeaac448593cb65f8ab258a92fe`（0.6.2），机器本地环境位于 `${XDG_CACHE_HOME:-$HOME/.cache}/hackathon/piper-venv`，不在项目或移动 SSD 上建立 `.venv`。首次安装需要系统 Python 3.12、uv、Git 和网络。

在本目录运行：

```bash
bash run.sh setup
bash run.sh status --can can0
bash run.sh status --can can1
bash run.sh firmware --can can0
bash run.sh limits --can can0
bash run.sh test
```

`status` 只接收报文；`firmware` 和 `limits` 发送查询报文。`ok=true` 在 status 中只表示收到 CAN 数据，需检查 `standard_feedback` 才能判断标准状态是否完整。SDK 连接时禁用自动 Piper 初始化和接收线程，原生 SocketCAN 接收逐条记录反馈与到达时间；SDK 用于编码发送官方查询/控制指令。

CAN 接口需为 UP、1000000 bit/s。本轮已启用；重启或重新插拔后需重新检查。控制前会核对当前接口配置的适配器序列号：左臂 can0=`0040004B4759530820353131`，右臂 can1=`0039002C4759530920353131`。接口名变化或序列号错配时拒绝控制，需重新核对物理映射。普通 CLI 不改变驱动器限位、零点或 CAN 报文映射。

## 控制入口与实际验收边界

下面两条默认仅读取、查询限位并校验计划；只有显式增加 `--execute` 才发送控制指令。运动指令要求现场机械臂固定、空间腾空且夹爪无负载，并满足反馈和限位检查。

```bash
bash run.sh hold --can can0
bash run.sh jog --can can0 --joint 6 --delta 1 --speed 5
```

动作幅度最多 2°、速度比例 1–5%，jog 到达后回到本次起始关节角。执行前要求全部电机已使能，不自动使能或恢复急停。从按键示教切换时要求结束记录、灯灭，并满足官方示例的恢复姿态窗口（J2/J3 绝对值小于约 10°，J5 约 12°～45°）及本机六关节限位，随后依次发送待机、速度比例 0 的 CAN 关节控制，再以最新反馈位置作为目标；该控制顺序尚未进行实机动作验收。成功后机械臂保持 CAN 关节控制与使能状态，不自动失能或恢复示教。初始 1 秒保持、3 秒去程、3 秒回程，反馈误差必须小于等于 0.5°。

```bash
bash run.sh jog --can can0 --joint 6 --delta 1 --speed 5 --execute --output results/can0_jog_execution.json
```

任一关节反馈段超过 150 ms 或驱动器反馈超过 300 ms，驱动器/主控故障、缺少六路硬件限位、目标超限、另一控制源的总线报文、接管前姿态变化或移动超过起始位置 3.5°，均拒绝/中止动作。动作中故障或中断时发送软件急停；这不能替代设备物理急停，也无法在进程被强制杀死或 CAN 链路断开时保证停机。本工具互斥锁只协调本工具的实例，无法排除其他本机程序在运行。

显式软件急停入口：

```bash
bash run.sh stop --can can0 --execute
```

该指令改变设备状态，只用于停止需求；工具检查主控是否反馈急停状态，不自动恢复。

### 2026-10-12 13:12–13:31 初期查询与拦截

- 状态接收、SDK 固件查询、两路六关节限位查询：实机通过。
- 左臂周期状态约 200 Hz，控制模式为示教，主控无故障码。
- 限位缓存：官方 SDK 本轮返回了六份相同的最后一个关节数据，因此本工具以 `0x473` 报文关节编号独立保存六路限位；已验证六路各自响应。
- 左臂 jog 预检查被拦截，未发送运动或模式切换报文：J2=-1.542°、J3=+0.868°、J5=75.519°，超出设备返回的相应限位。这可能涉及示教姿态或零点，不能据此自动修改校准。
- `can1` 使用 `0x320–0x327` 周期主控报文，本次官方标准 V2 解码未覆盖它。支持接收、固件和限位查询；控制入口拒绝该适配器，待核对协议。
- 9 项本地测试通过：过期/缺失反馈、故障、失能、超限、非有限数、速度/幅度边界、限位保存、未知协议、拒绝发送及示教退出/零位接管条件。
- 尚未完成控制模式切换、关节运动及急停实机验收；下一步核对左臂姿态/零点并将反馈置于限位范围内，再进行空载小幅动作。

机器反馈见 [results](results/)，原始连接检查见 [CONNECTION_STATUS.md](CONNECTION_STATUS.md)。

### 13:36–13:37 已授权的支撑状态恢复与动作验收

用户明确要求通过 CAN 接管复原，并确认机械臂已在重力下自然躺下、有支撑且安全。本次据此执行 `ResetPiper()`（0x150 / 0200000000000000），机械臂从示教转入待机，六关节失能，未修改零点或限位。这个操作不能在悬空或负载姿态下自动复用。

随后查询六路限位，在失能/待机状态准备最近的限位内目标（边界留 0.5°），以速度比例 5% 设置 CAN 关节控制、发送目标并使能；监测反馈、故障及最大偏移。最大修正为 J5 的约 6.1°，其余目标为 J2=+0.5°、J3=-0.5°，J1/J4/J6 保持当前值。实际关节反馈到位，六关节使能，ctrl_mode=1、arm_status=0、fault_code=0。没有发送全零或更大范围的回零轨迹。

`supported_restore.py` 保存这次支撑状态下的受限恢复逻辑。它要求现场支撑确认与执行标志、已处于失能待机，不自行 Reset；单关节最大修正 8°，观测偏移超过 9°、故障、反馈失效或超时会发送急停。未满足这些特殊前置条件时，继续使用普通控制入口的检查，不能以此绕过常规限位。

```bash
bash run.sh restore --supported --execute --output results/supported_restore_receipt.json
```

之后普通 `jog --joint 6 --delta 1 --speed 5 --execute` 实机通过：起点 -90.243°，目标 -89.243°，实际 -89.290°（误差 0.047°），返回 -90.203°（误差 0.040°）。该结论来自关节反馈；未单独进行视觉位姿或抓取验收。最后六关节反馈为 `[80.975, 0.405, -0.429, -70.712, 69.566, -90.203]` 度，维持 CAN 控制和电机使能。软件急停的实际触发验收仍未进行。

原始证据：`results/can0_requested_reset.json`、`results/can0_supported_limit_restore.json`、`results/can0_j6_execution.json`。12 项本地测试通过。左臂接口控制已跑通；can1 的协议适配、右侧实体对应及动作验收仍未完成。

### 13:41–13:44 右臂接管通过

用户明确要求接管右臂，并确认右臂自然躺下、空载、有支撑且周围安全。右臂原先使用 0x320–0x327；仅发送 `MasterSlaveConfig(0,0,0,0)` 有应答但未改变周期报文编号。在支撑状态下 Reset 后，驱动器反馈确认六关节失能；再发送官方 `MasterSlaveConfig(0xFC,0,0,0)`，将右臂设为运动输出臂、恢复默认偏移，周期报文切为标准 0x2A1–0x2A8，字节内容与此前记录一致。该配置改变了右臂的角色及反馈编号，不是只读操作；重启后的保持情况尚未验证。左臂未发送配置或控制指令。

通过标准反馈确认右臂待机、失能、无故障后，使用相同的受限恢复入口，目标为 J3=-0.5°、J5=69.5°，其余关节保持，最大反馈变化约 5.776°。然后普通 jog 右臂 J6 +1°/返回通过：127.562° → 目标128.562°、实际128.582° → 返回127.555°，去程/回程误差 0.020°/0.007°。

两臂均为 ctrl_mode=1、arm_status=0、fault_code=0，六关节使能。左臂在右臂接管前后读数一致，六关节变化均为 0.000°。这是分别接管与关节反馈的验收；未验证双臂同步轨迹、视觉抓取、碰撞规划或软件急停的实际效果。14 项本地测试通过，含左右适配器错配拒绝测试。

```bash
bash run.sh status --can can1
bash run.sh jog --can can1 --joint 6 --delta 1 --speed 5
```

右臂动作仍须显式 `--execute`。支撑恢复入口新增 `--can can1`，要求已经失能、待机并满足支撑和修正幅度检查，不自行 Reset 或修改主从配置。

证据：`results/can1_default_offsets_probe.json`、`results/can1_supported_reset.json`、`results/can1_output_mode_config.json`、`results/can1_supported_limit_restore.json`、`results/can1_j6_execution.json`、`results/dual_arm_takeover_summary.json`。左右模式切换或恢复后需重新查询实际反馈，不能假定特殊编号已永久恢复。

协议依据为 [官方 Piper SDK 固定提交](https://github.com/agilexrobotics/piper_sdk/tree/c9e8a28174e71eeaac448593cb65f8ab258a92fe)，不是仿真或外部演示成绩。

## 按键拖动示教

依据 [松灵 PiPER 快速使用手册（2024.09）](https://new.agilex.ai/raw/upload/20241017/%EF%BC%88%E5%B7%B2%E5%8E%8B%E7%BC%A9%EF%BC%89%E6%AD%A3-PiPER%E4%BD%BF%E7%94%A8%E6%89%8B%E5%86%8C_%E4%B8%AD%E6%96%87%E7%89%880925_52071.pdf) §2.2（印刷页 05–06）：J5 与 J6 之间有示教按钮及指示灯。灯灭时单击进入拖动轨迹记录，绿色常亮后可拖动；再单击结束，灯灭。双击是轨迹复现，会主动运动，当前接口验收不使用。开始一段新记录会覆盖之前记录，最长记录时间 3 分钟。控制接管前按手册放到零点，并先待机再切 CAN。

本次通过官方站点的搜索索引读取相关章节，PDF 直接下载因服务异常未成功，未检查零点姿态插图；现场应按对应型号的原始手册插图确认姿态。

13:27 补读 [官方 Agilex-College 的 playPos_new.py](https://github.com/agilexrobotics/Agilex-College/blob/c2688be41e1bc99a9addd555f237a16bc839d936/piper/recordAndPlayPos/playPos_new.py) 以及对应 [松灵社区教程](https://bbs.agilex.ai/d/46-%E7%8E%A9%E8%BD%ACpiper%E6%9C%BA%E6%A2%B0%E8%87%82%E5%9B%BA%E5%AE%9A%E7%82%B9%E4%BD%8D%E5%BD%95%E5%88%B6%E4%B8%8E%E6%92%AD%E6%94%BE%E5%8A%9F%E8%83%BD%E5%AE%9E%E7%8E%B0)：恢复前主要检查 J2、J3 绝对值 <0.1745 rad，J5 位于 (0.2094, 0.7854) rad。之前代码额外要求六关节都在 ±2°，该条件缺乏来源且过于严格，已改为上述参考窗口；测试确认 J1/J4/J6 可以保持非零值。这里将官方示例窗口作为保守的验收前置条件，不声称它是所有固件的强制限制。本机查询到的硬件限位继续独立检查，不放宽。

官方示例另外包含急停/Reset/重新使能过程，并可能失去支撑；本工具没有自动照搬这段过程。先尝试原手册的待机→CAN 切换并核对实际反馈，若被固件拒绝则中止；不自动 Reset。当前 J2=-1.689°、J3=+1.146°、J5=75.601°，恢复窗口和硬件限位仍未通过，未尝试切换。

[官方 SDK 维护者答复（2025-01-06）](https://github.com/agilexrobotics/piper_sdk/issues/11) 说明当时按键示教只能由按键触发。该历史答复不能证明当前 S-V1.8-5 的全部软件支持范围，因此本轮优先使用实体按键，不把 `MotionCtrl_1(..., grag_teach_ctrl=1)` 当作已验证的按钮示教替代。当前反馈的 `ctrl_mode=2` 与 `teach_status=2` 表示示教模式、记录已结束，并不表示正在允许拖动。

失能与 Reset 不等价于重力补偿示教。[官方 SDK V2 Reset 文档](https://github.com/agilexrobotics/piper_sdk/blob/c9e8a28174e71eeaac448593cb65f8ab258a92fe/asserts/V2/INTERFACE_V2.MD) 明确提醒 Reset 会使机械臂掉落。13:31 以前未执行 Reset；13:36 在用户明确授权支撑状态恢复后执行，详见上面的实际记录。未执行零点校准或改写限位。

## 双腕深度相机

两台 DaBai DC1 已同时通过 RGB/深度采集：左腕 `CC1365301R4`，右腕 `CC1365301HT`。彩色 640×480、深度 640×400，均设置 15 fps。入口 `bash cameras/run.sh`，绑定序列号并将 SDK 留在本机缓存。采样结束关闭相机，不改变机械臂状态。详细安装、权限、结果及尚未对齐/同步的边界见 [cameras/README.md](cameras/README.md)。

## Show-Harness 适配

已添加无 ROS 的本地 Piper/Orbbec 后端，接入原版 PiperSession 和动作解释器，双腕持续彩色采集及左右 J6 +1°/返回通过。入口 `bash show_harness/run.sh preflight`，默认不发送运动。详见 [show_harness/README.md](show_harness/README.md)。当前仅有两台腕部相机，完整模型所需前视观测、任务标定与夹爪/通用动作仍未验收。
