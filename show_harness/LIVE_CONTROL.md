# GUMI 2B 真机推理

在 alien 本机启动：

```bash
bash show_harness/models.sh start qwen3_5_2b
bash show_harness/run.sh gumi --host 0.0.0.0
```

内网打开 `http://10.24.72.109:8620/` 可查看共享画面。前视只使用打开页面的电脑的浏览器 webcam；点击“使用浏览器摄像头”并授权，需 localhost 或 HTTPS。alien 本机用 `http://127.0.0.1:8620/`，远端电脑的 localhost 转发方法见 [MODELS.md](MODELS.md#GUMI-前视-webcam)。将前视相机对准所选机械臂、物体和目标区域；左右腕图来自各自 Orbbec 相机。选择左臂或右臂，填写任务（英文与发布训练语料更一致），模型选择 Qwen3.5 2B + LoRA。先用“仅推理预览”查看原始 token，再点击“2B 推理并执行一步”。“2B 连续运行”每次重新采图、推理、完成一个动作后继续，默认最多 20 步，可设 1–150 步。

“停止模型并保持”和原有“暂停并保持位置”均取消模型运行、作废未执行的结果，并中断后续关节/夹爪路点。暂停后点击“恢复控制”再开始新一轮。模型运行时手控动作被拒绝；先暂停即可切回手控。页面隐藏、关闭、心跳超过 2 秒、前视失效、反馈/限位检查失败均停止；恢复连接不会自动续跑。电脑网页暂停是软件位置保持，不替代现场硬件急停。

配置在 `live_piper.yaml`，合并本机 `site_piper_arms.yaml` 与 `vlm_local.yaml`。依据固定上游 `c3a133eb111a8100bcb64509b1ea891fdcee1532` 的 `configs/robot_piper_ft.yaml`、`core/runners/mvtoken.py` 和 `core/launch.py`：

| 项目 | 当前真机路径 |
| --- | --- |
| 模型 | Qwen3.5-2B BF16 + `qwen3_5_2b_showharness_ft` |
| 输入 | Agentview、所选侧 Wristview、v3 lite 提示文本；两图各 256×256 letterbox |
| 解码 | no-think 模板、temperature 0；无 guided decoding，严格单 token |
| 历史 | 最近 5 个原始 MV token，新到旧；不写入夹爪动作 |
| 执行 | 仅在执行边界交换 MV_FWD / MV_BACK；手控不交换 |
| 运动 | 每 token 20 mm，wrist heading frame；1 秒平滑插值，50 Hz 关节流 |
| 夹爪 | GRASP / RELEASE；闭合实测小于 5 mm 按上游空夹策略自动张开 |
| DONE | 结束本轮，记录为模型报告完成，由现场确认是否成功 |

本机差异明确保留：从当前姿态开始，不自动回上游示例 home/begin，也不在开始前自动张爪；驱动采用原生本机 CAN、5% 速度和已验证的关节路点/跟随检查。手控步长为 3 mm。坏模型回复停止，不采用上游异常后 MV_DOWN 的兜底。初始图像必须新鲜，输入图像到执行的年龄上限为 5 秒；推理完成后还会重新检查相机与机械臂位姿。

左右高度下限继承本机已存参考 0.196 / 0.1815 m，可通过原有 `--floor-left` / `--floor-right` 显式覆盖。这些是先前观测保存的临时高度参考，尚不是重新完成的桌面标定。相机物理摆位、方向、抓取成功率需本次现场测试验证；单臂发布 LoRA 不代表双臂协作策略。首次接入不会把旧的 init 关节值发送给当前机械臂。

每轮证据保存在 `robot/results/show_harness/live/<时间>-<编号>/`：启动配置、实际输入两图、完整提示、原始 token、执行换向、前后末端反馈、夹爪自动释放、停止原因。结果中的 DONE 不会被写成实机成功。训练录制入口仍独立关闭。

HTTP 接口：`POST /api/model/start`（task、arm、profile、client、max_steps），`POST /api/model/heartbeat`（run_id、client），`POST /api/model/stop`，`GET /api/state` 的 model_run。连续运行需要浏览器保持心跳；一次 start 请求本身不授予无限期运行。

验证入口：`bash show_harness/run.sh test`，以及本机 `js-check-venv` 运行 `test_gumi_js.py`、`test_webcam_js.py`、`test_model_js.py`。单元测试使用假硬件；本机 2B 的图像推理记录和权重校验见 `MODELS.md`。2026-10-12 接入时，两路 CAN 曾在电脑重启后无收发，USB-CAN 真正拔插断电并重新启用 1 Mbps 后恢复；软件 reset 没有解决该次故障。
