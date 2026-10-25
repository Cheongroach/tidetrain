# Task3 GPU 执行进度

2026-10-25 13:25 UTC+8：C3第四轮最终commit与completed.json已发布回读，524,288有效token，最终SHA256 `37908eacf2a0a69b3258dd77b81f94a44c616e964d1557749c3d6f1ce1ffa63e`。唯一控制器997155已退出。四轮全参数／外层动量递推误差均0；两worker的290项Adam状态step依次128/256/384/512、cursor依次256/512/768/1024，全部有限且二阶矩非负；21份committed blobs合计67,191,974,291 bytes远端SHA256通过。不同A10实例重放第二轮完整delta／optimizer／RNG／cursor逐项相同。完整同条件FP32 GPU评测loss3.08765446→3.37185672，ppl21.92559026→29.13256786（+32.87%），没有质量收益。控制器HWM11.683GiB、审计HWM15.904GiB；本机串行完成，未申请Spark或新开发机。

主GPU终验及状态审计命令返回0；既有27项CPU回归返回0。新增尾窗评测回归三次断言均OK，但/proc线程名写入被沙箱拒绝，包装器返回1，不能称干净通过。原始日志和限制记录已归档。最终报告见 [GPU_REPORT.md](GPU_REPORT.md)，下面保留历史执行记录。

2026-10-25 12:20 UTC+8：第二轮全局commit已发布并回读，累计262,144有效tokens，state SHA256为`0f142a44199b9f2e0076b67887d3d3fc4365d070df2f0dc055aa68e06a36c792`。A10接受attempt计算34.069秒、验证35.136秒；A100前两个端点失效后generation2计算22.326秒、验证41.695秒。两worker delta/recovery先收齐到CPU，再完整发布HDFS及验证。聚合阶段进程HWM约11.3GiB，仍在当前32GiB开发机内运行；第三轮输入准备中。完整tensor审计和最终GPU评测尚未执行，当前没有四轮终验结论。

2026-10-25 12:01 UTC+8 当前状态：C3 唯一控制器997155运行，直接继承C1已提交首轮，仍为131,072有效tokens；第二轮A10在新实例完成128步、34.069秒，A100已接收参数及前轮恢复状态，尚未发布第二轮commit。C3有限deadline为14:47:09，C1 hash/deadline保持不变。C2在A10产物持久化后未完成验证，未接受结果不计入训练量；同一产物的孤立CPU验证通过，但原停滞原因未确定。

C3已减少游标验证和聚合阶段的存活tensor，增加验证起止／耗时日志；27项正确性、HTTP与端点回归通过，命令返回0、23.997秒。内存观察每5秒采样，当前控制器RSS约2.5GiB、HWM约4.2GiB，cgroup中另有约13.7GiB文件缓存；暂未发现需要迁移开发机的容量证据。按用户建议，若本机成为瓶颈可使用Dorado Spark或新的8C32G开发机；状态控制器必须保持唯一，不能放入独立重试／推测执行的Spark task。

首轮6份远端状态19,762,318,094 bytes的SHA256已复用原始回读证明；新轮次由最多4线程只读校验，未并发加载大型训练状态。最终完整FP32 GPU评测、独立GPU重放比较、完整optimizer／RNG／cursor与外层递推审计，会在控制器退出后串行执行。保留8×A10＋8×A100；资源采样查询失败的slot区间按未知记录并排除积分，不能当作0卡。下面保留历史记录。

2026-10-25 03:33 UTC+8。首轮 GPU 全局 commit 已发布并经 HDFS CLI 回读：131,072 有效 tokens。控制器已干净暂停；独立空缓存、两台不同 GPU 实例的冷恢复正在运行。四轮尚未完成。

初始 checkpoint 完整 FP32 验证：262,209 目标 token / 1,025 窗口，loss **3.08762771**，perplexity **21.92500375**，CPU 1054.26 秒。包含最后65个目标 token；先前1024-token诊断不能代表完整验证集。

A10 首份 HTTP 训练结果：128步 / 65,536 token，31.47秒计算时间，峰值 allocated **9,925,827,584 bytes**，mean loss **2.90879118**，first/last **3.39214182 / 2.76864028**。对应 job `d8f693ab18e83e7430a4e5ba5bf023bd00b07717d9d3ea6a2d776bc3a711a8e6`，实例 `instance-0012`；原始结果见 `a10-first-gpu-result.json`。尚未验证完整 optimizer/RNG/cursor 产物或外层参数更新。

