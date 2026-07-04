# Codex History Sync Tool

macOS 单文件 Python CLI，让你在 `cc-switch` 里在 **官方 Codex（ChatGPT OAuth）** 和 **中转站（自定义 `model_provider`）** 之间来回切换时，本地会话历史始终是同一套、不会因为 provider 变化而消失或分裂。

设计上分两层处理：

- **provider 配置层**：取并集，不强制覆盖。只统一共享配置（`sqlite_home`、`[history]`、`mcp_servers`、`marketplaces`、`plugins`、`projects`、`features`、`desktop`、`memories`），**绝不**改写每个 provider 自己的 `model_provider`、`[model_providers.*]`、`base_url`、`model`、`disable_response_storage`。
- **本地历史可见性层**：执行 `sync` 时，会先从本机所有 rollout / state 里**取并集**，再把这份并集统一对齐到**当前激活 provider 的历史 bucket**。也就是：你切到官方后跑一次 `sync`，官方侧会看到完整并集；切到中转后再跑一次 `sync`，中转侧也会看到同一份完整并集。

> **平台**：本仓库只针对 macOS。Windows 用户请用 [@Roxy-ljy/codex-cc-switch-history-sync](https://github.com/Roxy-ljy/codex-cc-switch-history-sync)（本项目参考了它的思路，收敛成了更贴合 macOS 个人环境的单文件版本）。

## 解决的问题

`cc-switch` 切换 provider 时，Codex 可能因为 `model_provider`、`sqlite_home`、本地索引或 state DB 不一致，导致历史记录只在某一边可见。这个工具把共享配置对齐 + 从 rollout 文件重建索引，让历史在所有 provider 下都可见。

它**只做本机状态同步，不上传任何会话**。

## 适用前提

- 仅支持 **macOS**
- 本机已安装并正常使用 **Codex Desktop**
- 本机已安装并正常使用 **cc-switch**
- 历史记录仍然存在于本机 `~/.codex/sessions` / `~/.codex/archived_sessions` rollout 文件中

如果 rollout 文件本身已经被删除，这个工具也无法“凭空恢复”那部分历史。

## 会修改哪些内容

- `~/.cc-switch/settings.json`（打开 `unifyCodexSessionHistory` / `preserveCodexOfficialAuthOnSwitch`）
- `~/.cc-switch/cc-switch.db`（各 provider 模板的共享块并集）
- `~/.codex/config.toml`（当前 provider 的 live 配置）
- `~/.codex/session_index.jsonl`（从 rollout 重建，bucket 无关）
- `~/.codex/state_5.sqlite`、`~/.codex/sqlite/state_5.sqlite`（补回缺失线程、同步标题、把 `threads.model_provider` 对齐到当前 bucket）
- `~/.codex/sessions/**/*.jsonl`、`~/.codex/archived_sessions/**/*.jsonl`（修正 rollout 的 `session_meta.payload.id`）
- `~/.codex/sessions/**/*.jsonl`、`~/.codex/archived_sessions/**/*.jsonl`（把 `session_meta.payload.model_provider` 对齐到当前 bucket）

它**不会**碰 `auth.json`，也**不会**强制改写任何 provider 配置自身的 `model_provider` / `base_url` / `model_providers.*` 块。

## 快速开始

把脚本放在任意目录，例如 `~/codex-history-sync-tool/codex_history_sync.py`。下面用 `./codex_history_sync.py` 指代它。

建议第一次先看状态，再做一次 dry-run：

```bash
python3 ./codex_history_sync.py status
python3 ./codex_history_sync.py --dry-run sync
```

执行一次真实同步（先对本地历史取并集，再把并集对齐到当前 provider，备份后再写）：

```bash
python3 ./codex_history_sync.py sync
```

后台盯住 `cc-switch` 切换，切完自动再做“取并集 + 对齐当前 bucket”：

```bash
python3 ./codex_history_sync.py watch --interval 2
```

如果你希望“切换完 provider 后不再手动跑脚本”，`watch` 是推荐用法。

## 典型工作流

### 场景 1：平时手动切换 provider

1. 在 `cc-switch` 里切到你想用的 provider
2. 执行 `python3 ./codex_history_sync.py sync`
3. 打开 / 回到 Codex Desktop

结果是：当前 provider 会看到“本机全部历史的并集”。

### 场景 2：希望官方和中转两边都始终能看到同一份历史

开一个后台 watcher：

```bash
python3 ./codex_history_sync.py watch --interval 2
```

这样每次 `cc-switch` 的当前 provider 变化后，脚本都会自动重做一次“取并集 + 重打当前 bucket”。

### 场景 3：`default` 被错误改成走中转

用 `set-official` 把 provider 配置里的中转路由剥掉：

```bash
python3 ./codex_history_sync.py --dry-run set-official default
python3 ./codex_history_sync.py set-official default
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
~/.codex/backups/codex-history-sync-tool/<时间戳>
```

虽然备份里仍会包含 `cc-switch.db` / `settings.json` 的快照，但备份根放在 `~/.codex`，因为这个工具的主目标是维护 Codex 本地历史，而不是维护 `cc-switch` 本身。

## 设计原则

- **取并集，不强制覆盖**：`model_provider` / `model_providers.*` / `base_url` / `disable_response_storage` / `model` 全部保留每个 provider 自己的值，不做统一。
- `[model_providers.*]` **不**进入并集——否则会把中转站的路由块复制进官方 provider。
- `sqlite_home` 和 `[history] persistence = "save-all"` 会统一（共享基础设施 + 历史功能本身）。
- `session_index.jsonl` 是 bucket 无关的（只有 `id` / `thread_name` / `updated_at`），从所有 rollout 重建，所以官方和中转两边看到的是同一份历史列表。
- state DB 和 rollout 的 `model_provider` 会在每次 `sync` 时统一改成**当前激活 provider 的历史 bucket**；但改写前的数据源是本机所有 rollout / state 的**并集**，所以不管你刚刚用的是官方还是中转，切到另一边再同步，看到的仍然是同一套历史。

## 不会做什么

- 不会修改 `auth.json`
- 不会上传、下载、云同步任何会话
- 不会强制统一每个 provider 自己的 `base_url` / `model` / `model_provider`
- 不会恢复已经从本机 rollout 文件里彻底删除的历史

## 排查

### 1. `status` 里 state DB 数量对，但 Codex 里还是空白

先确认当前 provider 是否真切到了你想看的那一边，然后重新执行：

```bash
python3 ./codex_history_sync.py sync
```

如果你经常切换，直接改用：

```bash
python3 ./codex_history_sync.py watch --interval 2
```

### 2. 官方有历史，中转没历史；或中转有历史，官方没历史

这通常不是“历史丢了”，而是本地历史标签还停留在另一边的 bucket。切到当前想看的 provider 后，再执行一次：

```bash
python3 ./codex_history_sync.py sync
```

### 3. `default` 明明应该走官方，却还在走中转

先检查该 provider 模板里是不是残留了：

- `model_provider = "custom"`
- `[model_providers.custom]`

如果有，使用上面的 `set-official default`。

## 安全性

- 每次真实写入前都会自动备份
- `--dry-run` 可先演练
- 这个工具优先修“可见性”和“索引一致性”，不碰 OAuth 登录态

## 参考

- [@Roxy-ljy/codex-cc-switch-history-sync](https://github.com/Roxy-ljy/codex-cc-switch-history-sync) —— Windows 版本与原始思路来源。
