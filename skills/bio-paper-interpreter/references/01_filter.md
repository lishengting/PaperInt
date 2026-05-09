# Phase 1: Filter

## Goal
确定论文是否属于生物信息学领域，并为通过筛选的论文分配主题标签。
不通过的论文记录跳过原因，不做解读。

## Input

- 论文元数据 JSON（位于 paper 目录下的 `{paper_id}.metadata.json`）
- `config.yaml`（关键词列表、标签定义）

找到 paper 目录：
```bash
PAPER_DIR="data/$(python3 -c "import json; s=json.load(open('data/downloaded.json')); print(s.get('paper_dirs',{}).get('{paper_id}',''))")"
```

## Workflow

### Step 1: Read Paper Metadata

```bash
cat $PAPER_DIR/{paper_id}.metadata.json
```

确认 JSON 包含 `title`、`abstract`、`doi` 等字段。

### Step 2: Run Relevance Filter

```bash
cat $PAPER_DIR/{paper_id}.metadata.json | \
  python3 scripts/filter_relevance.py --config config.yaml
```

脚本逻辑（详见 `scripts/filter_relevance.py`）：
- 先检查排除关键词：任何命中 → 立即拒绝
- 再统计包含关键词命中数：必须 ≥ `keywords.include_min_match`（默认 2）
- 无标题且无摘要 → 拒绝

脚本在 stdout 输出带 `relevance` 字段的 JSON，stderr 输出 `Relevance filter: {passed}/{total} passed`。

### Step 3: Decision

检查输出中的 `relevance.passed`：

- **`false`** → 保存跳过记录，STOP：

```bash
cat > $PAPER_DIR/{paper_id}.skipped.json << 'EOF'
{
  "paper_id": "...",
  "title": "...",
  "skipped_at": "<ISO timestamp>",
  "reason": "<relevance.reason>",
  "include_matches": [...],
  "exclude_matches": [...]
}
EOF
```

日志：`Phase 1 - REJECTED: {paper_id} — {reason}`

- **`true`** → 继续 Step 4。

### Step 4: Match Topic Tags

```bash
cat $PAPER_DIR/{paper_id}.metadata.json | \
  python3 scripts/match_tags.py --config config.yaml
```

脚本逻辑（详见 `scripts/match_tags.py`）：
- 用 `config.yaml` 中 `tags.definitions` 的正则模式匹配标题+摘要
- 基础标签（`tags.base_tag_ids`，默认 [2, 9]）始终包含
- 如果匹配到 ML/DL/LLM/AF 标签，自动添加 AI 父标签（`tags.ai_parent_tag_id`，默认 1）

### Step 5: Check Tag Coverage

如果只匹配到基础标签（2 个 base tags，无额外标签），说明论文主题特异性不足：

```
Phase 1 - INFO: {paper_id} only base tags — consider skipping
```

此时可选择跳过或继续。由 agent 或用户判断。

### Step 6: Report

输出带 `relevance` 和 `matched_tags` 的完整 JSON 给下一阶段使用。

日志：`Phase 1 - COMPLETED: {paper_id} — {n} tags: {labels}`

## Output

通过筛选后，paper JSON 包含：
- `relevance` — 筛选结果（passed, reason, include_matches, exclude_matches）
- `matched_tags` — 标签结果（tag_ids, matched_labels）

不通过筛选：
- `{paper_dir}/{paper_id}.skipped.json`

## Rules

1. 先筛选后解读 — 不跳过相关性检查
2. 记录跳过原因 — `reason` 字段必须精确（`no_content` / `excluded` / `insufficient_matches_N_lt_M`）
3. 关键词来自 config.yaml — 不硬编码
4. 标签来自 config.yaml — 不自行定义
5. 基础标签不视为"匹配不足"的唯一理由 — 由 agent 综合判断
6. 不修改 config.yaml
7. 跳过论文不视为错误 — `REJECTED` 是正常结束状态

## Completion Check

Phase 1 完成前确认：
- [ ] 已运行 `filter_relevance.py`
- [ ] 已运行 `match_tags.py`（如果通过筛选）
- [ ] 跳过记录已保存（如果未通过）
- [ ] 日志已写入 `execution_log.md`
- [ ] 结果 JSON 包含 `relevance` 和 `matched_tags` 字段

## Completion
- 通过：输出带标签的 paper JSON，记录 `Phase 1 - COMPLETED`
- 不通过：保存 `_skipped.json`，记录 `Phase 1 - REJECTED`
- Git commit（如未被 gitignore）