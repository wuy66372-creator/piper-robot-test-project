# 2026-10-22 Git 模型归档边界

来源：alien `/home/hokei/Hackathon`。详细文件、字节数及 SHA-256 见 `model-archive-20261022.json`，基座固定来源见 `show_harness/models.lock.json`。

六个已完成 LoRA 作业的最终配置、契约、初始化/恢复记录、tokenizer 和训练回执纳入 Git；两个约 3.7 MB 的 rank-8 最终 adapter 权重也纳入 Git。四个约 519.5 MB 的 rank-64 权重保留在 alien 的清单路径，未进入 Git，因此仅克隆仓库不能完整恢复这四个模型。需从 alien 取回对应 `adapter_model.safetensors`，并按清单核对 SHA-256 后使用。本次未验证正在激活的模型，也未启动服务。

原始数据集、optimizer、重复 checkpoint、持续相机帧和运行日志留在 alien，未删除。Git 保存精选验收报告、图像及 2026-10-06 离线运行证据；这不等于备份全部训练数据。

`robot/Show-Harness` 是独立的上游仓库，没有作为未配置的 gitlink 直接加入。固定上游 commit 记录在清单中，环境恢复沿用 `show_harness/training_setup.sh`；各机器虚拟环境不提交。
