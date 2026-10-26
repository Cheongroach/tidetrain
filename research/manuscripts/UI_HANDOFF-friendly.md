# 平台配置交接：保存字段之后还要验证什么

这份交接描述两卡 HTTP 基线的历史配置，不是当前新预约。配置保存并回读过，但当时两笔各一张 A10 的有限订单仍待交付；真正的训练证据在后来的[两卡终验](../../results/qwen05b-4round/REPORT-friendly.md)。

## 模型目录提供完整离线包

模型版本 2.0 曾绑定一个含 48 个文件的解包目录，文件逐个回读校验。目录有配置、assets、wheels 和代码。GPU 使用平台下发路径，不直接访问控制端 HDFS URI。

配置中的 `LOWBAND_PACKAGE_DIR` 从 `BERNARD_MODEL_PATH` 取得。若目录不存在或只有 tar 文件，HTTP 服务就没有完整运行输入。

## 最容易混淆的字段

| 字段 | 实际作用 |
| --- | --- |
| HTTP debug 协议 | 平台调试方式，不会替程序监听端口 |
| `PORT0` | 原入口默认读取的监听端口，需要实测连通 |
| `LOWBAND_HTTP_TOKEN` | 控制端和 worker 的相同随机凭据，不写进文档或代码 |
| `LOWBAND_ZONE` | 逻辑槽位标签，不保证物理地域 |
| `LOWBAND_SERVICE_SECONDS` | 服务进程时限，不是资源归还动作 |
| 模型名称／版本 | 指向完整离线资产，保存其他字段时不能意外清空 |

当时规格为 16 CPU／64 GB／单实例一 GPU，配置七个端口、开源模型选项“否”、HTTP debug。低层服务健康检查是否正确，仍要看真正端口和训练子进程。

## 一次完整配置操作的验收顺序

1. 检查已解包目录和 `package-manifest.json`，核对模型名称／版本绑定。
2. 在支持的 UI 字段中保存 CMD、env、端口与规格；完成两层确认后再回读。
3. 核验 deployment 的镜像和实例身份；HTTP health 只代表服务存活。
4. 实测 HTTP／持久存储回环，再验证 CUDA 训练和完整产物。
5. 发布全局 commit 后另查预约、deployment 和副本释放，不用 completed.json 代替资源终态。

这是核对顺序，不表示此时获准执行新的资源变更。

## 曾发生过的配置问题

LF 曾把密钥值填入旧 key `LOWBAND_RUNTIME`，后来更正为 `LOWBAND_HTTP_TOKEN`。UI 全量保存还清空过模型绑定，通过官方 CLI 部分字段更新恢复。禁用字段和双层确认曾被误读成表单错误。

普通 Bernard CLI 的部分更新不支持所有 env／CMD 字段。应按已确认的官方能力用 UI 保存并回读，不把 GET 响应拼成未知 PUT 请求。

## 为什么保留原文

原文保存确切的命令、字段和预约到期回读，便于查历史差异；其中短期限不是千卡等待策略。更易读的通信原理在[部署说明](../../docs/HTTP_DEPLOYMENT-friendly.md)，具体历史字段在[原交接](UI_HANDOFF.md)。

---

原文：[UI_HANDOFF.md](UI_HANDOFF.md)。保留历史操作、配置或研究推导，便于核对当时状态。

阅读版整理于 2026-10-26；历史状态不作当前运行承诺。
