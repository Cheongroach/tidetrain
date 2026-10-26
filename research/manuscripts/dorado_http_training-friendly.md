# Dorado Notebook：每个单元格为什么存在

这份 Notebook 是原两卡 HTTP 基线的控制入口，不是显卡申请器，也不是千卡调度程序。它尚未在 Dorado Executor 完成实测；本轮已暂停操作。公开副本里服务、存储路径是匿名占位值，不能照抄运行。

## 运行环境和输入

CPU 控制端需要 PyTorch、HDFS CLI、服务发现能力和到 GPU PORT0 的网络权限。平台给 GPU 下发完整离线目录；GPU 不直接读写 HDFS／ByteNAS。

`LOWBAND_PACKAGE_DIR` 指已解包目录，`LOWBAND_RUN_ID` 标识实验，`LOWBAND_HTTP_TOKEN` 是控制端与 worker 的相同凭据。凭据只从受控环境读取，不能粘贴到 Notebook。

## 三个代码单元格分别检查什么

| 代码单元格 | 作用 | 为什么必须放在这里 |
| --- | --- | --- |
| 1：资产与端点发现 | 核对解包清单，导入代码，发现 hl／lf 两个槽位 | 没有双端点时应等待资源，不创建训练结果 |
| 2：HTTP／HDFS 预检 | 往返校验并检查身份，保存两端 probe_report | HTTP 能连与持久存储能提交是两个条件 |
| 3：训练控制循环 | 从配置创建 HdfsStore／Controller，恢复或运行同一实验 | 只有收齐并保存恢复状态后才能推进全局版本 |

`hl`／`lf` 是逻辑名字，不能据此推断实际机房。发现端点数不会申请或扩容显卡。

## 为什么控制循环只能执行一份

两个 Notebook 同时执行这个 cell，会有两份控制器试图推进同一全局版本。原基线只靠本地文件锁，不能防跨主机双主；不要把 cell 放入 Spark mapPartitions 或推测执行 task。新分片容量路径使用单宿主机账本，也不意味着原 Notebook 自动获得跨主机 HA。

## 输出不等于所有验收都完成

`completed.json` 只说明训练状态已提交。还需完整 validation、接受 token、恢复比较、流量和资源退出回读，不能凭它声称模型变好或副本已释放。

[易读 Notebook](dorado_http_training-friendly.ipynb)保留代码，只重排解释和清空执行输出，没有实际运行；[原 Notebook](dorado_http_training.ipynb)用于核对代码。开始理解前建议先读[部署说明](../../docs/HTTP_DEPLOYMENT-friendly.md)和[两卡结果](../../results/qwen05b-4round/REPORT-friendly.md)。

---

原文：[dorado_http_training.ipynb](dorado_http_training.ipynb)。保留原始可执行代码和当时的环境说明。

阅读版整理于 2026-10-26；历史状态不作当前运行承诺。
