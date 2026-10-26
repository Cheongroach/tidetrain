# 低带宽训练有哪些可借鉴的方法

这份调研要回答的是：多个训练参与者之间的网络慢、机器还可能退出，怎样减少等待并保持可恢复性？来源检索截至 2026-10-25，以下是当时已读取版本的结论，不是 TideTrain 的性能实测。

TideTrain 的两卡实验已经能恢复，但全量状态传输很大，而且本次模型质量变差。采用新论文的方法之前，需要把网络／恢复改造和训练算法改造分别评估。

## 从最直观的变化理解

梯度描述一次更新方向；DDP 通常每步交换梯度；local-SGD 让每组 GPU 先训练若干步，再交换模型变化。通信少了，各组也会走得更远，参数偏离和质量变化需要实测。DiLoCo 风格在本地使用 AdamW，再用外层 Nesterov 合并更新；增加本地步数的质量影响需要实测。[OpenDiLoCo](https://github.com/PrimeIntellect-ai/OpenDiLoCo)、[论文](https://arxiv.org/abs/2407.07852)。

| 方法 | 主要改变 | 对本项目的意义 | 必须保留的条件 |
| --- | --- | --- | --- |
| OpenDiLoCo | 本地多步训练，低频外层同步 | 提供数学与实现参考，含 FSDP／Hivemind | 官方仓库已声明不再维护；推荐链接当前指向的 prime 是 CLI／SDK，不能当作同一训练实现 |
| Streaming DiLoCo | 把全模型交换拆成错峰的小片段，延迟应用 | 平滑带宽突发，尝试让计算覆盖通信 | 相同精度、完整覆盖周期下，分片本身不减少总字节；必须实际并发通信才有重叠 |
| Decoupled DiLoCo | learner 独立推进，syncer 按 quorum 与等待窗口合并 | 某个 learner 失败时，其他参与者未必都停 | 片段版本、应用顺序与未完成交换必须写入恢复状态；百万芯片规模含模拟 |
| DiLoCoX | 模型并行、双层 optimizer、延迟合并和压缩 | 面向单组也装不下的大模型 | 107B／1 Gbit/s／357×是其特定实验与基线的作者报告，不能迁移成 TideTrain 加速 |
| DeMo | 对动量做变换，交换选出的少量分量，保留残差 | 可作为压缩 optimizer 对照 | 交换包含索引和值，依赖 AllGather；参与者增多会影响总交换量 |
| FlexDeMo | 组内分片，组间交换压缩动量 | 连接可靠组内网络与慢速组间网络 | 所读详细实验为 T5、两节点各两 GPU；更大规模观察不是千卡确定结果 |

FSDP 是把模型／状态分片到多卡；AllGather 是让每个参与者收齐各方数据。压缩后每个 worker 发送少，并不自动保证所有 worker 合起来仍少。

## Streaming 为什么主要先改善等待

一次交换整份模型会形成带宽突发。把它拆成 P 次较小交换，总和仍是一份模型；获益来自时间分布和计算通信重叠。论文采用量化时，总字节才进一步下降。

MaxText 官方文档还区分算法中的 delay／alpha 参数与真实运行时重叠：设置参数并不代表网络线程实际与计算并发。参考 [Streaming 论文](https://arxiv.org/html/2501.18512v1)和 [MaxText 文档](https://maxtext.readthedocs.io/en/latest/reference/core_concepts/diloco.html)。

## 异步恢复为什么更难

同步轮次有一个明确的“上一版”；异步片段可能来自不同全局版本，也可能已经上传却尚未应用。恢复必须知道每个片段从哪版出发、做了多少训练、何时被谁以什么权重合并，以及还有哪些交换没完成。

Decoupled 的 event tape 是记录这些应用事件的序列。它不能简化成“barrier 超时后继续”，同步一轮的逐项重放一致也不能证明异步恢复一致。[Decoupled 论文](https://arxiv.org/html/2604.21428v1)。

## 压缩需要同时保住什么

只交换 top-k 分量会留下未发送部分；error feedback 将这部分留作下一次补偿。因此残差、动量、索引布局和精度也属于恢复状态。组间 AllGather 的量级需按全部参与者计算，不能直接用单 worker 的压缩倍数预测千卡吞吐。

Top-k 是只保留最大的若干分量；DCT 是把数值转换到另一组正交基，error feedback 记录没发出的残差。DeMo 的基线调参和多数据中心可迁移性也在审稿记录中被讨论。[DeMo 接收与审稿页](https://openreview.net/forum?id=U9oewpa7cn)、[FlexDeMo 论文](https://arxiv.org/html/2502.06728v1)。层级求和改变浮点加法顺序时，应给数值容差，不能自动承诺原 FP32 逐 bit 等价。

## 当前最容易清楚归因的选择

保持同步 local-SGD 数学不变，先分片归约、区域缓存和区域恢复状态；再单独比较 Streaming／Decoupled／压缩。这能分清收益来自搬运方式还是训练算法。

目前只完成单网关分片和账本，区域聚合、异步与压缩仍是计划。Qwen 基线 perplexity 上升 32.87%；固定相同 token、初始模型、语料和验证集，进行多个种子对照后，才能谈质量非劣性。用户要求直接千卡申请，不需要从八卡爬坡；但此前资源入口选错，实际千卡结果仍未产生，执行已暂停。

其他来源：[DiLoCoX](https://arxiv.org/html/2506.21263v1)、[当前 prime 仓库](https://github.com/PrimeIntellect-ai/prime)。完整引用与当时的证据标签保留在[原调研](LITERATURE_REVIEW.md)。

---

原文：[LITERATURE_REVIEW.md](LITERATURE_REVIEW.md)。保留历史操作、配置或研究推导，便于核对当时状态。

阅读版整理于 2026-10-26；历史状态不作当前运行承诺。
