# Task3 GPU 实验报告：qwen05b-spot-20261025-c3

四轮 Qwen2.5-0.5B local-SGD 已提交，524,288 有效训练 token；全量评测与恢复／状态审计完成。本次验证loss和perplexity退化，没有证明训练质量收益。

## 训练与验证

两个单 GPU slot：A10 与 A100 PCIe 80GB。每轮各128步、batch=2、seq_len=256；FP32参数／AdamW状态，BF16 autocast训练。全局版本在唯一CPU控制器通过外层Nesterov提交。配置hash：`1a60cd28890a76cdedfb0f6b43d6ce8c73d33852bb7a3975ab6b53ac78e66bb7`。

| checkpoint | 完整验证 loss | perplexity | GPU评测秒数 |
| --- | ---: | ---: | ---: |
| round 0 | 3.087654460 | 21.925590258 | 29.610 |
| round 4 | 3.371856719 | 29.132567861 | 31.492 |

两次均为A10、FP32、batch4、1,025窗口和262,209目标token，包含尾窗。单次smoke不能建立统计显著性、DDP非劣性或大语料收敛结论。

本次完整验证loss变化 +0.284202259，perplexity变化 +32.870%；验证指标退化，没有证明训练质量收益。状态与恢复检查通过只验证系统协议和该轮确定性，不能替代收敛结论。

| round | A10计算秒 | A100计算秒 | 累计有效token |
| --- | ---: | ---: | ---: |
| 1 | 33.096 | 22.787 | 131,072 |
| 2 | 34.069 | 22.326 | 262,144 |
| 3 | 33.164 | 22.902 | 393,216 |
| 4 | 33.889 | 22.194 | 524,288 |

计算时间来自128步训练循环（GPU synchronize后截止），排除模型加载、optimizer恢复、序列化、HTTP、HDFS和全局提交。不能用它替代端到端吞吐。

初始版本发布至最终commit经过 43204.062 秒，有效吞吐 12.135 token/s；首轮commit至末轮 35213.481 秒，新增393,216 token对应 11.167 token/s。包含恢复、镜像更新、失败、存储及空闲，排除资产准备和最终评测；不是稳定态训练基线。

C3控制器启动到最终commit经过 4401.539 秒，新增393,216有效token，对应 89.336 token/s。该区间排除C1/C2之间的停机等待，包含C3初始化、恢复、重试和持久化，仍不等同于稳定态吞吐。

## 恢复与状态

首轮后停止唯一控制器，空CPU缓存读取HDFS，全局参数／外层动量与GPU optimizer／RNG／cursor逐项恢复。最终run `qwen05b-spot-20261025-c3` 直接继承c1首轮已提交历史；c1 deadline与run hash不变，child另有有限deadline。c2未提交的第二轮结果没有计入有效token。

每轮完整外层递推误差为0；Adam step依次128/256/384/512、cursor依次256/512/768/1024，全部290项参数optimizer状态有限，二阶矩非负，CPU／CUDA／Python RNG存在。独立A10对第二轮输入重放，完整delta、optimizer、RNG及cursor逐项一致。它覆盖该轮恢复确定性，未执行四轮不中断GPU对照。

已观测失败：同一GPU实例成功结果仍为succeeded但本地产物消失；下一次训练Transformers import失败；A100端点变化；4GB HDFS读取超过旧300秒超时。清理主体和平台端点变化原因未确认。控制器先拉齐delta/recovery再发布HDFS；v9将运行root从/tmp移到/opt并检查runtime文件；大对象get/put最多900秒且受剩余deadline约束。

c2在A10两份产物持久化后未完成验证；孤立CPU验证同一产物通过，但不足以确定原停滞原因。C3释放恢复状态后再加载前轮游标，聚合后释放delta，并记录验证耗时；27项正确性／HTTP／端点回归干净退出通过。完整状态审计、重放比较与最终GPU评测在控制器退出后串行运行。

C3控制器常驻内存采样峰值 11.180 GiB，进程HWM最高 11.683 GiB，采样866次。整个开发机cgroup的内存包含其他进程和文件缓存，不能把总占用等同于控制器常驻内存；详细记录见summary.json及controller-memory.jsonl。

完整状态审计峰值HWM 15.904 GiB；在当前32GiB开发机串行执行通过。本次无需申请新开发机或Spark资源；规模扩展仍需分片数据路径。

新增评测路径本地尾窗回归断言通过，但原生库尝试写/proc线程名被沙箱拦截，命令包装器返回1，不能称干净退出；三份日志及gpu-evaluation-regression.json保留此限制。独立完整GPU评测与四轮状态审计命令均返回0。

## 流量、成本与扩展

唯一committed blob 67.192 GB（全局状态、delta、optimizer；输入、孤儿及镜像另计）。接受job按序列化对象计算的HTTP预算 86.955 GB；并非wire测量。初始会话counter不完整、部分下载命中cache；成功HTTP body计数也排除headers/TCP/失败请求。逐阶段实测和controller会话见summary.json。

资源采样积分 203.622 GPU·h，184个样本，峰值16个副本；采样截至 2026-10-25 13:21:49 UTC+8；包括部署、预热、失败及空闲，不是账单。接受训练循环合计 0.062341 GPU·h，仅对应两个算法slot；保留8+8资源遵循用户授权，部署副本数不等于world size。

查询失败的slot区间已排除，未知覆盖 5.131 slot·h，涉及22个采样区间；未知数量没有按0计费，也未向最后采样之后外推。

按已知资源采样覆盖归一化，每百万有效token对应 388.379 GPU·h，或 2574.806 token/GPU·h。分母包含此前失败、部署预热与空闲副本，且缺失区间被排除；这个值用于解释本次实验资源消耗，不能作为稳定态效率或账单。

现实现warm round每单卡island约11.86GB状态HTTP预算；100/1000个单卡island为1.186/11.857TB。中央10Gbps串行理想时间约949/9485秒，明显无法由增加GPU解决。百卡先实现区域缓存、分片聚合与4–8GPU island，再验证固定token质量；千卡增加持久leader fencing、分片manifest、quorum与背压，并扩大固定版本语料。

100/1000 logical worker CPU协议探针通过，仅8参数合成tensor和本地filesystem；没有百卡／千卡GPU、HTTP或HDFS实测。当前WikiText训练流2,517,232 token，千卡单轮会产生大量重复训练。详见SCALING_DESIGN.md。

## 证据

commits/保留版本、接受结果、dispatch及输入ref；gpu-commit-audit.json记录全参数递推与远端SHA256，gpu-recovery-replay.json记录独立GPU对照，两份validation报告包含数据hash。大型权重按HDFS内容寻址保留，报告目录不复制权重。
