# Piper 模型方向审计：2026-10-14

结论：官方混合语料实机 LoRA 输出的 MV_BACK 在 Piper 执行边界应映射为 MV_FWD。GUMI 配置仍声明该映射，但此前工作区控制代码已去掉映射检查并直接执行原始 token，造成配置和执行不一致。现在每次启动按所选 profile 读取和校验映射，仅在执行时应用一次，原始模型输出与最近动作历史保持不变。预览和运行页面分别标明“模型”和“执行”。手控方向不交换。

证据范围：本机线程索引未找到用户指定的“Piper 项目／排查机械臂抓取失败”对话，因此不引用该对话的结论；实际核对本项目以下材料：

- 上游固定提交 c3a133eb 的 `Show-Harness/docs/finetuned.md`、“Execution-boundary token swap”段；`configs/robot_piper_ft.yaml`、`core/launch.py` 中映射声明与实现。
- 运行 `results/show_harness/live/20261014-134415-RcorWT/`，任务 Pick up the water bottle，4B real-arm LoRA：第 0 步 MV_UP；随后原始与执行 token 都是 MV_BACK。第 1、2 步实际分别移动约 [-14.94,-12.55,0.44] mm、[-15.54,-12.59,0.16] mm，与后退方向一致；第 3 步被截断，第 5–16 步反馈位移为零，但执行结果仍为 ok。前视和左腕图没有对调，腕图中目标瓶子在夹爪前方；RGB 转换、图像顺序、v3 提示与最新动作优先的历史均核对。
- `results/show_harness/direction-audit/20261014/replay.json`：对本轮保存的第 0、15 步图像做离线回放，分别复现 MV_UP 和 MV_BACK；修复后的执行方向分别为 MV_UP 和 MV_FWD，未发送机器人命令。

同时增加实际进展检查：MV_* 被 reach clamp 截断且反馈位移不足 1 mm 时停止模型、保持暂停，保留执行日志与具体原因，防止重复零位移动作。仍保留工作空间和关节限位；不是继续放宽这些边界。

上游映射修复和离线回放不能证明本机已完成抓取。实际前视是正面桌面视角，而训练提示描述 overhead；相机位置、腕部安装与姿态可能进一步影响泛化。下一轮应先检查“模型 MV_BACK → 执行 MV_FWD”的预览，再现场验证一次受控移动及抓取效果。