本地已下载 delta 1,976,222,459 bytes，但 recovery 下载404；随后同一实例 delta/recovery status均报告不存在，job status仍为succeeded。控制器799023已停止，不能把这份结果计入有效全局 token。新增缺失产物失败记录和generation重分配；修复了可复现的旧job重复DELETE清空共享目录问题。该清理缺陷是否就是本次GPU文件消失原因尚未确认。

v4 / ICM2424182 1.0.0.4：两服务官方deployment GET均已回读镜像 `instance-0013`。部分A10真实BF16 matmul通过，另一些A10宿主470.129.06而加载470库，CUDA12.9报驱动过旧。A100宿主470.129.06却加载575.57.08库，三路径均803；见 `cuda-a100-v4.json`。多机驱动不同，不能把一次通过当全体资源通过。

v5/build170566失败于NVIDIA安装包下载403，torch2.5.1/cu118安装阶段通过。v6改为官方CUDA Debian包，加入匹配470用户态库候选、按job清理与认证存储状态，正在构建。没有安装宿主内核模块或修改宿主驱动。

本地回归：CUDA路径选择和HTTP发现、传输、恢复13项通过；新增旧清理重放、缺失成功产物fencing后HTTP7项通过，冷缓存重启仍与不中断参数完全一致。CPU验证不等同于GPU恢复或大规模性能。

资源：两预约仍8×A10 + 8×A100 PCIe80GB，到10月26日20:00。观察守护364697每300秒读取平台。63次样本积分约 **67.94 GPU·h**，包含等待、预热、失败和闲置；非账单，不能覆盖样本间变化或滚动副本重叠。20卡不是硬上限，资源授权见 `resource_authorization.json`。

下一步：v6完成构建并回读 → A100优先更新/真实BF16探针 → A10待当前不可恢复attempt正式fencing后更新 → 原run有效期内恢复4轮，否则新run → round1后停止唯一控制器并以空缓存恢复 → 初始/最终相同FP32完整窗口 → optimizer/RNG/cursor、流量/时延/卡时报告 → 多实例调度及分层数据路径。当前没有16卡DDP或百卡/千卡性能结论。

02:38更新：v7/build170568、ICM1.0.0.7构建成功，采用已核验SHA256的31.5MB驱动Debian包；v6脚本引用不存在的fatbinaryloader文件，后续以v7为基底。v8/build170569 (`dbefltf1enl4ln9e7d30`)正在构建，移除基础镜像遗留的torchvision/torchaudio2.9扩展，并以离线wheel环境验证Qwen2导入。服务当前仍v4，不能声称cu118已GPU验证。A10缺失产物attempt失败记录已HDFS回读，控制器已停止；基线CPU评测也已结束。下一步等待v8成功→更新active_image.json及双服务→恢复原c1；c1 round0 HL下一generation为3（最多4次attempt），超出需新run。HTTP全量评测本地集成验证通过，1023目标token含尾窗与直接FP32评测一致。

2026-10-25 03:12 UTC+8 更新：v8/build170569 已成功并经 CLI 普通服务 container_image 更新、双 deployment GET 回读。实测 A10/CUDA11.8 使用匹配470库，A100也通过真实BF16探针。A10首轮128步33.09615秒，A10022.78718秒，各65,536 tokens；两份delta=1,976,222,062 bytes、recovery=3,952,504,290 bytes均存在。GPU初始完整FP32验证完成：1,025窗/262,209目标token，loss3.0876544596、ppl21.9255902578、29.60969秒。

发现HdfsStore导入后未保留本地cache，导致完整HTTP文件持久化后再次HDFS下载；已增加durable import缓存，并验证临时文件关闭后缓存仍有效。旧控制器865090已停止；新883518使用原已成功GPU实例和原cache接收结果，不重新训练。新增18项CPU检查通过（HTTP/端点13、缓存2、只读挂载bridge3）；并增加HTTP/持久化阶段秒数及实际HTTP字节计数。读bridge只在CPU侧用于已核验HDFS mount，所有写入/rename仍走标准HDFS CLI。

首轮commit后计划以独立空cache和两个不同GPU实例冷恢复。独立GPU replay脚本等待round1输入，再重放下一轮并比较完整delta/optimizer/RNG/cursor。v8控制器首个进程被中断，首轮HTTP counters不完整；有效payload下限可由已提交refs推导，但不能把它称为精确wire流量。当前所有GPU实例数仍保持8+8，没有缩容。

