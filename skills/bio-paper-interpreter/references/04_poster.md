# Phase 4: Poster Generation (Zero-Shot)

Generate 6 publication-ready poster files summarizing the paper's core methodology.
No AutoFigure dependency — uses deepseek-v4-pro for SVG generation and
qwen-image-2.0-pro for direct image generation.

Requires `LLM_API_KEY` in the environment (or via `config.yaml`).
Without an API key, Phase 4 is silently skipped.

## Output (6 files per paper)

| # | File | Generator | Description |
|---|------|-----------|-------------|
| 1 | `{pid}.poster.en.svg` | deepseek-v4-pro | One-shot SVG, English |
| 2 | `{pid}.poster.zh.svg` | deepseek-v4-pro | One-shot SVG, Chinese |
| 3 | `{pid}.poster.en.png` | cairosvg render | Rendered from #1 |
| 4 | `{pid}.poster.zh.png` | cairosvg render | Rendered from #2 |
| 5 | `{pid}.poster.direct.en.png` | qwen-image-2.0-pro | Text→description→image, English |
| 6 | `{pid}.poster.direct.zh.png` | qwen-image-2.0-pro | Text→description→image, Chinese |

## Workflow

### Step 1: Verify API Key

```bash
echo $LLM_API_KEY | head -c 10
```

### Step 2: Generate Posters

```bash
python3 skills/bio-paper-interpreter/scripts/generate_poster.py \
  --paper-dir $PAPER_DIR \
  --paper-id {paper_id} \
  --api-key $LLM_API_KEY
```

Options:
- `--lang`: `en`, `zh`, or `both` (default)
- `--methodology-model`: text LLM for SVG generation (default: deepseek-v4-pro)
- `--methodology-base-url`: API base URL
- `--enhancement-model`: image model for direct PNG (default: qwen-image-2.0-pro)

### Step 3: Verify Output

```bash
ls -la $PAPER_DIR/{paper_id}.poster.*
```

## Completion Check

Phase 4 完成前确认：
- [ ] `LLM_API_KEY` 已配置
- [ ] `{pid}.poster.en.svg` + `.zh.svg` 均已保存且非空
- [ ] `{pid}.poster.en.png` + `.zh.png` 均已渲染
- [ ] `{pid}.poster.direct.en.png` + `.zh.png` 均已生成
- [ ] SVG 在浏览器中可正常渲染
- [ ] 日志已写入 `execution_log.md`