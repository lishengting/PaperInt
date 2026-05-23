# Phase 2: Interpret

## Goal
从论文 PDF 和元数据中提取信息，生成结构化中文解读报告和文章体简报，输出到
`{paper_dir}/{paper_id}.interpret.md`、`{paper_dir}/{paper_id}.brief.md` 和 `.interpret.json`。

## Input
- Phase 1 写入数据库的 `matched_tags`
- PDF 文件 `{paper_dir}/{paper_id}.pdf`
- `config.yaml`（LLM 端点/模型、PDF 提取配置、`pdf_text_max_chars`）
- `references/prompts/interpret.yaml` 和 `references/prompts/brief.yaml`（prompt 模板）

找到 paper 目录：
```bash
PAPER_DIR="data/$(python3 -c "
import sys; sys.path.insert(0, 'scripts')
from paper_db import get_conn, get_db_path, get_paper_dir
import yaml
config = yaml.safe_load(open('config.yaml'))
conn = get_conn(config)
print(get_paper_dir(conn, '{paper_id}') or '')
")"
```

## Workflow

### Step 1: Extract PDF Content

```bash
# Primary method (pymupdf4llm → Markdown + images):
python3 skills/bio-paper-interpreter/scripts/extract_pdf.py $PAPER_DIR/{paper_id}.pdf \
  --max-chars 100000 \
  --image-path $PAPER_DIR/images/ \
  --json > /tmp/{paper_id}_extract.json

# Output fields: markdown, images_dir, representative_image, extractor, image_count
```

检查提取结果：
- **≥ 1000 字符** → full_text 模式（深度解读）
- **< 1000 字符或无 PDF** → 标记为 `interpret_failed`；当前 CLI 不回退到 abstract-only 模式

#### Image Extraction

使用 pymupdf4llm 时，PDF 中的嵌入图片会自动提取到 `{paper_dir}/images/`
目录（PNG 格式）。系统自动选择最有代表性的图表（优先早期页面的较大图片）。

提取器通过 `config.yaml` 中的 `download.pdf_extraction.extractor` 配置：
- `"auto"`（默认）— 优先 pymupdf4llm，失败时回退到 pdftotext
- `"pymupdf4llm"` — 仅使用 pymupdf4llm
- `"pdftotext"` — 仅使用 pdftotext（无图片提取）

回退到 pdftotext 或无图片的 PDF 时，`representative_image` 为 null。

### Step 2: Determine Interpretation Path

- **Path A (Claude Code Direct)**: Claude 直接读取论文内容和 `references/prompts/` 中的 prompt 模板，
  生成解读。不需要外部 LLM API。
- **Path B (External LLM Pipeline)**: `paper_cli.py` 在进程内构建 prompt 并调用
  `config.yaml` 中配置的 LLM API。需要 `LLM_API_KEY` 环境变量。下面的
  `build_prompt.py` + curl 示例仅用于手动调试。

### Step 3: Read System Prompt

从 `references/prompts/` 目录读取对应的 prompt 模板：
- 结构化解读: `references/prompts/interpret.yaml` — 系统提示词 + 用户提示词模板
- 文章体简报: `references/prompts/brief.yaml` — 系统提示词 + 用户提示词模板

**Prompt 模板不再从 `config.yaml` 读取**。所有 prompt 模板统一放在技能目录内，`build_prompt.py` 自动从 YAML 加载。

### Step 4: Collect Paper Resources

**必须检查**（见下方资源发现清单）：

1. **论文元数据** — title, authors, doi, date, category, abstract
2. **论文正文** — 从 PDF 提取的全文内容
3. **论文页面** — 对 medRxiv/bioRxiv，检查 article landing page：
   - `https://www.{server}.org/content/{doi}`
   - `https://www.{server}.org/content/{doi}.supplementary-material`
