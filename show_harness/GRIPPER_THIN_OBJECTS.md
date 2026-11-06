# 薄物体夹持：2026-10-14

GUMI 默认关闭 `live_piper.yaml` 的 `plugins.auto_release`。夹爪开度小于 5 mm 无法区分纸巾等薄物体与真正空抓，因此模型的 GRASP 不再触发基于该阈值的自动 RELEASE。后续移动与重复 GRASP 保持夹紧；模型或操作者明确发出 RELEASE 时仍正常打开。夹爪故障检查、使能检查和停止逻辑保留，未增加夹持力度。

本次依据运行 `results/show_harness/live/20261014-140215-5pI__6/`：第 2、3、5、6、7、8 步 GRASP 均记录 `auto_released: true`。关闭默认空抓自动重开后，以假机器人验证 0.2 mm 薄物体夹持、重复 GRASP、MV_UP、显式 RELEASE；另保留显式启用 auto_release 的行为测试。本次未执行实机夹爪动作，也不把窄开度标为抓取成功。

运行状态和每轮 run.json 明确记录 `auto_release_enabled: false`，未启用时 `auto_release_empty_width_m` 记录为 null。若后续任务确实要重新启用自动空抓恢复，需要根据物体类别重新配置并验证；当前页面刷新后从暂停状态开始新一轮。