2026-10-25 03:24 UTC+8 首轮完成：state SHA256=78d16be7be96428524c66f0058df12b4c138af29bbf9a85c57b9a367827a5d0c，3,952,432,695 bytes；原run deadline=1792872323.30477保持不变。缓存修复后的首个控制器883518用时794.736秒，HDFS新增写9,881,163,055 bytes、68次CLI命令475.683秒。首轮LF delta下载57.537秒、HDFS发布86.525秒；recovery下载114.305秒、HDFS发布151.908秒。其HL下载阶段命中既有缓存，bytes字段是对象长度，不代表该阶段发生网络传输。

冷控制器898282于1792869838.953启动，cache_empty=true，HL实例913689d1a1334d149276f032fbc95eee、LF实例d8c6539e8a764cb3aac225335219e279均区别于首轮实例。预选旧端点已被滚动部署移除，启动preflight捕获后重新官方发现再选择。独立GPU replay也已切换到当前有效的另一台A10，等待round1 input。实际deployment地域为lf-gpuspot1-default（A10）与yg-gpuspot1-default（A100）；HL/LF是逻辑slot标签，不能称为实际HL/LF物理卡。

2026-10-25 03:33 UTC+8：空缓存控制器已回读首轮全局参数／外层动量并发布第二轮parameter-only输入（SHA256 4af6c66b6652b7722b09123b16369def9697a8aec5f2ad8797170a1f12998fe6）。HL下发1,976,222,459 bytes用时27.2796秒，计数为成功HTTP请求body字节，未包含headers／TCP／失败传输；正在恢复前轮optimizer。独立GPU replay已获得同一输入并开始上传，结果尚未完成。

补齐fork_run.py和只读父blob fallback，5项CPU检查通过，拒绝继承in-flight dispatch／未提交update；fork-ready.json最后发布。守护904651将在第二轮commit后（或原deadline／进程退出时）确认旧控制器退出，再创建c2有限窗口并继承已提交历史；不会修改c1的run.json或预约。原run SHA256保持fe370dab2bf01105556b3dd13a451e50706280d462c27d05919894b1fd4fe979。

100／1000 logical worker的两轮CPU控制协议探针均通过，分别0.687／6.248秒，见scaling-control-probe.json。该探针只有8参数合成tensor和本地文件系统，不能宣称GPU／HTTP／HDFS规模性能。远端blob SHA256＋完整optimizer／RNG／cursor＋外层递推审计904087正逐轮执行，完成前不记录总体验收通过。资源样本76次积分85.623 GPU·h（包括预热、失败与闲置，非账单）；保持原8+8部署。

2026-10-25 03:43 UTC+8：冷恢复A10 gen0完成128步31.726秒，但delta发布HDFS83.908秒后，远端recovery404；同实例status仍succeeded而delta/recovery均缺失。已保存原始状态并fencing，不计有效token，删除原因尚未确认。v8继承v7的Jobs.release幂等／引用保护代码已从构建request overlay核验，不能据此认定本次仍为旧清理bug。控制器已改为先把delta/recovery都下载到CPU本地，再分别发布HDFS，缩短对Spot临时文件的依赖窗口；当前c1已加载旧代码，后续c2控制器会使用新代码。

对应真实localhost HTTP故障注入（在首个durable import时删除GPU端临时文件）及HTTP／发现／fork共19项断言完成，日志显示OK，25.073秒。但命令包装器最终返回1，报告原生库写/proc/<pid>/task/<tid>/comm受沙箱限制以及已终止测试子进程code15；不能称该命令干净退出，见/tmp/lowband-spot-c1/controller-precollect-tests.log。

A100旧端点不可达，官方GET新tasks曾无CUDA-ready，03:38重新发现2个ready实例，映射到<private-host>:10264，实例6775a02927ad4ea3a196fdb3da4aa7bb；故障具体原因未确认。监视909598仅维护c1端点并记录gpu-endpoint-remaps.jsonl。HL gen1已重发并训练；LF gen0已分配并上传参数，正在回读前轮optimizer。两组目标8+8保持不变。

独立GPU replay改用已SHA256核验的控制器cache输入（durable refs不变，GPU仍是另一实例），909147已收集结果：128步34.196秒，loss指标与HL gen0相同；完整tensor／optimizer／RNG／cursor比较仍等待正式接受记录，尚不声称完全一致。原远端全局blob SHA256回读耗时621.900秒；现由最多4个CPU并发读取的ledger进程914229核验其余committed blobs，数值审计916211复用该真实远端proof，避免重复读取，日志gpu-commit-audit-v2.log。最终完整FP32评测等待进程905817仍在等四轮commit。

