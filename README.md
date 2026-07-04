# Codex History Sync Tool

macOS 单文件 Python CLI，让你在 `cc-switch` 里在 **官方 Codex（ChatGPT OAuth）** 和 **中转站（自定义 `model_provider`）** 之间来回切换时，本地会话历史始终是同一套、不会因为 provider 变化而消失或分裂。

设计上**取并集，不强制覆盖**：只统一共享配置（`sqlite_home`、`[history]`、`mcp_servers`、`marketplaces`、`plugins`、`projects`、`features`、`desktop`、`memories`），**绝不**改写每个 provider 自己的 `model_provider`、`[model_providers.*]`、`base_url`、`model`、`disable_response_storage`。所以 `default` 可以稳稳指向官方 OpenAI，其它 provider 各自指向自己的中转站，互不干扰。

> **平台**：本仓库只针对 macOS。Windows 用户请用 [@Roxy-ljy/codex-cc-switch-history-sync](https://github.com/Roxy-ljy/codex-cc-switch-history-sync)（本项目参考了它的思路，收敛成了更贴合 macOS 个人环境的单文件版本）。

## 解决的问题

`cc-switch` 切换 provider 时，Codex 可能因为 `model_provider`、`sqlite_home`、本地索引或 state DB 不一致，导致历史记录只在某一边可见。这个工具把共享配置对齐 + 从 rollout 文件重建索引，让历史在所有 provider 下都可见。

它**只做本机状态同步，不上传任何会话**。

## 会修改哪些内容

- `~/.cc-switch/settings.json`（打开 `unifyCodexSessionHistory` / `preserveCodexOfficialAuthOnSwitch`）
- `~/.cc-switch/cc-switch.db`（各 provider 模板的共享块并集）
- `~/.codex/config.toml`（当前 provider 的 live 配置）
- `~/.codex/session_index.jsonl`（从 rollout 重建，bucket 无关）
- `~/.codex/state_5.sqlite`、`~/.codex/sqlite/state_5.sqlite`（补回缺失线程、同步标题）
- `~/.codex/sessions/**/*.jsonl`、`~/.codex/archived_sessions/**/*.jsonl`（修正 rollout 的 `session_meta.payload.id`）

它**不会**碰 `auth.json`，也**不会**强制改写任何 provider 的 `model_provider` / `base_url` / `model_providers.*` 块。

## 用法

把脚本放在任意目录，例如 `~/codex-history-sync-tool/codex_history_sync.py`。下面用 `./codex_history_sync.py` 指代它。

先看当前状态：

```bash
python3 ./codex_history_sync.py status
```

先演练，不写入：

```bash
python3 ./codex_history_sync.py --dry-run sync
```

执行一次真实同步（取并集，备份后再写）：

```bash
python3 ./codex_history_sync.py sync
```

后台盯住 `cc-switch` 切换，切完自动再同步：

```bash
python3 ./codex_history_sync.py watch --interval 2
```

### 让某个 provider 指向官方

如果你有一个 provider（比如 `default`）本该走官方 OpenAI，但模板里被塞了 `model_provider = "custom"` + `[model_providers.custom]` 中转块，用 `set-official` 把这些路由剥掉，让它回到 ChatGPT OAuth：

```bash
# 先看会改什么
python3 ./codex_history_sync.py --dry-run set-official default

# 确认后真正执行（会先备份）
python3 ./codex_history_sync.py set-official default
```

它只会剥掉 `model_provider = "..."`、空的 `[model_providers]` 头、以及所有 `[model_providers.<key>]` 块；如果该 provider 正是当前激活的，`~/.codex/config.toml` 也会同步修好。

## 备份位置

每次真实写入都会备份到：

```text
~/.cc-switch/backups/codex-history-sync-tool/<时间戳>
```

包含 `cc-switch.db`、`config.toml`、`settings.json`、`session_index.jsonl`、各 state DB 的快照。

## 设计原则

- **取并集，不强制覆盖**：`model_provider` / `model_providers.*` / `base_url` / `disable_response_storage` / `model` 全部保留每个 provider 自己的值，不做统一。
- `[model_providers.*]` **不**进入并集——否则会把中转站的路由块复制进官方 provider。
- `sqlite_home` 和 `[history] persistence = "save-all"` 会统一（共享基础设施 + 历史功能本身）。
- `session_index.jsonl` 是 bucket 无关的（只有 `id` / `thread_name` / `updated_at`），从所有 rollout 重建，所以官方和中转两边看到的是同一份历史列表。
- state DB 不再强制把 `threads.model_provider` 改成单一 bucket；每个线程保留它创建时的 provider。

## 参考

- [@Roxy-ljy/codex-cc-switch-history-sync](https://github.com/Roxy-ljy/codex-cc-switch-history-sync) —— Windows 版本与原始思路来源。
