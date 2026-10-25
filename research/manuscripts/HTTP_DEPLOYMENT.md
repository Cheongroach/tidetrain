# Dorado 主动调用 Spot GPU 的训练入口

2026-10-25：8×A10 与 8×A100-PCIE-80GB 已下发，两个 slot 的 1 MiB HTTP/HDFS 回环通过，初始全局 checkpoint 已持久化。首次 HL GPU 子进程退出 1，当前控制器暂停，尚无成功 GPU 更新。诊断镜像 **ICM2424182 / 1.0.0.3** 已构建成功，两端镜像绑定和下发均已完成并回读，新控制器已恢复。见 [精确变更与续跑步骤](ICM_V3_HANDOFF.md)。Dorado Executor 仍未实测。

2026-10-24 17:32（UTC+8）：原Spot-UCE窗口已结束。预约HL #16689 / LF #16688各1张A10，17:00到期；守护16:57删除两个deployment，平台删除记录与空列表已回读。控制端未发现两地端点，17:00退出，训练未启动。独立镜像ICM2424182 v1.0.0.2已构建并部署HL，LF更新仍受浏览器超时阻塞，详见 [Spot 执行记录](SPOT_STATUS.md)。

下一轮入口支持 `--deployment-file`，用官方deployment tasks与认证HTTP发现端点，无需依赖聊天SDK注册。文件包含 `cli`、`psm`、`deployments`（worker映射到deployment_id/service_id）、`image_url`、`deadline`；截止时间必须等于控制入口的订单截止时间，卡槽必须匹配训练配置。4项CPU测试通过，尚未GPU验证。新预约需明确新的有限窗口，原订单不续期。

GPU 端没有 HDFS/ByteNAS 访问依赖。镜像提供 Python 3.11 和平台 PyTorch，平台下发的模型目录提供离线包，临时文件写容器本地盘。Dorado 控制进程发起全部 HTTP 连接，并独自读写 HDFS。平台模型目录必须是完整解包目录，不能把 tar 文件路径直接当目录。

## 交付文件

- `http_worker.py`：HTTP 服务与独立训练子进程，单 GPU 单任务，实例 UUID、任务幂等、续租和超时终止。
- `http_transport.py`：最大 4 MiB 分块、上传偏移查询、重复块校验、下载续传、完整 SHA256 校验。
- `http_controller.py`：持久化 dispatch 意图后发任务，按同一实例轮询/取回；结果持久化并校验后才提交轮次。
- `hdfs_store.py`：仅控制端使用的 HDFS CLI 后端；先上传临时文件，再 rename 发布 immutable manifest。权限/网络错误不会被当作文件不存在。
- `dorado_discovery.py`：使用本机已核对的 `LLMClient` 公共发现接口，按 service ID 过滤并保持 endpoint 选择，兼容 IPv6；不调用聊天 API。
- `http_probe.py`：无需 torch 的 HTTP 往返及可选 HDFS 提交预检。
- `spot_controller_entry.py`：控制端一次性等待/预检/训练入口，使用订单绝对截止时间，等待期间不初始化训练或占用额外 GPU。
- `spot_task_endpoints.py`：官方CLI实例发现，校验镜像/新replicaset、认证HTTP、worker/配置哈希/实例UUID，到期清空地址缓存。
- `spot_budget_guard.py`：一次性指定deployment释放与采样记录；删除提交与平台移除分别核验。
- `dorado_http_training.ipynb`：供新建独立 Dorado Notebook 使用的入口，不修改用户的现有 Notebook。
- `dist/http-worker-v1.tar.gz`：初始模型、分词数据、代码和完整 wheelhouse，文件哈希见包内 `package-manifest.json`。

## GPU 服务配置

保留现有两个独立服务，均保持 16 CPU / 64 GB / 1 GPU。模型绑定为本次完整离线包：`modelzoo://`、`search_life_service_infra_lowband_train`、`2.0`。环境变量和 HTTP CMD 已保存并回读；GPU 容器内的启动及目录/端口仍待运行验证。