2026-10-25 03:50 UTC+8：冷恢复HL gen1的真实GPU子进程失败，ImportError: cannot import name AutoModelForCausalLM from transformers (unknown location)。同一服务此前产物缺失；容器日志证实启动时离线wheel安装成功。清理主体尚未确认。以v8为基底构建v9/build170574/dbegmtn1enl8i32rdr60，默认本地root从/tmp迁到/opt/lowband-local-$zone，并在health实时检查Python、Transformers initializer、模型config和数据manifest。7项运行目录／HTTP发现回归干净退出通过。

LF前轮optimizer HDFS读取超过300秒，c1控制器898282退出，未发布第二轮commit。HdfsStore现在将-get/-put上限设为900秒且取immutable deadline剩余时间的较小值，metadata仍300秒。fork守护的首次子进程SIGSEGV；重试927765正使用标准HDFS CLI发布c2，只继承首轮已提交记录（不继承未接受attempt），准备期仍未完成。v9构建／双服务更新／deployment回读／唯一c2控制器启动由931053守护接管，日志/tmp/lowband-spot-c2/launch-v9.log。未改c1 run.json。

原独立GPU replay909147已收集两份SHA256核验本地产物后退出等待；929935仅用这些既有产物进行比较，动态选择c1/c2接受记录，不再启动GPU训练。原HTTP计数未checkpoint，比较报告明确不可用。远端SHA256读取ledger及完整数值审计继续运行。资源80次样本积分91.039 GPU·h（预热／失败／空闲皆包含；非账单），峰值16。

2026-10-25 03:53 UTC+8：v9/build170574/dbegmtn1enl8i32rdr60构建成功，镜像60c795746696cffcc06cd42a98b0440f；普通Bernard服务container_image更新经dry-run与双deployment官方GET回读通过，目标各8未变。A10新实例b5945e19fbcd4fbab71c28d81a97bd8d实测cuda_ready/runtime_ready均true，四项runtime完整性为true，真实blobroot=/opt/lowband-local-hl/worker/<instance>/blobs；此时A100仍无ready端点。

首轮完整数值审计已通过：两worker各290项AdamW参数状态、step128、cursor256，delta与optimizer有限，二阶矩非负，CPU/CUDA/Python RNG存在；全局参数和动量递推最大误差均0。其6份远端unique committed blobs合计19,762,318,094 bytes全部SHA256回读通过；LF optimizer实际回读398.096秒，支持提高大对象单命令上限但不改变deadline。5项传输／immutable deadline上限检查干净退出通过。c2 deadline为1792876701.522374（05:18:21），fork manifest发布仍在进行，未启动第二控制器。

2026-10-25 03:57 UTC+8：c2 fork-ready已发布并验证6份继承manifest与c1逐项一致；parent SHA未变，首轮state未变，131,072有效tokens。fork命令打印完成、所有记录回读通过，但命令包装器退出1，因标准HDFS客户端额外触发/opt下conf-refresh与pycache写被沙箱拒绝；不能称命令干净退出。c2启动已设置HADOOP_CONF_SAFE_REFRESH_RUNNING=true／PYTHONDONTWRITEBYTECODE=1，使用既有配置避免这些辅助写。

v9新副本官方发现5个A10／3个A100 ready。第一次后台c2控制器退出SIGSEGV，随后最小torch import三种环境均成功；具体退出原因未确认。后台重试未产日志即退出；前台运行可存活，现以执行会话15413将完整stdout/stderr保留在/tmp/lowband-spot-c2/controller-rounds.log。尚未接受第二轮update。首轮全量审计结果在gpu-commit-audit.partial.json。

2026-10-25 04:02 UTC+8：c2唯一控制器940717保持运行，已向v9新A10实例29bf393d5c4f4b10a597cfe60fa54025下发首轮commit参数及前轮optimizer/RNG/cursor；参数body上传1,976,222,459 bytes/24.842秒，recovery body上传3,952,504,290 bytes/49.859秒，恢复源读取另计9.383秒。第二轮A10已running，A100实例a9f53e1aa79246329845953a46032d93已分配，尚未接受第二轮update。c2输入SHA与独立GPU replay输入一致。原c1 hash/deadline不变。

首轮6份远端unique blobs合计19,762,318,094 bytes完整SHA256回读通过，完整optimizer和全局参数/动量递推审计通过；后续轮次仍待审计。最终GPU评测waiter已在同样的HADOOP_CONF_SAFE_REFRESH_RUNNING=true和PYTHONDONTWRITEBYTECODE=1环境重启，避免已知辅助写限制；尚未启动最终评测。资源82次样本积分93.759 GPU·h，峰值16，包含预热、失败与空闲，非账单；8+8保持不变。
