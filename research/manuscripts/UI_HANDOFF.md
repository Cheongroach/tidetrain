# HTTP worker 平台配置交接

2026-10-24 14:30（UTC+8），控制面 `cn`，Bernard 服务。两个服务的 HTTP CMD/token/slot、模型 `2.0`、镜像和规格已回读确认。本文件的配置操作已经完成。

Spot-UCE 预约 **HL #16689 / GPU Center #40393**、**LF #16688 / GPU Center #40392** 已创建，各申请 1 张 A10、离线训练、00:00–00:00，原始 YAML 均确认当天 **17:00 到期**。当前交付均为 0/1，没有真实 GPU 训练结果。一次性控制进程已启动，状态见 `spot_run.json`。

## 已完成的模型交付

模型：`search_life_service_infra_lowband_train`，版本 `2.0`。

平台下发来源：

```text
<controller-storage-uri><operator>/lowband_training/models/http-worker-f637c697cf13
```

目录含 `package-manifest.json`、`config.json`、`assets/`、`wheels/` 和 `lowband_training/`。48 个文件已发布并逐文件回读校验。GPU 只读平台下发的目录，不直接访问上述 HDFS URI。

## 两个服务的必要更新

| 字段 | HL | LF |
| --- | --- | --- |
| 服务 | `<configured-service>` | `<configured-service>` |
| 页面 | [HL 更新入口](<platform-url>) | [LF 更新入口](<platform-url>) |
| `LOWBAND_ZONE` | `hl` | `lf` |
| `LOWBAND_HTTP_TOKEN` | 从受控本地文件注入 | 使用相同值 |
| `LOWBAND_SERVICE_SECONDS` | 可省略，默认 `10740` | 同左 |
| `LOWBAND_HTTP_PORT` | 可省略，读取平台 `PORT0` | 同左 |

替换旧 `LOWBAND_RUNTIME` NAS 变量为 `LOWBAND_HTTP_TOKEN`。共享 token 已生成于本工作区 `lowband_training/.secrets/http-token`，权限 `0600`，目录已加入 `.gitignore`；值不写入本文件、离线包或 Notebook。

保留 `LOWBAND_RUN_ID=qwen05b-spot-20261021-a1`、两站的 `LOWBAND_ZONE`、`MERLIN_COPILOT_SERVICE_PORT_INDEX=6` 和 `MESH_AGENT_SIDECAR_ENABLE_SEED_MERLIN_COPILOT=1`。旧 probe/startup timeout 变量不被 HTTP 入口读取。

CMD：

```bash
timeout --signal=TERM --kill-after=30s 10770s /bin/bash -lc 'set -euo pipefail; export LOWBAND_PACKAGE_DIR="${BERNARD_MODEL_PATH:?platform model path is absent}"; test -s "$LOWBAND_PACKAGE_DIR/package-manifest.json"; exec /bin/bash "$LOWBAND_PACKAGE_DIR/lowband_training/http_service_entry.sh"'
```

HL 浏览器草稿使用无内嵌双引号的等价命令，避免自动填充过程中的引号转义：

```bash
timeout --signal=TERM --kill-after=30s 10770s /bin/bash -lc 'set -eu; export LOWBAND_PACKAGE_DIR=${BERNARD_MODEL_PATH:?}; test -s ${LOWBAND_PACKAGE_DIR}/package-manifest.json; exec /bin/bash ${LOWBAND_PACKAGE_DIR}/lowband_training/http_service_entry.sh'
```

后者要求平台模型目录不含空白字符；平台 GPU 路径仍需启动验证。

保留规格 **16 CPU / 64 GB / 1 GPU**，ICM 镜像 **1488235 / devbox.pytorch2 / 1.0.0.3**，**7 个端口**，开源模型选项 **否**，调试协议 **HTTP**，不挂载 ByteNAS/HDFS。服务发现 tags 保留原值。HTTP 调试协议不是监听端口配置，运行后必须验证 `PORT0` 的真实可达性。

## 保存过程与回读

此前浏览器多次超时，曾误判禁用“服务 ID”字段为空校验错误。实际更新流程有“确认提交”和“服务配置对比”两层确认，完成确认后保存成功。LF 密钥值已填写但 key 仍为 `LOWBAND_RUNTIME`，现已修正为 `LOWBAND_HTTP_TOKEN`。UI 全量更新清空了模型名称/版本，已通过官方 CLI 部分字段更新恢复为 `2.0`。

普通 Bernard 服务 env/CMD 未被 CLI update 支持。[merlin-service 技能](file:///home/tiger/.trae-cn/skills/merlin-service/SKILL.md)明确“`service bernard-services update` 只改普通服务的少数字段，**不含 env**”。本次使用 UI 保存 env/CMD，用官方 CLI 恢复模型绑定，没有拼接 service GET 响应自行 PUT。

最终 CLI 已回读两侧 model binding、CMD、env、镜像和规格；token 与本地文件一致，验证输出不包含密钥值。

## 预约约束和验收顺序

每站 **1 GPU**，只使用 **Spot-UCE**，优先离线训练、其次数据处理；需求时段 **00:00–00:00**，当前份额为零也可按用户授权提交。但预约必须另有可核验的精确到期，包含准备和等待在内每站总占用最多 **10800 秒**，两站合计最多 **6 GPUh**。未找到能执行这一限制的预约字段前，不提交无限续约合约。进程 timeout 不释放或限制重新拉起的副本。

本次直接验证 Spot-UCE 表单支持日期、小时、分钟和秒的有效期选择。两份 GPU Center YAML 的 `intent.time_window.end_time` 均为 `1792832400`，对应当天 17:00（UTC+8）。订单分别于 14:22 / 14:24 创建；从创建时刻起最多可占用约 5.22 GPUh。实际交付和到期释放仍需继续验证。

配置及有限合约生效后：

1. 回读部署、实际/健康实例数和各站唯一 endpoint。
2. 用 `http_probe.py` 验证真实 HTTP 上传/下载 SHA256；由控制端完成 HDFS immutable 提交。
3. 用 `dorado_http_training.ipynb` 或独立 `http_controller` 发起训练；回收 delta、Adam/RNG/数据游标后才能提交轮次。
4. 收集 CUDA 训练结果、最终 round manifest、有效 token、传输字节及实例起止时间；验证到期释放后再核算实际 GPUh。

CPU 正确性、离线安装和模型绑定只构成准备证据。没有 endpoint、训练结果及释放回读时，不能报告 GPU 吞吐、跨机加速或端到端成功。
