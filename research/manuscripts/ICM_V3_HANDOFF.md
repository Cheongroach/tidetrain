# Spot GPU 训练诊断镜像下发

2026-10-25 01:55 UTC+8：两端镜像1.0.0.3已通过UI保存和下发，官方deployment GET已回读新镜像。两端worker寿命均为10740s。当前未完成任何 GPU round commit。

## 精确变更

在两个独立服务的镜像配置中，将 ICM **2424182** 的版本从 **1.0.0.2** 改为 **1.0.0.3**，保存后对现有部署下发。

| 服务 | 部署 ID | 当前资源 | 新镜像 |
| --- | --- | --- | --- |
| `<configured-service>` | platform-id-0001 | 8×A10，LF Spot | ICM2424182 / 1.0.0.3 |
| `<configured-service>` | platform-id-0002 | 8×A100-PCIE-80GB，YG Spot | ICM2424182 / 1.0.0.3 |

镜像地址：`<worker-image>

[构建成功日志](<platform-url>)。新增功能：失败子进程日志的认证回传、CUDA runtime 记录、全量验证集评估 job。新镜像已更新嵌入包的文件 SHA256；初始权重、语料、训练超参数一致。

LF 当前 HTTP worker 寿命为 3540 秒，启动 CMD 为 3570 秒。四轮训练需要跨此窗口保持服务，建议 UI 中将 LF 的 `LOWBAND_SERVICE_SECONDS` 改为 **10740**，启动 CMD 的外层 timeout 改为 **10770s**。两端 token 继续使用现有受控环境变量。

[HL 服务](<platform-url>)

[LF 服务](<platform-url>)

普通 Bernard CLI 仅支持 `container_image`，不支持这里的 `container_image_type` / `icm_url_list` 绑定；普通服务 env 变更也由 UI 处理。此前浏览器工具连续超时；恢复后已提交并回读本次UI变更。

## 下发后继续

1. 官方 `deployments get` 回读两端 `detail.infoSpec.image` 等于新镜像，并核对 new replicaset。
2. HTTP 身份必须匹配原配置 hash，选中的 incarnation 应具有足够寿命。
3. 从同一 HDFS run 恢复；初始全局 checkpoint 已提交，无成功训练 update。控制器仅启动一份。
4. 派发一次诊断任务并读取认证 status 的 `log_tail`，按实际堆栈修复；成功后完成 4 轮和 524,288 有效训练 token。
5. 完成控制器冷缓存重启、optimizer/RNG/cursor 回读和初始/最终 checkpoint 相同窗口的全量验证。
6. 汇总所有模型、delta、完整 optimizer 与 HDFS 传输字节和实际卡时；目前使用两个单 GPU slot，不称作 16 卡 DDP。

预约 #platform-id-0003 / #platform-id-0004 均到 **2026-10-26 20:00 UTC+8**；观察守护仍运行。原 19:34 截止时间已失效。等待和下发记录不是 GPU 正确性或性能结论。

续跑的受控入口将校验新镜像、从磁盘读取共享 token，并保留原训练 run 的绝对 deadline。如果原训练 deadline 已过，应建立新 run，不能修改 immutable run.json 延长期限。
