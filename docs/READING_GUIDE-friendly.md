# TideTrain 阅读地图

先读项目结论，再按需要打开细节。以下易读版全部以原文件名加 `-friendly` 保存；原文保留作证据和历史记录。

本轮两卡恢复实验完成，模型质量退化，真实千卡未完成；千卡资源入口选错，用户已经暂停。文件中的旧排队／预约状态不是当前运行承诺。

- [先了解项目](../README-friendly.md)。
- [看真实两卡证据](../results/qwen05b-4round/REPORT-friendly.md)。
- [看千卡为何没完成](../research/scale/EXECUTION_REPORT-friendly.md)。
- [看通信与扩展设计](SCALE_PLAN-friendly.md)。
- [看 CPU／GPU 部署](HTTP_DEPLOYMENT-friendly.md)。
- [看其他训练方法](../research/LITERATURE_REVIEW-friendly.md)。

## 阅读前最需要分清的词

| 词 | 在这项实验里的意思 |
| --- | --- |
| 参数／checkpoint | 模型数值，以及可以恢复某一已提交时刻的文件 |
| slot／worker／island | 一个逻辑训练参与者；此基线每组只选一个 GPU |
| replica | 平台部署副本，未必被算法选中 |
| world size | 实际共同参与该训练算法的数量，此 GPU 基线为 2 |
| local-SGD | 每组先训练多步，再交换模型变化 |
| DDP | 通常每步同步梯度的多 GPU 训练；这里没有非劣性对照 |
| delta | 本轮结束参数减去本轮起始参数 |
| optimizer／Adam | 决定如何更新参数的算法及其历史状态 |
| RNG／cursor | 随机数状态／数据读取位置，二者都要恢复 |
| manifest／commit | 文件引用清单／发布新版本的动作 |
| lease／fence／CAS | 有效期使用权／换实例的递增编号／条件版本提交 |
| quorum | 允许提交所需的最少参与者，会影响数据覆盖 |
| loss／perplexity | 同一设置下越低越好的预测误差；ppl=exp(loss) |
| RSS／HWM | 进程当前驻留内存／进程历史最高驻留内存 |
| GPU·h | 一张 GPU 分配一小时；采样估算与精确账单不同 |

## 本目录全部原文与易读版

历史镜像、预约和 UI 交接用于追溯；首次阅读不需要先读它们。

