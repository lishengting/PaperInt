# Phase 4: Poster Generation (AutoFigure)

Generate a publication-ready poster SVG summarizing the paper's core methodology
using [AutoFigure](https://github.com/vlln/AutoFigure) (ICLR 2026).

AutoFigure uses LLM-powered iterative refinement (generate → evaluate → refine)
to produce SVG diagrams from paper PDFs.

This phase requires `AUTOFIGURE_API_KEY` set in the environment (or via `config.yaml`).
Without an API key, Phase 4 is silently skipped.

## Workflow

### Step 1: Verify Prerequisites

```bash
# Check AutoFigure is installed
python3 -c "from autofigure import AutoFigureAgent, Config; print('OK')"

# Check API key
echo $AUTOFIGURE_API_KEY | head -c 10
```

### Step 2: Generate Poster

```bash
python3 scripts/generate_poster.py $PAPER_DIR/{paper_id}.pdf \
  --output-dir $PAPER_DIR \
  --paper-id {paper_id} \
  --provider openrouter \
  --model google/gemini-3.1-pro-preview \
  --max-iterations 5
```

Options:
- `--provider`: `openrouter` (default), `gemini`, or `bianxie`
- `--model`: defaults to `google/gemini-3.1-pro-preview`
- `--max-iterations`: refinement rounds (default 5, range 1-10)
- `--enable-enhancement`: apply AI-powered aesthetic post-processing
- `--api-key`: override API key (or set `AUTOFIGURE_API_KEY` env var)

### Step 3: Verify Output

```bash
ls -la $PAPER_DIR/{paper_id}.poster.svg
```

Ensure the SVG file is non-empty and renders correctly in a browser.

## Output

- `{paper_dir}/{paper_id}.poster.svg` — standalone SVG diagram (vector format, scalable to any size)

## Completion Check

Phase 4 完成前确认：
- [ ] `AUTOFIGURE_API_KEY` 已配置
- [ ] AutoFigure 已安装并可导入
- [ ] `{paper_id}.poster.svg` 已保存且非空
- [ ] SVG 在浏览器中可正常渲染
- [ ] 日志已写入 `execution_log.md`