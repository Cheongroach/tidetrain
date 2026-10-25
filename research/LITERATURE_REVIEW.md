# 低带宽与可抢占集群训练方法调研

检索日期：2026-10-25。范围为低频同步、通信与计算重叠、分片／压缩、异构和故障恢复。只引用已读取的论文或官方实现；论文的量级、性能数字不作为 TideTrain 的实测。

## 对当前系统的判断

TideTrain 的已完成实验是两个单 GPU island 的同步 local-SGD。它通过全量参数、Adam、RNG、游标持久化验证恢复，却把每个 warm round 的约 6D 状态流量集中到控制器；其中 D 是 FP32 参数大小。第一步应将区域 optimizer 状态与全局参数归约分离，建立有界分片和持久协调，再测相同算法。异步碎片合并和压缩属于后续独立算法对照。

低频跨 island 通信、岛内快同步和局部 optimizer 是多个独立实现共同采用的方向。[CONFIRMED] OpenDiLoCo 提供 FSDP/Hivemind 训练入口，DiLoCoX 将模型并行与双层 optimizer 结合，FlexDeMo 使用岛内分片、岛间压缩。[cite:1](#cite-1)[cite:4](#cite-4)[cite:5](#cite-5)

## 方法对比

| 方法与来源 | 关键机制 | 适用于 TideTrain 的部分 | 证据边界与代价 |
| --- | --- | --- | --- |
| OpenDiLoCo，2024 | 本地多步 AdamW，外层 Nesterov；FSDP 与 Hivemind 实现 | 数学基线、固定 token 对照、岛内分片 | 官方仓库已声明不再维护；其推荐的 `prime` 链接当前对应 CLI/SDK，不能直接作为原训练代码替代。[cite:1](#cite-1)[cite:7](#cite-7) |
| Streaming DiLoCo，2025 | 按片段错峰同步、延迟应用、权重混合和低精度外层梯度 | 让同步分布在 H 步内，减少突发带宽并尝试重叠 | 分片本身不减少覆盖全模型的总字节；实际重叠必须有并发通信执行，不能只设置 delay 参数。[cite:2](#cite-2)[cite:6](#cite-6) |
| Decoupled DiLoCo，2026-04-23 | 独立 learner、中央 syncer、最低 quorum、grace window、按 token 合并 | 故障只影响单个 learner，记录 fragment 版本和应用事件 | 已读取版本的百万芯片 chaos 规模含模拟，不能据此声称百万真实芯片性能。异步恢复比同步 round 恢复复杂。[cite:3](#cite-3) |
| DiLoCoX，2025 | Pipeline Parallelism、双 optimizer、一轮延迟重叠、adaptive compression | 以后扩到单岛内装不下的大模型；研究分片外层状态 | 论文报告 107B/1Gbps/357×，属于该论文特定基线，不能移植到 0.5B smoke 或宣称 TideTrain 加速。[SINGLE-SOURCE][cite:4](#cite-4) |
| DeMo，ICLR 2026 | DCT 等正交变换、top-k、local momentum 与 error feedback | 作为压缩 optimizer 对照，记录残差恢复状态 | 官方审稿页确认依赖 AllGather，压缩索引交换不等于可直接 all-reduce；64 GPU 的量级不能证明千个 island。[cite:8](#cite-8) |
| FlexDeMo，2025 | 岛内 reduce-scatter、岛间快动量分量交换 | 与 hybrid FSDP 结合，降低跨岛通信 | 已读取版本详细实验为 T5、2 节点×2 GPU；更大实验仅作初步观察，不应扩大为确定结论。[cite:5](#cite-5) |

## Streaming 和总流量的区别

Vanilla DiLoCo 每 H 步交换全模型，Streaming 将它分为 P 次较小交换。两者在同样 dtype 和完整覆盖周期下的总参数字节相同；Streaming 主要平滑突发流量并提供可重叠的时间窗口。论文进一步采用量化才降低总字节。MaxText 官方文档明确区分突发大小与总体流量，并提示其纯 JAX SPMD 路径中 delay/alpha 可只是算法研究开关，不能自动证明真实网络重叠。[CONFIRMED][cite:2](#cite-2)[cite:6](#cite-6)

TideTrain 当前额外回传和重下发 optimizer，超出标准 parameters-only 同步的流量。区域持久化、warm learner 保留 optimizer 和全局只交换参数／delta，比立即引入激进稀疏压缩更容易先保持同步语义。故障时仍需从区域恢复完整 optimizer/RNG/游标；减少检查点频率会改变可恢复边界，应单独测重算量。

## Decoupled 的恢复语义

异步 learner 不等同于仅将 barrier 改成超时。每个 fragment 需记录起始全局版本、已完成 step/token、上传版本、合并版本、应用时间和权重。Syncer 使用 quorum 与等待窗口合并已达到条件的 learner，恢复时需复现应用顺序；迟到结果的采用／丢弃必须可追溯。[cite:3](#cite-3)

该论文基础设施部分描述 event tape 与一致分布式 checkpoint，说明恢复对象应包含未完成的碎片交换和应用事件。TideTrain 第一版改造保持同步 round，使用持久 fencing 和事件记录；下一算法版本才引入 bounded staleness、按 fragment token 加权和延迟应用。同步系统的逐项恢复一致性不能直接扩展为异步系统等价性。

## 压缩的工程与质量成本

DeMo 的 top-k 输出含索引和值，聚合要求处理不同 worker 的支持集；AllGather 流量随参与岛数增加，不能把单 worker 压缩倍数直接当千卡吞吐倍数。官方审稿记录也讨论了充分调参的 AdamW 基线和实际多数据中心可迁移性。[cite:8](#cite-8)

任何压缩路径必须持久化 error-feedback／momentum 残差、压缩布局和 dtype。分片归约或层级部分和改变浮点加法顺序时，应给出明确数值容差，不能承诺与旧 FP32 逐 worker 归约逐 bit 相同。

## 百卡和千卡的难点

1. **数据平面容量。** 千个单卡 island 的当前 warm 状态预算达 11.857 TB/round；需区域缓存、区域 optimizer 状态、有界归约与分批 admission。该数字来自 TideTrain 参数字节契约，不是千卡实测。
2. **成员与故障域。** 单 GPU、island、区域网关、syncer 的失败语义不同。global epoch、worker generation、持久 lease/CAS 和 commit fencing 必须贯穿整个接受链路。
3. **训练质量。** 当前 smoke perplexity 上升 32.87%。必须固定 token、语料版本、初始 checkpoint 和验证集，再比较 local steps、外层 LR、异步 delay 和压缩。吞吐提高不能代替质量闭合。
4. **资源异构与长尾。** 同型卡先组成岛；不同岛按实际接受 token 加权。全员 barrier 对慢节点与抢占敏感，quorum 改变数据覆盖，因此需同时记录缺席者游标和重算。
5. **证据与成本。** 预约副本不等于有效 world size；申请、预检、排队或 CPU 协议探针不等于百卡／千卡 GPU 结果。GPU·h 需计入预热、失败、空闲和重复计算。

## 实施选择

优先完成 P1：原始 tensor 的有界分片、流式 Nesterov、区域恢复对象、持久 fencing、重复／迟到接受检查与事件日志。然后 P2 用真实 Qwen 状态验证、运行少量 GPU，测 RAM、字节、端到端吞吐和全量验证。P3/P4 申请真实百卡／千卡资源进行容量与故障测试；P5 使用更大固定语料和多个种子的质量对照。

不将全文量 checkpoint、激进压缩、异步 delay、quorum 和新 optimizer 同时修改，否则无法归因。Streaming／Decoupled、DeMo 与 DiLoCoX 的数值结果分别来自自己的模型、硬件和基线；没有一个来源足以证明当前 0.5B 配置已具备千卡收敛或有效成本优势。

## 来源

<a id="cite-1"></a>[cite:1](#cite-1) OpenDiLoCo 官方仓库与论文入口：https://github.com/PrimeIntellect-ai/OpenDiLoCo ，论文 https://arxiv.org/abs/2407.07852 。

<a id="cite-2"></a>[cite:2](#cite-2) Douillard et al., Streaming DiLoCo with overlapping communication: Towards a Distributed Free Lunch，2025-01-30：https://arxiv.org/html/2501.18512v1 。

<a id="cite-3"></a>[cite:3](#cite-3) Decoupled DiLoCo Team, Decoupled DiLoCo for Resilient Distributed Pre-training，2026-04-23：https://arxiv.org/html/2604.21428v1 。

<a id="cite-4"></a>[cite:4](#cite-4) Qi et al., DiLoCoX: A Low-Communication Large-Scale Training Framework for Decentralized Cluster，2025-06-26：https://arxiv.org/html/2506.21263v1 。

<a id="cite-5"></a>[cite:5](#cite-5) From et al., FlexDeMo: Decoupled Momentum Optimization for Fully and Hybrid Sharded Training，2025-02-10：https://arxiv.org/html/2502.06728v1 。

<a id="cite-6"></a>[cite:6](#cite-6) MaxText 官方 DiLoCo / Streaming DiLoCo 理论与实现边界：https://maxtext.readthedocs.io/en/latest/reference/core_concepts/diloco.html 。

<a id="cite-7"></a>[cite:7](#cite-7) 当前 `prime` 官方仓库 README 为 Hosted Training CLI/SDK：https://github.com/PrimeIntellect-ai/prime 。旧仓库跳转提示不应替代实际代码能力核对。

<a id="cite-8"></a>[cite:8](#cite-8) DeMo 官方 ICLR 2026 接收、修订与审稿记录：https://openreview.net/forum?id=U9oewpa7cn 。