4. **补充材料** — 检查 PDF 正文中提到的 Supplementary Note/Table/Figure/Data
5. **代码和数据可用性声明** — 检查 PDF 末尾的 Data Availability / Code Availability
6. **外部资源链接** — 论文中明确给出的 GitHub、Zenodo、Figshare、GEO、SRA 等

### Step 5: Interpret (Path A — Claude Code Direct)

Claude 直接解读论文，**严格按照下方 Output 模板**生成结构化报告。

解读要求：
- **Paper Understanding**: 用流畅的中文叙述，深入分析研究问题、设计、方法和发现
- **Paper Claims**: 从论文中提取所有明确陈述，填入对应表格
- **资源清单**: 检查并登记所有补充材料、代码仓库、数据访问号
- **Interpretation Insights**: 附加的批判性分析——创新点、局限性、实践意义
- **精炼总结**: 3-5 条 bullet points

### Step 6: Interpret (Path B — External LLM, manual debugging)

`paper_cli.py` 默认在进程内构建 prompt 并调用 LLM；下面的命令用于手动调试同一套
prompt 模板。

```bash
# Build prompts
cat $PAPER_DIR/{paper_id}.metadata.json | \
  python3 skills/bio-paper-interpreter/scripts/build_prompt.py \
  --config config.yaml \
  --mode full_text \
  --pdf-text-file /tmp/{paper_id}_text.txt > /tmp/{paper_id}_prompt.json

# Call LLM
LLM_BASE_URL=$(python3 -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c['llm']['api_base_url'])")
LLM_MODEL=$(python3 -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c['llm']['model'])")

curl -s "$LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json, yaml
c = yaml.safe_load(open('config.yaml'))
p = json.load(open('/tmp/{paper_id}_prompt.json'))
print(json.dumps({
    'model': c['llm']['model'],
    'temperature': c['llm']['temperature'],
    'max_tokens': c['llm']['max_tokens'],
    'messages': [
        {'role': 'system', 'content': p['system_prompt']},
        {'role': 'user', 'content': p['user_prompt']}
    ]
}))
")" > /tmp/{paper_id}_llm_response.json
```

### Step 7: Save Output

**主输出** — 结构化 Markdown 报告（按照下方模板）:

```bash
cat > $PAPER_DIR/{paper_id}.interpret.md << 'EOF'
...（下方 Output 模板内容）
EOF
```

**副输出** — JSON 元数据:

```bash
cat > $PAPER_DIR/{paper_id}.interpret.json << 'EOF'
{
  "paper_id": "...",
  "doi": "...",
  "title": "...",
  "content": "<full markdown from .md file>",
  "tags": [<matched tag ids>],
  "tag_labels": [<matched tag labels>],
  "mode": "full_text",
  "interpreted_at": "<ISO timestamp>"
}
EOF
```

## Output

Phase 2 生成两个输出文件：

1. **`{paper_dir}/{paper_id}.interpret.md`** — 结构化技术报告（Paper Understanding、Paper Claims 表格、资源清单等）
2. **`{paper_dir}/{paper_id}.brief.md`** — 文章体中文简报（研究背景、核心方法、主要发现、讨论与意义、实践启示）

Phase 3 将两者转换为 HTML：
- `{paper_id}.interpret.html`
- `{paper_id}.brief.html`

### {paper_dir}/{paper_id}.interpret.md