| 用途 | 易读版 | 保留原文 |
| --- | --- | --- |
| 项目概览 | [README-friendly.md](../README-friendly.md) | [原文](../README.md) |
| 历史 GPU 排查 | [archive/20261025/GPU_PROGRESS-friendly.md](../archive/20261025/GPU_PROGRESS-friendly.md) | [原文](../archive/20261025/GPU_PROGRESS.md) |
| 两卡实验与完整验证 | [archive/20261025/GPU_REPORT-friendly.md](../archive/20261025/GPU_REPORT-friendly.md) | [原文](../archive/20261025/GPU_REPORT.md) |
| HTTP 部署与恢复 | [archive/20261025/HTTP_DEPLOYMENT-friendly.md](../archive/20261025/HTTP_DEPLOYMENT-friendly.md) | [原文](../archive/20261025/HTTP_DEPLOYMENT.md) |
| 扩展设计与通信预算 | [archive/20261025/SCALING_DESIGN-friendly.md](../archive/20261025/SCALING_DESIGN-friendly.md) | [原文](../archive/20261025/SCALING_DESIGN.md) |
| HTTP 部署与恢复 | [docs/HTTP_DEPLOYMENT-friendly.md](HTTP_DEPLOYMENT-friendly.md) | [原文](HTTP_DEPLOYMENT.md) |
| 公开副本与证据追溯 | [docs/PUBLIC_EXPORT-friendly.md](PUBLIC_EXPORT-friendly.md) | [原文](PUBLIC_EXPORT.md) |
| 扩展设计与通信预算 | [docs/SCALE_PLAN-friendly.md](SCALE_PLAN-friendly.md) | [原文](SCALE_PLAN.md) |
| HTTP 部署与恢复 | [lowband_training/HTTP_DEPLOYMENT-friendly.md](../lowband_training/HTTP_DEPLOYMENT-friendly.md) | [原文](../lowband_training/HTTP_DEPLOYMENT.md) |
| Notebook 阅读说明 | [lowband_training/dorado_http_training-friendly.md](../lowband_training/dorado_http_training-friendly.md) | [原文](../lowband_training/dorado_http_training.ipynb) |
| 可读 Notebook，代码未运行 | [Notebook 版](../lowband_training/dorado_http_training-friendly.ipynb) | 保留原代码 |
| 方法调研 | [research/LITERATURE_REVIEW-friendly.md](../research/LITERATURE_REVIEW-friendly.md) | [原文](../research/LITERATURE_REVIEW.md) |
| 对话中的技术决策 | [research/conversation/CONVERSATION_HISTORY-friendly.md](../research/conversation/CONVERSATION_HISTORY-friendly.md) | [原文](../research/conversation/CONVERSATION_HISTORY.md) |
| 千卡阶段用户修正 | [research/conversation/SCALE_CONTINUATION-friendly.md](../research/conversation/SCALE_CONTINUATION-friendly.md) | [原文](../research/conversation/SCALE_CONTINUATION.md) |
| 历史 GPU 排查 | [research/manuscripts/GPU_PROGRESS-friendly.md](../research/manuscripts/GPU_PROGRESS-friendly.md) | [原文](../research/manuscripts/GPU_PROGRESS.md) |
| HTTP 部署与恢复 | [research/manuscripts/HTTP_DEPLOYMENT-friendly.md](../research/manuscripts/HTTP_DEPLOYMENT-friendly.md) | [原文](../research/manuscripts/HTTP_DEPLOYMENT.md) |
| 诊断镜像交接 | [research/manuscripts/ICM_V3_HANDOFF-friendly.md](../research/manuscripts/ICM_V3_HANDOFF-friendly.md) | [原文](../research/manuscripts/ICM_V3_HANDOFF.md) |
| 扩展设计与通信预算 | [research/manuscripts/SCALING_DESIGN-friendly.md](../research/manuscripts/SCALING_DESIGN-friendly.md) | [原文](../research/manuscripts/SCALING_DESIGN.md) |
| 第二轮资源预约 | [research/manuscripts/SPOT_B1_STATUS-friendly.md](../research/manuscripts/SPOT_B1_STATUS-friendly.md) | [原文](../research/manuscripts/SPOT_B1_STATUS.md) |
| 第一轮资源预约 | [research/manuscripts/SPOT_STATUS-friendly.md](../research/manuscripts/SPOT_STATUS-friendly.md) | [原文](../research/manuscripts/SPOT_STATUS.md) |
| 平台配置交接 | [research/manuscripts/UI_HANDOFF-friendly.md](../research/manuscripts/UI_HANDOFF-friendly.md) | [原文](../research/manuscripts/UI_HANDOFF.md) |
| 已放弃的挂载路径 | [research/manuscripts/deployment-followup-friendly.md](../research/manuscripts/deployment-followup-friendly.md) | [原文](../research/manuscripts/deployment-followup.md) |
| Notebook 阅读说明 | [research/manuscripts/dorado_http_training-friendly.md](../research/manuscripts/dorado_http_training-friendly.md) | [原文](../research/manuscripts/dorado_http_training.ipynb) |
| 可读 Notebook，代码未运行 | [Notebook 版](../research/manuscripts/dorado_http_training-friendly.ipynb) | 保留原代码 |
| 扩展尝试与证据范围 | [research/scale/EXECUTION_REPORT-friendly.md](../research/scale/EXECUTION_REPORT-friendly.md) | [原文](../research/scale/EXECUTION_REPORT.md) |
| 两卡实验与完整验证 | [results/qwen05b-4round/REPORT-friendly.md](../results/qwen05b-4round/REPORT-friendly.md) | [原文](../results/qwen05b-4round/REPORT.md) |

## 为什么有些易读版内容相似

发布分支、研究分支和原始证据目录保存了相同或近似手稿。对应副本各有一个易读入口，但采用同一套已核验的解释，避免旧状态互相矛盾。完整文件映射和原始 SHA256 在 [DOCUMENTS-friendly.json](DOCUMENTS-friendly.json)。

阅读稿保留理解结论所需的数字、条件、公式和引用；精确配置标识、所有重试流水和早期局部结果仍在原文及机读记录。