必要环境变量：

| 变量 | 值或来源 |
| --- | --- |
| `LOWBAND_PACKAGE_DIR` | CMD 从平台 `BERNARD_MODEL_PATH` 读取；启动时校验目录内 manifest，实际 GPU 路径仍待确认 |
| `LOWBAND_ZONE` | HL 服务为 `hl`；LF 服务为 `lf` |
| `LOWBAND_HTTP_TOKEN` | 每次运行的共享随机密钥，通过受控环境变量注入；不写入包或 Notebook |
| `LOWBAND_HTTP_PORT` | 可省略，省略时读取平台 `PORT0` |
| `LOWBAND_HTTP_HOST` | 默认 `::`，实际验证 IPv6/IPv4 可达性 |
| `LOWBAND_SERVICE_SECONDS` | 默认 10740 秒；外层启动命令另设总超时 |

建议 CMD（不在 GPU 端使用 HDFS/NAS 路径）：

```bash
timeout --signal=TERM --kill-after=30s 10770s /bin/bash -lc \
  'set -euo pipefail; export LOWBAND_PACKAGE_DIR="${BERNARD_MODEL_PATH:?platform model path is absent}"; test -s "$LOWBAND_PACKAGE_DIR/package-manifest.json"; exec /bin/bash "$LOWBAND_PACKAGE_DIR/lowband_training/http_service_entry.sh"'
```

启动脚本校验全包哈希，离线安装全部 wheels 到本地 venv，再监听 HTTP 端口。保留镜像 PyTorch，不在 GPU 运行时访问 pip/HDFS/NAS。`/health` 表示 HTTP supervisor 存活，不能据此断言 CUDA 或训练成功；训练子进程的结果才是训练证据。

配额继续使用 Spot-UCE，每站 1 卡、00:00–00:00 的需求窗口，但**合约必须另有精确有限到期时间**，总占用每站不超过三小时，两站合计六卡时。进程超时不释放副本，也不能代替平台到期/释放验证。

本次订单分别创建于 14:22:02 / 14:24:50，到期 17:00；从实际创建时刻起，最多可占用约 5.22 GPUh。YAML 的 `start_time` 比页面提交时间早八小时，这只是订单时间窗口字段，不能据此虚构此前的资源交付或 GPUh。实际成本必须从交付流水和实例起止时间核算。

## Dorado 接入

先把离线包放到控制端可读目录并解包。预检只需标准库；训练协调器需要 CPU PyTorch、包内依赖和环境中的 `bytedmerlin`。Spark 不是两个固定 worker 的必要条件。推荐一个独立控制进程，不把有状态控制循环放进可独立重试的 `mapPartitions`。若使用 Spark，关闭 speculation，并由唯一控制端提交全局轮次。

服务发现配置在 `dorado_services.json`。`GPU_COUNT` 不申请 GPU，本入口也不从 endpoint 数推导或扩大 GPU 配额。

```python
import json, os
from lowband_training.dorado_discovery import BernardEndpoints
from lowband_training.http_probe import probe

provider = BernardEndpoints(**json.load(open("dorado_services.json")))
endpoints = provider()
# 零 endpoint 时仅等待资源交付，不启动训练。
for worker, endpoint in endpoints.items():
    print(probe(endpoint, os.environ["LOWBAND_HTTP_TOKEN"],
        "<controller-storage-uri><operator>/lowband_training/http-validation"))
```

正式控制端 CLI 接收完整路径，不需要在 GPU 注入 HDFS 地址：

```bash
python -m lowband_training.http_controller \
  --config "$LOWBAND_PACKAGE_DIR/config.json" \
  --assets "$LOWBAND_PACKAGE_DIR/assets" \
  --services lowband_training/dorado_services.json \
  --root "<controller-storage-uri><operator>/lowband_training/runs/$LOWBAND_RUN_ID" \
  --cache "/tmp/lowband-control-$LOWBAND_RUN_ID" \
  --run-id "$LOWBAND_RUN_ID"
```

