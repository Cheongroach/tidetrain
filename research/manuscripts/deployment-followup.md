2026-10-24 更新：用户已确认 Spot-UCE 不支持 ByteNAS，GPU 也不能直接访问 HDFS。原挂载排查已结束。已实现 Dorado 主动 HTTP 调用方案，详见 HTTP_DEPLOYMENT.md。下文为历史记录。

任务 3 部署核对（2026-10-22）

HL/LF 服务已创建，Spot-UCE 预约尚未提交，尚无 GPU 运行结果。

本轮通过 merlin-cli 的 CN 控制面只读查询，确认 HL 服务保存了 16 CPU / 64 GB / 1 GPU、ICM 1488235 版本 1.0.0.3、7 个端口、预定 CMD 和环境变量。Afflux serving-psms 查询返回该 PSM ResourceNotFound；Bernard service get 成功。

浏览器恢复后重新打开 HL 新增部署表单，确认 Reserved 表单有 ByteNAS 挂载，Spot-UCE 表单没有该字段。“预约单配置”页只有预约单推荐开关。服务更新页有多盘和 CloudFS 开关，但尚未找到指定 HDFS URI、ByteNAS volume 与目标路径的配置入口；未保存服务更新。

官方《单部署详情及配置说明》明确默认健康状态取决于 PORT0 监听；HTTP debug 协议字段不等于实际监听端口。本项目入口未提供 PORT0 服务，部署前需配置离线任务健康检查，避免正常训练被重建。《创建/更新服务》列出 IGNORE_LIVENESS_CHECK=1、IS_LIVENESS_CHECK=0、IS_READY_CHECK=0 的语义；尚未写入服务。

参考：
- <platform-url>
- <platform-url>
- <platform-url>

知识库检索 Bernard Spot UCE 自动创建部署 / HDFS / ByteNAS / CloudFS，没有找到直接解释 Spot 自动部署挂载来源的依据。飞书 connector 授权后读取返回 missing_scope（docx:document、docx:document:readonly），第二次授权被用户取消；后续仅用已可访问的浏览器读文档。

待确认的具体配置：
- 两站 HDFS <controller-storage-uri> → <controller-storage-mount>
- HL xugexin-hl → /mnt/bn/xugexin-hl。
- LF zy-exp-models-lf → /mnt/bn/zy-exp-models-lf。
- 预约精确有限到期时间，确保每站总占用不超过 3 小时。

当前浏览器 HL 表单 viewId：0b7d1318-99f9-4356-90ba-8f5cce6efbf1。旧 viewId 可能失效，恢复时须以 navigate 返回值为准。
