# Spot-UCE 执行记录

2026-10-24 17:32（UTC+8）。本次窗口已结束，训练未启动。HL/LF各申请1张A10，原订单17:00到期。守护在16:57:02/16:57:05删除deployment34522476/34522477（记录356776229/356776237）；两项GET返回not found，两服务部署列表均为空。已核验平台deployment移除，尚无底层GPU计费流水。

控制端终态为 `order_expired_before_both_endpoints`，17:00退出；`controller_started=false`，`gpu_training_verified=false`，无 `completed.json`。两个后台进程均已结束。

CLI/UI stdout 返回 HTTP400；用户完成 WebShell SSO 后仍返回 `Relay url is empty`。尚未读取到实际启动异常栈。基础镜像配置已核验：Entrypoint=null，Cmd=/lib/systemd/systemd；这是兼容性疑点，未确认退出根因。

独立镜像 SID `dbe5btn1enl7melegsi0`，ICM `2424182`。v1/build170535 因构建机连接开发机 IPv6 超时而失败。v2 `dbe5fnn1enl7melegsig`/build170536，ICM 1.0.0.2，构建平台显示成功（3m45s）：[日志](<platform-url>)。仓库 manifest 已核验为 `sha256:700294c540c210f1ce2e8a475b2ac2bd86cd176aebdd2bd0cf38bd5a2c1c12c7`，ENTRYPOINT 指向包内 HTTP 启动脚本，Cmd=null。

v2 把离线包嵌入 `/opt/lowband-package`，直接启动 HTTP worker。GPU 不读取 HDFS/NAS；训练 token 不进入镜像。下载走平台代理与独立临时构建凭证，已验证授权200/无凭证404；模板 `Dockerfile.spot-http` 不包含凭证。构建下载服务已停止，`mlx export health` 已撤销该唯一代理，回读0个有效链接，原授权URL返回403。

HL 新镜像和 `/opt/lowband-package` CMD 已保存并回读，模型绑定已恢复为2.0。15:19有实例响应认证 `/health`，`device=cuda`、`training_verified=false`。守护记录显示16:12:37启动的后续HL实例保持 `TASK_RUNNING` 至16:56；SDK仍没有发现端点。未完成GPU实例HTTP/HDFS预检，也未证明CUDA训练。

LF 新镜像/CMD已填入更新表单，但浏览器操作连续超时；17:28回读仍为旧镜像。页面最后停在“确认提交”弹窗，需恢复浏览器连接再提交、恢复模型绑定并回读。LF最后记录仍为 `NotReady`。

已新增 `spot_task_endpoints.py`，由官方CLI读取指定deployment tasks，仅选当前镜像、新replicaset和有效容器，再认证 `/health` 核验worker、配置哈希、CUDA路径和实例UUID。`spot_controller_entry` 与 `http_controller` 均支持 `--deployment-file`；旧SDK路径仍可用。4项真实本地HTTP CPU测试通过，覆盖NotReady但HTTP可用、新旧实例隔离、错误身份拒绝和到期清空缓存。此路径尚未GPU实测。

控制与守护终态见 `spot_run.json`、`spot_budget.json`，后者保留167次采样。deployment从14:32:29/14:32:30创建至16:57:02/16:57:05删除，目标各1实例的累计驻留约4.82小时，**不是精确GPU计费**。HL滚动更新曾报告新旧共2个replica；实际GPUh必须核对交付/实例流水。

后续：在无GPU占用时完成LF配置 → 明确新的有限Spot-UCE窗口与预算 → 配置新的deployment IDs和绝对截止时间 → HTTP/HDFS预检 → CUDA更新与完整轮次产物 → 释放回读。原订单不续期。
