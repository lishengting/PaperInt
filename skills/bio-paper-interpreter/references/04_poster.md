# Phase 4: Poster Generation (Zero-Shot)

Generate publication-ready poster files summarizing the paper's core methodology.
Default output is English-only: 3 files per paper. Use `--cn` in `paper_cli.py` or
`--lang both` in `generate_poster.py` for bilingual output. Use `--trans` to derive
Chinese SVG/direct poster content from English outputs plus PDF context.

No AutoFigure dependency — uses the configured text LLM for SVG generation and
`qwen-image-2.0-pro` for direct image generation.

Requires `LLM_API_KEY` in the environment (or the configured API-key source).
Without an API key, Phase 4 is skipped and the pipeline continues.

## Output

### Default English output (3 files)

| # | File | Generator | Description |
|---|------|-----------|-------------|
| 1 | `{pid}.poster.en.svg` | text LLM | One-shot SVG, English |
| 2 | `{pid}.poster.en.png` | headless Chrome render | Rendered from #1 |
| 3 | `{pid}.poster.direct.en.png` | qwen-image-2.0-pro | Text→description→image, English |

### With `--lang both` / `paper_cli.py --cn` (additional Chinese files)

| # | File | Generator | Description |
|---|------|-----------|-------------|
| 4 | `{pid}.poster.zh.svg` | text LLM | One-shot SVG, Chinese |
| 5 | `{pid}.poster.zh.png` | headless Chrome render | Rendered from #4 |
| 6 | `{pid}.poster.direct.zh.png` | qwen-image-2.0-pro | Text→description→image, Chinese |

With `--trans`, the Chinese SVG is translated from the English SVG and the Chinese direct PNG uses a translated English visual description plus paper context.

## Workflow

### Step 1: Verify API Key

```bash
printf '%s' "$LLM_API_KEY" | head -c 10
```

### Step 2: Generate Posters

```bash
python3 skills/bio-paper-interpreter/scripts/generate_poster.py \
  --paper-dir $PAPER_DIR \
  --paper-id {paper_id} \
  --api-key $LLM_API_KEY
```

Options:
- `--lang`: `en`, `zh`, or `both` (default: `en`)
- `--trans`: translate Chinese SVG/direct poster content from English outputs instead of independently generating from the paper
- `--methodology-model`: text LLM for SVG generation
- `--methodology-base-url`: API base URL
- `--enhancement-model`: image model for direct PNG (default: `qwen-image-2.0-pro`)

### Step 3: Verify Output

```bash
ls -la $PAPER_DIR/{paper_id}.poster.*
```

## Completion Check

Phase 4 默认英文输出完成前确认：
- [ ] `LLM_API_KEY` 已配置，或 Phase 4 已明确记录为 `SKIPPED`
- [ ] `{pid}.poster.en.svg` 已保存且非空
- [ ] `{pid}.poster.en.png` 已渲染
- [ ] `{pid}.poster.direct.en.png` 已生成，或 direct image API 失败已在日志中记录
- [ ] SVG 在浏览器中可正常渲染
- [ ] 日志已写入 `data/execution_log.md`

使用 `--lang both` / `paper_cli.py --cn` 时额外确认：
- [ ] `{pid}.poster.zh.svg` 已保存且非空
- [ ] `{pid}.poster.zh.png` 已渲染
- [ ] `{pid}.poster.direct.zh.png` 已生成，或 direct image API 失败已在日志中记录

使用 `--trans` 时额外确认：
- [ ] 中文 SVG 保留英文 SVG 的 viewBox、几何结构、颜色和布局
- [ ] 中文 SVG 的可见文本已翻译为中文，且字号/换行适配显示空间
