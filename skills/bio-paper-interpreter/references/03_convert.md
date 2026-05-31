# Phase 3: Convert

## Goal

将 Phase 2 生成的 Markdown 解读报告转换为带样式的独立 HTML 文件，
可直接在浏览器中打开阅读或发布到网页平台。

当前 CLI 会转换已存在的 Markdown 输入：
- `{paper_dir}/{paper_id}.interpret.md` → `{paper_dir}/{paper_id}.interpret.html`
- `{paper_dir}/{paper_id}.brief.md` → `{paper_dir}/{paper_id}.brief.html`
- `{paper_dir}/{paper_id}.interpret.zh.md` → `{paper_dir}/{paper_id}.interpret.zh.html`
- `{paper_dir}/{paper_id}.brief.zh.md` → `{paper_dir}/{paper_id}.brief.zh.html`

如果 Phase 4 已生成同语言 poster，`paper_cli.py` 会在 Phase 4 后重新运行
Phase 3，把英文 poster 嵌入英文 HTML，把中文 poster 嵌入中文 HTML。

## Input

- Phase 2 输出的 `{paper_dir}/{paper_id}.interpret.md`
- Phase 2 输出的 `{paper_dir}/{paper_id}.brief.md`（如果生成成功）
- Phase 2 可选输出的 `{paper_dir}/{paper_id}.interpret.zh.md` / `.brief.zh.md`
- `{paper_dir}/{paper_id}.interpret.json` 中记录的 representative image（如果存在）
- `{paper_dir}/{paper_id}.poster.en.png` 或 `.poster.zh.png`（如果 Phase 4 已生成）

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

### Step 1: Verify Inputs

确认至少一个 Phase 2 Markdown 文件存在：

```bash
ls $PAPER_DIR/{paper_id}.interpret.md $PAPER_DIR/{paper_id}.brief.md
```

### Step 2: Convert Interpretation HTML

```bash
python3 skills/bio-paper-interpreter/scripts/md_to_html.py \
  --input $PAPER_DIR/{paper_id}.interpret.md \
  --output $PAPER_DIR/{paper_id}.interpret.html \
  --lang en
```

如果有代表性图表或 poster，可追加：

```bash
python3 skills/bio-paper-interpreter/scripts/md_to_html.py \
  --input $PAPER_DIR/{paper_id}.interpret.md \
  --output $PAPER_DIR/{paper_id}.interpret.html \
  --lang en \
  --image $PAPER_DIR/images/fig_xxx.png \
  --poster $PAPER_DIR/{paper_id}.poster.en.png
```

### Step 3: Convert Brief HTML

```bash
python3 skills/bio-paper-interpreter/scripts/md_to_html.py \
  --input $PAPER_DIR/{paper_id}.brief.md \
  --output $PAPER_DIR/{paper_id}.brief.html \
  --lang en
```

`paper_cli.py` 自动对英文 `interpret`/`brief` 和可选中文 `.zh` Markdown 输入循环转换，缺失的输入会跳过。

## Output

- `{paper_dir}/{paper_id}.interpret.html` — 英文结构化解读 HTML
- `{paper_dir}/{paper_id}.brief.html` — 英文文章体简报 HTML
- `{paper_dir}/{paper_id}.interpret.zh.html` — 中文结构化解读 HTML（如存在中文 Markdown）
- `{paper_dir}/{paper_id}.brief.zh.html` — 中文文章体简报 HTML（如存在中文 Markdown）

## CSS 特性

- 亮色/暗色主题自动适配（`prefers-color-scheme`）
- 响应式布局，适合桌面和移动端阅读
- 表格斑马纹、代码块高亮、引用块样式
- 系统字体栈（SF / Segoe UI / Helvetica）
- 可内嵌代表性图表和同语言 poster，无外部资源依赖

## Rules

1. Phase 3 仅在 Phase 2 COMPLETED 后执行。
2. HTML 覆盖写入，操作幂等。
3. 使用 `skills/bio-paper-interpreter/scripts/md_to_html.py`，不手动拼接 HTML。
4. 不修改 `.md` 源文件。
5. Phase 4 生成 poster 后，`paper_cli.py` 会重新运行 Phase 3 以嵌入 poster。

## Completion Check

- [ ] `.interpret.html` 已生成（如果 `.interpret.md` 存在）
- [ ] `.brief.html` 已生成（如果 `.brief.md` 存在）
- [ ] 文件大小合理（通常显著大于原 `.md` 文件）
- [ ] 日志已写入 `data/execution_log.md`

## Completion

- 输出：`{paper_dir}/{paper_id}.interpret.html` 和/或 `{paper_dir}/{paper_id}.brief.html`
- 日志：`Phase 3 - COMPLETED: {paper_id} — HTML saved: {paper_dir}`
