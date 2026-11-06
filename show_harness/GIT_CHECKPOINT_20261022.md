# 2026-10-22 Git checkpoint 验证

本次保存 alien 上已有的采集、训练、双模型及相机工作流；没有启动硬件、训练或模型服务。模型及本机数据的归档范围见 `../MODEL_ARCHIVE.md`。

- 源码与文档的 diff 空白检查通过。精选原始相机诊断记录中的 5 个 TXT 含末尾空白/结尾空行，作为原始证据保留；全文件空白检查因此有这几项格式提示。
- 17 套 Python suite 共 163 项：162 通过、1 error。运行附有进程内审计限制，以避免访问实际设备和服务；可选 ffmpeg 视频导出未验证。
- QuickJS 的 12 项 unittest 全通过；`test_gumi_js`、`test_webcam_js` 两个假浏览器脚本的顶层断言通过。
- 当前既有环境没有 Node，5 个 Node-only 检查未运行；没有安装新依赖。

已知失败：`test_browser_camera.FrontHTTP.test_wrong_origin_and_oversized_payload_denied` 在拒绝请求后复用 HTTP Session 时出现 `RemoteDisconnected`。独立重复 10 次，7 次通过、3 次同样报错；去掉网络审计后仍复现。当前处理器设置 `close_connection=True`，响应未显式发送 `Connection: close`，存在复用正在关闭连接的竞态。本次 Git 备份未改动该行为，不能据此称全部测试通过或实机已验收。
