# 原挂载方案为什么后来不用了

这份历史交接起初想让两边 GPU 都通过 HDFS／ByteNAS 共享训练状态。后来用户确认 Spot-UCE 不支持 ByteNAS，GPU 也不能直接访问 HDFS；于是把持久存储职责移到 CPU，用 HTTP 与 GPU 交互。

## 哪些观察成立，哪些没有成立

早期只读查询确认过服务规格、镜像、CMD、环境变量和七个端口。Reserved 表单有 ByteNAS 选项，Spot-UCE 没有；服务更新页出现 CloudFS 或多盘开关，不能据此证明指定 HDFS URI／ByteNAS 卷可挂到 Spot 容器。

当时还没有取得正式 GPU 训练结果。网页选了 HTTP debug 协议，也不代表训练脚本会监听 PORT0；端口健康检查必须按真正入口验证。

## 后来的结构

GPU 读镜像、平台模型目录和本地盘。唯一 CPU 控制器发起 HTTP 请求，回收完整模型变化与恢复状态，再写 HDFS。这样不依赖 GPU 容器访问共享存储。

[HTTP 部署说明](../../docs/HTTP_DEPLOYMENT-friendly.md)是后来的入口；原文中的挂载路径、旧 viewId、授权和表单疑点用于解释被放弃的方案，不应作为当前待执行清单。具体历史内容保留在[原交接](deployment-followup.md)。

---

原文：[deployment-followup.md](deployment-followup.md)。保留历史操作、配置或研究推导，便于核对当时状态。

阅读版整理于 2026-10-26；历史状态不作当前运行承诺。