```markdown
# Paper: [论文英文标题]
> **作者**: [authors]
> **来源**: [source] | [date]
> **DOI**: [doi]
> **标签**: #tag1 #tag2
> **解读模式**: full_text

---

## Paper Understanding

### Research Question
[1-3 段说明论文要回答的科学问题、研究背景和生物学/计算目标。
用中文叙述，使未读原文的读者能清楚理解该研究的动机和目的。]

### Study Design
[说明样本/队列/实验设计/比较组/数据类型。只写论文明确给出的内容。
包括样本量、纳入/排除标准、数据来源。]

### Method Overview
[用清晰的中文解释主要分析方法、算法逻辑和技术路线。
让读者理解每一步为什么存在，以及各步骤之间的依赖关系。
涉及的工具和软件在后文 Paper Claims 中列表，此处只写方法逻辑。]

### Key Findings
[列出论文声称的主要发现、关键图表和关键数值。
包括 p 值、效应量、性能指标等定量结果。]

### Interpretation Insights
[批判性分析：创新点、局限性、对领域的影响、与实践的关联。
区别于 Paper Understanding —— 此处加入解读者的判断和分析。
明确区分论文陈述 vs 合理推断。]

## Paper Claims

### Analysis Steps
| Step | Input | Tool/Method | Output | Location in Paper |

### Code and Data Availability
| Resource | URL/Identifier | Purpose | Access Notes |

### Software Requirements
| Software | Version | Purpose | Source in Paper |

### Data Requirements
| Database | Accession | Samples | Data Type | Location in Paper |

### Parameters
| Tool | Parameter | Value | Source |

### Expected Results
| Output | Figure/Table | Expected Value | Notes |

## Source Files Reviewed
| File/URL | Type | Local Path | Status | Notes |

## Supplementary Materials Inventory
| Item | Type | URL/Path | Local Path | Mentioned In | Status |

## Resource Locations
| Resource | Type | URL/Identifier | Local Path | Access Notes |

## Tags
- [tag_id] label
- ...

## 精炼总结
- [每条一句话，核心要点，不超过 30 字]
- ...
```

## Rules

1. **结构化优先** — 严格按 Output 模板输出，每个 section 不能省略
2. **论文声明与解读分析分离** — Paper Understanding 写论文内容；Interpretation Insights 写分析判断
3. **明确标注来源** — 尽量写明“根据论文第X节/图X/表X”，无法定位时明确说明来源范围
4. **不遗漏资源** — Claude Code direct/manual 解读应检查论文页面、补充材料 tab、代码仓库和数据可用性声明；批处理 CLI 主要依据 PDF 提取内容和 PDF validation
5. **表格完整** — 所有表格必须填写；不确定的标注 "Not specified" 或 "TBD"
6. **PDF 截断** — 按 `config.yaml` 中 `download.pdf_text_max_chars`（默认 100000）截断
7. **模式区分** — 当前批处理 CLI 只在 PDF 文本充足时进入 `full_text`；文本不足会标记 `interpret_failed`
8. **Prompt 模板来自 `references/prompts/`** — 不硬编码 system prompt
9. **不修改 config.yaml**
10. **精炼总结** — 3-5 条 bullet points，每条 ≤30 字
11. **引用位置** — 所有声明注明章节/图表/URL
12. **不编造** — 论文未提及的不写；推断必须明确标注

## Completion Check

Phase 2 完成前确认：
- [ ] PDF 内容已提取（如有 PDF）
- [ ] 图片已提取到 `{paper_dir}/images/`（如适用）
- [ ] 代表性图表路径已记录到 `.interpret.json`
- [ ] 模式已确定为 full_text，或文本不足时已标记 `interpret_failed`
- [ ] Claude Code direct/manual 解读已检查论文页面和补充材料 tab（对预印本）
- [ ] 所有 Output 模板中的 section 已填写
- [ ] `.interpret.md` 文件已保存到 `{paper_dir}/{paper_id}.interpret.md`
- [ ] `.interpret.json` 文件已保存到 `{paper_dir}/{paper_id}.interpret.json`
- [ ] 日志已写入 `execution_log.md`

## Completion
- 输出 `{paper_dir}/{paper_id}.interpret.md` + `{paper_dir}/{paper_id}.brief.md` + `.interpret.json`
- 更新数据库状态：`python3 -c "import sys; sys.path.insert(0, 'scripts'); from paper_db import get_conn, get_db_path, mark_interpreted; import yaml; c=yaml.safe_load(open('config.yaml')); mark_interpreted(get_conn(c), '{paper_id}')"`
- 日志：`Phase 2 - COMPLETED: {paper_id} — {mode}, {n} tags`
- Git commit（如未被 gitignore）