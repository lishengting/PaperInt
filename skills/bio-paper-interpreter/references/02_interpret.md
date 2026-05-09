# Phase 2: Interpret

## Goal
从论文 PDF 或元数据中提取信息，生成结构化中文解读报告，输出到
`{paper_dir}/{paper_id}.interpret.md`（主输出）和 `.interpret.json`（结构化元数据）。

## Input
- Phase 1 输出的 paper JSON（含 `relevance`、`matched_tags`）
- PDF 文件 `{paper_dir}/{paper_id}.pdf`（如果存在）
- `config.yaml`（system prompts、LLM 配置、pdf_text_max_chars）

找到 paper 目录：
```bash
PAPER_DIR=$(find data -name "{paper_id}.metadata.json" -exec dirname {} \;)
```

## Workflow

### Step 1: Extract PDF Text

```bash
bash scripts/extract_pdf_text.sh $PAPER_DIR/{paper_id}.pdf \
  --max-chars 100000 > /tmp/{paper_id}_text.txt
```

检查提取结果：
- **> 1000 字符** → full_text 模式（深度解读）
- **≤ 1000 字符或无 PDF** → abstract_only 模式（基于标题+摘要）

### Step 2: Determine Interpretation Path

- **Path A (Claude Code Direct)**: Claude 直接读取论文内容和 system prompt，
  生成解读。不需要外部 LLM API。
- **Path B (External LLM Pipeline)**: 运行 `build_prompt.py` 构建提示词，
  调用 `config.yaml` 中配置的 LLM API 生成解读。需要 `LLM_API_KEY` 环境变量。

### Step 3: Read System Prompt

从 `config.yaml` 读取对应的 system prompt：
- full_text: `system_prompts.full_text`
- abstract_only: `system_prompts.abstract_only`

### Step 4: Collect Paper Resources

**必须检查**（参照 `01_reader.md` 的资源发现流程）：

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

### Step 6: Interpret (Path B — External LLM)

```bash
# Build prompts
cat $PAPER_DIR/{paper_id}.metadata.json | \
  python3 scripts/build_prompt.py \
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
  "mode": "full_text | abstract_only",
  "interpreted_at": "<ISO timestamp>"
}
EOF
```

## Output: {paper_dir}/{paper_id}.interpret.md

```markdown
# Paper: [论文英文标题]
> **作者**: [authors]
> **来源**: [source] | [date]
> **DOI**: [doi]
> **标签**: #tag1 #tag2
> **解读模式**: full_text | abstract_only

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
3. **明确标注来源** — 全文模式写"根据论文第X节"，摘要模式写"根据摘要推断"
4. **不遗漏资源** — 必须检查论文页面、补充材料 tab、代码仓库和数据可用性声明
5. **表格完整** — 所有表格必须填写；不确定的标注 "Not specified" 或 "TBD"
6. **PDF 截断** — 按 `config.yaml` 中 `download.pdf_text_max_chars`（默认 100000）截断
7. **模式区分** — JSON 中 `mode` 字段必须准确标记 `full_text` 或 `abstract_only`
8. **Prompt 来自 config** — 不硬编码 system prompt
9. **不修改 config.yaml**
10. **精炼总结** — 3-5 条 bullet points，每条 ≤30 字
11. **引用位置** — 所有声明注明章节/图表/URL
12. **不编造** — 论文未提及的不写；推断必须明确标注

## Completion Check

Phase 2 完成前确认：
- [ ] PDF 文本已提取（如有 PDF）
- [ ] 模式已确定（full_text / abstract_only）
- [ ] 论文页面和补充材料 tab 已检查（对预印本）
- [ ] 所有 Output 模板中的 section 已填写
- [ ] `.interpret.md` 文件已保存到 `{paper_dir}/{paper_id}.interpret.md`
- [ ] `.interpret.json` 文件已保存到 `{paper_dir}/{paper_id}.interpret.json`
- [ ] 日志已写入 `execution_log.md`

## Completion
- 输出 `{paper_dir}/{paper_id}.interpret.md` + `.interpret.json`
- 日志：`Phase 2 - COMPLETED: {paper_id} — {mode}, {n} tags`
- Git commit（如未被 gitignore）