# Phase 3: Convert

## Goal
将 Phase 2 生成的 Markdown 解读报告转换为带样式的独立 HTML 文件，
可直接在浏览器中打开阅读或发布到网页平台。

## Input
- Phase 2 输出的 `{paper_dir}/{paper_id}.interpret.md`

找到 paper 目录：
```bash
PAPER_DIR=$(find data -name "{paper_id}.metadata.json" -exec dirname {} \;)
```

## Workflow

### Step 1: Verify Input

确认 Phase 2 的 `.interpret.md` 文件存在：
```bash
ls $PAPER_DIR/{paper_id}.interpret.md
```

### Step 2: Convert to HTML

```bash
python3 scripts/md_to_html.py \
  --input $PAPER_DIR/{paper_id}.interpret.md \
  --output $PAPER_DIR/{paper_id}.interpret.html
```

脚本使用 Python `markdown` 库（版本 ≥ 3.0，系统已安装），启用以下扩展：
- `tables` — Markdown 表格 → HTML table
- `fenced_code` — 围栏代码块（``` ```）
- `codehilite` — 代码语法高亮
- `toc` — 自动生成目录
- `smarty` — 智能引号/破折号

输出为包含内嵌 CSS 的独立 HTML 文件，支持亮色/暗色主题自动切换。

### Step 3: Verify Output

```bash
# 检查文件大小（应显著大于原 .md 文件）
wc -c $PAPER_DIR/{paper_id}.interpret.html
```

## Output

- `data/interpreted/{paper_id}.html` — 带样式的独立 HTML 文件

## CSS 特性

- 亮色/暗色主题自动适配（`prefers-color-scheme`）
- 响应式布局，适合桌面和移动端阅读
- 表格斑马纹、代码块高亮、引用块样式
- 系统字体栈（SF / Segoe UI / Helvetica）

## Rules

1. Phase 3 仅在 Phase 2 COMPLETED 后执行
2. HTML 覆盖写入（幂等操作）
3. 使用 `scripts/md_to_html.py`，不手动拼接 HTML
4. 不修改 `.md` 源文件

## Completion Check

- [ ] `.interpret.html` 文件已生成
- [ ] 文件大小合理（> 10KB）
- [ ] 日志已写入 `execution_log.md`

## Completion
- 输出 `{paper_dir}/{paper_id}.interpret.html`
- 日志：`Phase 3 - COMPLETED: {paper_id} — HTML generated`