# TideTrain

受限网络与可抢占 GPU 上的低频同步训练、持久版本提交和恢复验证。

## 已完成的实测

Qwen2.5-0.5B 在两个真实单 GPU slot（A10、A100 PCIe 80GB）完成四轮 local-SGD，共 524,288 个接受训练 token。平台保留的 8+8 副本用于资源调度，实际训练 world size 为 2。

| 项目 | 结果 |
| --- | --- |
| 完整 FP32 验证 | 1,025 窗口、262,209 个目标 token，包含尾窗 |
| 验证 loss | 3.087654460 → 3.371856719 |
| Perplexity | 21.925590258 → 29.132567861，退化 32.87% |
| 恢复重放 | 不同 A10 实例的完整 delta、Adam 状态、RNG、游标逐项一致 |
| 状态审计 | 21 个内容寻址对象、67.192 GB；四轮参数和外层动量递推误差均为 0 |
| C3 续跑有效吞吐 | 393,216 token / 4,401.539 秒 = 89.336 token/s |
| 内存峰值 HWM | 控制器 11.683 GiB；串行完整状态审计 15.904 GiB |
| 资源采样 | 203.622 GPU·h，含预热、失败和闲置；不是账单 |

本次证明了提交和恢复链路的正确性，**没有证明训练质量收益、DDP 非劣性或百卡／千卡性能**。完整结果见 [实测报告](results/qwen05b-4round/REPORT.md)。

扩展改造已增加 safetensors 分片、流式归约、SQLite WAL 租约／fencing／CAS、紧凑恢复 manifest 和离线千卡启动入口。真实 Qwen checkpoint 的 CPU 分片审计误差为 0、峰值 RSS 1.61 GiB；11 项回归及两轮 CPU HTTP 恢复测试通过。已直接提交 1000 H20 与 1000 A10 请求；H20 启动失败，修正后的 A10 请求在 staging 后触发平台自动续试，目前仍待分配节点，尚无千卡性能结果。详见 [扩容执行记录](results/scale-20261025/REPORT.md)。

## 项目成果：STAR

**S — 场景。** Spot GPU 会被替换，GPU 容器不能访问共享 HDFS/NAS，参数与 Adam 状态达到 GiB 规模；成功的进程退出并不保证产物仍然存在。

**T — 责任。** 负责把 HTTP 调度链路扩展为有状态训练系统，确保旧实例、重复结果和部分上传不会推进全局版本或数据游标，并用实测核验恢复与成本。

**A — 行动。** 实现内容寻址对象、SHA256 校验、commit-last manifest、实例身份与 generation 校验、任务租约、唯一控制器、冷缓存恢复和按接受 token 加权的外层 Nesterov 更新。先把 delta 与恢复状态收齐再发布持久存储，并将全参数审计和相同精度的完整验证作为终验。

**R — 结果。** 完成 524,288 token 的四轮真实 GPU 实验，独立恢复重放逐项一致，21 份远端状态审计通过；定位中央状态 I/O 是扩展瓶颈。验证 perplexity 上升 32.87%，因此成果定位为训练系统正确性与恢复工程，不能写成模型效果提升。

后续扩展把归约改为有界分片并直接申请千卡；已有 CPU checkpoint 数学证据和真实平台提交／失败证据，尚未完成千卡训练验收。

## 代码和分支

- `main`：代码、使用说明、经过核验的聚合结果与执行计划。
- `research`：手稿、调研、对话轨迹、运行日志、原始接受记录和实验过程。该分支保留失败和负结果。

两个分支都不包含凭据或模型权重。实验中的内部地址、部署身份和操作路径在发布副本中替换为稳定匿名标签；原始 SHA256、数值与错误性质保留。

## 在同一个大目录下检出多个分支

使用 `git worktree`，每个子目录拥有独立工作区，共享一个 Git 对象库：

```bash
mkdir -p ~/projects/TideTrain
cd ~/projects/TideTrain
git clone https://github.com/Cheongroach/tidetrain.git main
git -C main fetch origin
git -C main worktree add ../research -b research origin/research
git -C main worktree list
```

目录布局为 `TideTrain/main/` 和 `TideTrain/research/`。可同时打开两个目录；同一个分支不能同时检出到两个 worktree。另开开发分支：

```bash
git -C main worktree add ../scale-dev -b scale-dev main
```

移除已不用的工作区前先提交或保存变更，再执行 `git -C main worktree remove ../scale-dev`。

## 本地入口

Python 3.11、PyTorch ≥2.2。GPU 路径需要支持 BF16 的 CUDA 环境；模型训练使用本地固定版本资产，不在 worker 内下载模型。

```bash
python -m pip install -r requirements.txt
python -m unittest lowband_training.test_correctness \
  lowband_training.test_http lowband_training.test_fork_run \
  lowband_training.test_spot_task_endpoints \
  lowband_training.test_scale lowband_training.test_scale_http \
  lowband_training.test_cuda_bootstrap lowband_training.test_watch_scale
python -m lowband_training.smoke --help
```

部署前配置自己的发现提供器、HTTP token 和控制侧存储。CPU 协议测试不能替代真实 GPU、HTTP 多节点或 HDFS 性能实验。补充尾窗评测回归断言曾通过，但执行包装器因 `/proc` 线程命名限制返回 1；此限制单独保留在结果中。

## 百卡、千卡扩展

计划和执行状态见 [扩展计划](docs/SCALE_PLAN.md)。按用户要求直接申请千卡；小规模诊断与千卡请求并行。质量对照使用固定 token 预算，申请或排队不等同于实测完成。

`prepare_scale` 生成可检查的完整 payload，不自动提交；使用自己的队列、镜像、可达网关和 token 文件：

```bash
python -m pip download --only-binary=:all: --no-deps \
  safetensors==0.4.5 --dest runtime/wheels
python -m lowband_training.prepare_scale --help
# H20: --nodes 125 --gpus-per-node 8 --gpu-type NVIDIA_H20
# A10: --nodes 250 --gpus-per-node 4 --gpu-type NVIDIA_A10
# --dependency-wheel 必须指向与镜像 CPython 3.11、平台架构匹配的 wheel
```

按生成的 `config.json` 启动 `scale_gateway`，然后依平台流程预检、合并预检标识并提交。每个实验使用独立 endpoint／账本；当前网关是单宿主机实现，多区域聚合和 HA 尚待实现。
