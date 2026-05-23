# Phase 1: Tag Match

## Goal

为已下载论文分配主题标签，并把匹配结果写入共享数据库。当前 `paper_cli.py`
的 Phase 1 只执行标签匹配；不会因为相关性不足而拒绝论文，也不会写
`skipped.json`。

`filter_relevance.py` 仍可作为手动/辅助检查脚本使用，但它没有接入默认
`paper_cli.py` Phase 1 流程。

## Input

- 论文元数据 JSON：`{paper_dir}/{paper_id}.metadata.json`
- 数据库中的 paper 行（由 `paper_cli.py` 读取）
- `config.yaml` 中的 `tags.definitions`、`tags.base_tag_ids`、`tags.ai_parent_tag_id`

找到 paper 目录：

```bash
PAPER_DIR="data/$(python3 -c "
import sys; sys.path.insert(0, 'scripts')
from paper_db import get_conn, get_paper_dir
import yaml
config = yaml.safe_load(open('config.yaml'))
conn = get_conn(config)
print(get_paper_dir(conn, '{paper_id}') or '')
")"
```

## Workflow

### Step 1: Read Paper Metadata

```bash
python3 - <<'PY'
import json
print(json.dumps(json.load(open('$PAPER_DIR/{paper_id}.metadata.json')), ensure_ascii=False, indent=2))
PY
```

确认 JSON 包含 `title`、`abstract`、`doi` 等字段。`paper_cli.py` 会把 metadata
和数据库中的 paper 字段合并后传给标签匹配逻辑。

### Step 2: Match Topic Tags

默认 CLI 流程等价于运行 `match_tags.py`：

```bash
python3 skills/bio-paper-interpreter/scripts/match_tags.py \
  --config config.yaml < $PAPER_DIR/{paper_id}.metadata.json
```

脚本逻辑：
- 用 `config.yaml` 中 `tags.definitions` 的正则模式匹配标题和摘要。
- 基础标签（`tags.base_tag_ids`，默认 `[2, 9]`）始终包含。
- 如果匹配到 ML/DL/LLM/AF 标签，自动添加 AI 父标签（`tags.ai_parent_tag_id`，默认 `1`）。

### Step 3: Update Database

`paper_cli.py` 调用 `update_tags()` 把匹配结果写入 `papers.matched_tags`，然后记录：

```text
Phase 1 - COMPLETED: {paper_id} — {n} tags: {labels}
```

当前默认流程中，Phase 1 成功后总是继续 Phase 2。

## Optional Manual Relevance Check

如果需要在默认 CLI 之外手动检查关键词相关性，可以运行：

```bash
python3 skills/bio-paper-interpreter/scripts/filter_relevance.py \
  --config config.yaml < $PAPER_DIR/{paper_id}.metadata.json
```

该脚本会输出带 `relevance` 字段的 JSON，并根据 `config.yaml` 中的包含/排除关键词给出
`passed`、`reason`、`include_matches`、`exclude_matches`。是否据此跳过论文需要人工或额外流程决定；
默认 `paper_cli.py` 不会自动调用它。

## Output

- 数据库 `papers.matched_tags` 字段被更新。
- `data/execution_log.md` 记录 `Phase 1 - COMPLETED`。
- 不生成 Phase 1 文件输出。

## Rules

1. 默认 Phase 1 只做标签匹配，不做相关性拒绝。
2. 标签来自 `config.yaml`，不要在文档或代码中硬编码新标签。
3. 基础标签始终保留；额外标签由正则命中决定。
4. 不修改 `config.yaml`。
5. 手动运行 `filter_relevance.py` 时，要明确它是辅助检查，不是默认 CLI 状态机的一部分。

## Completion Check

Phase 1 完成前确认：
- [ ] 已运行标签匹配逻辑。
- [ ] `papers.matched_tags` 已更新。
- [ ] `data/execution_log.md` 已记录 `Phase 1 - COMPLETED`。

## Completion

- 输出：数据库中的 matched tags。
- 日志：`Phase 1 - COMPLETED: {paper_id} — {n} tags: {labels}`