若 Dorado 使用其他 HDFS CLI 前缀，可传 `--hdfs-command '["hadoop","fs"]'`，只接受 argv 数组，不拼 shell。`--endpoints` 可替代 `--services`，读取 `{ "hl": "http://host:port", "lf": "http://[ipv6]:port" }` 文件。

## 恢复语义和边界

1. `run.json` 保存原始 deadline，控制端恢复不会延长运行时限。
2. `dispatch/<round>/<worker>/<generation>.json` 先持久化，再提交 HTTP job。POST 响应丢失后查同一 `job_id`，不立即重复训练。
3. 每次服务启动生成新的 instance UUID。所有 stateful HTTP 请求携带 UUID，IP/端口复用不能冒充旧实例。
4. 更换实例先推进 dispatch generation；旧 generation 的结果不被接受。停止旧续租，旧计算由租约到期终止，可能存在不超过租约窗口的重复计算。
5. delta、Adam 状态、RNG、数据游标都到达控制端并持久化、校验后才写 update manifest；全局 round manifest 发布后才推进全局版本。下载中断时不推进数据游标。
6. 一个活跃控制进程是部署前提。当前仅有本地文件锁，没有分布式 leader election/CAS fencing；必须由 Dorado 作业编排保证全局单实例，不能同时运行两份 Notebook。
7. controller 退出会停止续租；GPU 任务租约默认 120 秒。等待实例和失败重试受持久化 deadline 约束，每 worker/round 最多四代 attempt。
8. 完整恢复状态传输较大：Qwen 0.5B FP32 delta 约 1.84 GiB、Adam 状态约 3.68 GiB/worker/round；还需计入模型下发、失败重试与 HDFS 读写。`http_sent_bytes` / `http_received_bytes` 只统计成功响应对应的应用层 payload，不代表物理链路总字节，也未覆盖失败请求的全部流量。
9. 当前 controller 顺序上传/回收两侧 blob，两个 GPU 的训练子进程可同时计算。未实现传输流水线和分布式聚合；先保证正确性，不能据此宣称吞吐加速。
10. 每次训练子进程重新加载模型/optimizer；尚未优化 GPU 常驻模型和 CPU staging 内存。HTTP supervisor 默认 blob quota 24 GiB，输出/临时空间的实际峰值仍需 GPU 环境验证。
11. bearer token 通过内网 HTTP 传输，当前没有内置 TLS。仅用于受控测试网络；如平台要求 TLS/鉴权网关，需按实际入口适配，不能把它作为公网服务部署。

## 已验证与待验证

已验证：本机 CPU、真实 localhost HTTP、分块重试、完整哈希、重复 POST、租约过期、旧实例/旧 generation 拒绝、worker 与控制端冷重启、最终参数逐元素一致；真实控制端 HDFS 的 1 MiB 写入/rename/冷读取及 immutable manifest 冲突拒绝；离线 wheelhouse 安装和 Qwen import；48 文件离线目录发布及逐文件哈希回读；模型版本 2.0 创建及两侧服务绑定；HTTP 配置回读；Spot-UCE 预约创建及绝对到期字段。

未验证：Dorado Executor 网络与身份、平台模型目录具体路径、原镜像中的完整启动、实际 GPU 数值运行、跨机房 HTTP 带宽、平台合约到期释放。预约已提交，当前仍等待交付，不能报告 GPU 吞吐或跨机加速。本次后台控制进程运行在开发机控制端，不代表已在 Dorado Executor 执行。

本机复验：

```bash
CUDA_VISIBLE_DEVICES='' python3 -m unittest lowband_training.test_http lowband_training.test_correctness -v
```
