#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
DATA_ROOT = PROJECT_ROOT / 'data'

sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
sys.path.insert(0, str(SCRIPT_DIR))

from paper_cli import (NO_ISSN_JOURNAL_ID, get_journal_options_for_viewer,
                       load_config, query_papers_for_viewer)
from paper_db import get_conn, get_stats


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PaperInt 文献浏览器</title>
  <style>
    :root {
      --canvas: #f4f6fb;
      --surface: #ffffff;
      --primary: #3b45e5;
      --primary-hover: #626aea;
      --primary-dark: #2f37b7;
      --text: #020c1a;
      --muted: #70749e;
      --soft: #a7abd3;
      --border: #e9ebf7;
      --chip: #eef1ff;
      --success: #0f9f6e;
      --warn: #b7791f;
      --danger: #d64545;
      --shadow: 0 18px 45px rgba(27, 39, 94, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background: var(--canvas);
      font: 14px/1.57 PingFang SC, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    button, input, select { font: inherit; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { color: var(--primary-hover); }
    .app {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      padding: 28px 22px;
      border-right: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.72);
      backdrop-filter: blur(8px);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 28px;
    }
    .brand-mark {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--primary), #7d83ff);
      box-shadow: 0 12px 24px rgba(59, 69, 229, 0.24);
    }
    .brand-title { font-weight: 800; font-size: 20px; letter-spacing: -0.02em; }
    .brand-subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .side-card {
      border: 1px solid var(--border);
      border-radius: 18px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 16px;
      margin-bottom: 16px;
    }
    .side-card h3 {
      margin: 0 0 12px;
      font-size: 14px;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .stat {
      padding: 10px;
      border-radius: 12px;
      background: #f7f8ff;
    }
    .stat-value { font-weight: 800; font-size: 18px; color: var(--primary-dark); }
    .stat-label { color: var(--muted); font-size: 12px; }
    .main {
      min-width: 0;
      padding: 22px 34px 44px;
    }
    .topbar {
      display: flex;
      justify-content: flex-end;
      gap: 16px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 26px;
    }
    .hero {
      max-width: 980px;
      margin: 0 auto 22px;
      text-align: center;
    }
    .hero h1 {
      margin: 8px 0 10px;
      font-size: clamp(30px, 4vw, 44px);
      line-height: 1.12;
      letter-spacing: -0.04em;
    }
    .hero p {
      margin: 0 auto 24px;
      color: var(--muted);
      max-width: 720px;
      font-size: 16px;
    }
    .control-panel {
      max-width: 1120px;
      margin: 0 auto 22px;
      border: 1px solid var(--border);
      border-radius: 24px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .controls {
      display: grid;
      grid-template-columns: minmax(220px, 2fr) 170px 130px auto;
      gap: 12px;
      align-items: end;
    }
    .field { text-align: left; }
    .field label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin: 0 0 7px 2px;
    }
    .input, select {
      width: 100%;
      height: 42px;
      border: 1px solid var(--border);
      border-radius: 14px;
      color: var(--text);
      background: #fff;
      padding: 0 14px;
      outline: none;
    }
    .input:focus, select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(59, 69, 229, 0.10);
    }
    .primary-btn, .ghost-btn {
      height: 42px;
      border: 0;
      border-radius: 14px;
      cursor: pointer;
      padding: 0 18px;
      font-weight: 700;
    }
    .primary-btn {
      color: #fff;
      background: var(--primary);
      box-shadow: 0 12px 22px rgba(59, 69, 229, 0.22);
    }
    .primary-btn:hover { background: var(--primary-hover); }
    .ghost-btn {
      color: var(--primary-dark);
      background: var(--chip);
    }
    .journal-panel {
      margin-top: 16px;
      border-top: 1px solid var(--border);
      padding-top: 16px;
      text-align: left;
    }
    .journal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .journal-actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .small-btn {
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #fff;
      color: var(--primary-dark);
      cursor: pointer;
      height: 30px;
      padding: 0 12px;
      font-size: 12px;
      font-weight: 700;
    }
    .journal-search { max-width: 360px; margin-bottom: 10px; }
    .journal-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
      gap: 8px;
      max-height: 250px;
      overflow: auto;
      padding: 2px 2px 8px;
    }
    .journal-item {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #fbfcff;
      padding: 10px;
    }
    .journal-item input { margin-top: 4px; }
    .journal-name { font-weight: 700; font-size: 13px; }
    .journal-meta { color: var(--muted); font-size: 12px; }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      max-width: 1120px;
      margin: 0 auto 18px;
    }
    .chip {
      border-radius: 999px;
      background: var(--chip);
      color: #353966;
      padding: 6px 11px;
      font-size: 12px;
      font-weight: 700;
    }
    .results {
      max-width: 1120px;
      margin: 0 auto;
    }
    .result-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      color: var(--muted);
    }
    .paper-card {
      border: 1px solid var(--border);
      border-radius: 22px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 20px;
      margin-bottom: 14px;
    }
    .paper-title {
      margin: 0 0 10px;
      font-size: 20px;
      line-height: 1.35;
      letter-spacing: -0.02em;
    }
    .paper-abstract {
      color: #353966;
      margin: 0 0 14px;
      display: -webkit-box;
      -webkit-line-clamp: 4;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .meta-row, .links-row, .tag-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }
    .meta {
      border: 1px solid var(--border);
      border-radius: 999px;
      color: var(--muted);
      background: #fff;
      padding: 5px 10px;
      font-size: 12px;
    }
    .status-interpreted { color: var(--success); border-color: rgba(15, 159, 110, 0.24); }
    .status-download_failed, .status-interpret_failed { color: var(--danger); border-color: rgba(214, 69, 69, 0.24); }
    .status-searched, .status-downloaded { color: var(--warn); border-color: rgba(183, 121, 31, 0.24); }
    .link-btn {
      display: inline-flex;
      align-items: center;
      height: 34px;
      border-radius: 999px;
      border: 1px solid var(--border);
      padding: 0 13px;
      background: #fff;
      font-weight: 800;
      font-size: 12px;
    }
    .link-btn.primary {
      color: #fff;
      border-color: var(--primary);
      background: var(--primary);
    }
    .empty, .error {
      border: 1px dashed var(--soft);
      border-radius: 20px;
      background: rgba(255,255,255,0.7);
      padding: 32px;
      text-align: center;
      color: var(--muted);
    }
    .error { color: var(--danger); border-color: rgba(214,69,69,0.35); }
    .pagination {
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 10px;
      margin: 22px 0 0;
    }
    .pagination button:disabled { opacity: 0.45; cursor: not-allowed; }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--border); }
      .controls { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 620px) {
      .main { padding: 18px; }
      .controls { grid-template-columns: 1fr; }
      .journal-head, .result-head { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark"></div>
        <div>
          <div class="brand-title">PaperInt</div>
          <div class="brand-subtitle">文献数据库浏览器</div>
        </div>
      </div>
      <div class="side-card">
        <h3>数据库状态</h3>
        <div class="stats-grid" id="statsGrid"></div>
      </div>
      <div class="side-card">
        <h3>默认筛选</h3>
        <p style="margin:0;color:var(--muted);font-size:13px;">默认选中 Nature、Science、Cell 及其子刊配置中匹配到的数据库期刊。没有 ISSN 的条目统一归入预印版。</p>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <span>本地 SQLite</span>
        <span>安全文件 API</span>
        <span>动态分页</span>
      </div>
      <section class="hero">
        <h1>探索已检索和解读的生物医学文献</h1>
        <p>按关键词、状态、期刊和 ISSN 组合筛选论文，并通过 HTTP API 打开本地生成的 brief HTML 与 PDF。</p>
      </section>
      <section class="control-panel">
        <div class="controls">
          <div class="field">
            <label for="keywordInput">关键词</label>
            <input id="keywordInput" class="input" value="microbiome" placeholder="microbiome">
          </div>
          <div class="field">
            <label for="statusSelect">状态</label>
            <select id="statusSelect">
              <option value="ALL">ALL</option>
              <option value="searched">searched</option>
              <option value="downloaded">downloaded</option>
              <option value="download_failed">download_failed</option>
              <option value="interpreted" selected>interpreted</option>
              <option value="interpret_failed">interpret_failed</option>
            </select>
          </div>
          <div class="field">
            <label for="limitSelect">每页数量</label>
            <select id="limitSelect">
              <option>5</option>
              <option selected>10</option>
              <option>20</option>
              <option>50</option>
              <option>100</option>
            </select>
          </div>
          <button class="primary-btn" id="searchBtn">搜索</button>
        </div>
        <div class="journal-panel">
          <div class="journal-head">
            <div>
              <strong>期刊 / ISSN</strong>
              <div style="color:var(--muted);font-size:12px;" id="journalSummary">正在载入期刊列表...</div>
            </div>
            <div class="journal-actions">
              <button class="small-btn" id="defaultJournalBtn">默认 CNS</button>
              <button class="small-btn" id="allJournalBtn">全选</button>
              <button class="small-btn" id="clearJournalBtn">清空</button>
            </div>
          </div>
          <input id="journalSearch" class="input journal-search" placeholder="搜索期刊名、ISSN 或别名">
          <div class="journal-list" id="journalList"></div>
        </div>
      </section>
      <div class="chips" id="activeChips"></div>
      <section class="results">
        <div class="result-head">
          <div id="resultSummary">准备加载结果</div>
          <button class="ghost-btn" id="refreshBtn">刷新</button>
        </div>
        <div id="papersList"></div>
        <div class="pagination">
          <button class="ghost-btn" id="prevBtn">上一页</button>
          <span id="pageInfo">1 / 1</span>
          <button class="ghost-btn" id="nextBtn">下一页</button>
        </div>
      </section>
    </main>
  </div>
  <script>
    const noIssnId = '__no_issn__';
    let journals = [];
    let selectedJournals = new Set();
    let currentPage = 1;
    let lastTotal = 0;

    const $ = (id) => document.getElementById(id);

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
    }

    function paramValue(params, key, fallback) {
      const value = params.get(key);
      return value === null ? fallback : value;
    }

    function selectedIds() {
      return Array.from(selectedJournals).sort();
    }

    function setDefaultsFromUrl() {
      const params = new URLSearchParams(window.location.search);
      $('keywordInput').value = paramValue(params, 'k', 'microbiome');
      $('statusSelect').value = paramValue(params, 'status', 'interpreted');
      $('limitSelect').value = paramValue(params, 'limit', '10');
      currentPage = Math.max(1, Number.parseInt(paramValue(params, 'page', '1'), 10) || 1);
      const journalParam = params.get('journals');
      if (journalParam !== null) {
        selectedJournals = new Set(journalParam.split(',').map((item) => item.trim()).filter(Boolean));
      }
      return journalParam !== null;
    }

    async function fetchJson(url) {
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    async function loadStats() {
      try {
        const stats = await fetchJson('/api/stats');
        const order = ['searched', 'downloaded', 'interpreted', 'download_failed', 'interpret_failed'];
        $('statsGrid').innerHTML = order.map((key) => `
          <div class="stat">
            <div class="stat-value">${escapeHtml(stats[key] || 0)}</div>
            <div class="stat-label">${escapeHtml(key)}</div>
          </div>
        `).join('');
      } catch (error) {
        $('statsGrid').innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      }
    }

    async function loadJournals(useUrlSelection) {
      const data = await fetchJson('/api/journals');
      journals = data.items || [];
      if (!useUrlSelection) {
        selectedJournals = new Set(journals.filter((item) => item.default_selected).map((item) => item.id));
      }
      renderJournals();
    }

    function renderJournals() {
      const needle = $('journalSearch').value.trim().toLowerCase();
      const visible = journals.filter((item) => {
        if (!needle) return true;
        const haystack = [item.label, item.journal, item.issn, ...(item.aliases || [])].join(' ').toLowerCase();
        return haystack.includes(needle);
      });
      $('journalSummary').textContent = `已选 ${selectedJournals.size} / ${journals.length} 个期刊选项`;
      $('journalList').innerHTML = visible.map((item) => {
        const checked = selectedJournals.has(item.id) ? 'checked' : '';
        const meta = item.is_preprint ? `${item.count} 篇无 ISSN 文献` : `${item.issn || ''} · ${item.count} 篇`;
        return `
          <label class="journal-item">
            <input type="checkbox" data-id="${escapeHtml(item.id)}" ${checked}>
            <span>
              <span class="journal-name">${escapeHtml(item.journal)}</span><br>
              <span class="journal-meta">${escapeHtml(meta)}</span>
            </span>
          </label>
        `;
      }).join('') || '<div class="empty">没有匹配的期刊选项。</div>';
      document.querySelectorAll('#journalList input[type="checkbox"]').forEach((box) => {
        box.addEventListener('change', () => {
          if (box.checked) selectedJournals.add(box.dataset.id);
          else selectedJournals.delete(box.dataset.id);
          renderJournals();
        });
      });
    }

    function buildParams(page = currentPage) {
      const params = new URLSearchParams();
      params.set('k', $('keywordInput').value.trim() || 'microbiome');
      params.set('status', $('statusSelect').value || 'interpreted');
      params.set('limit', $('limitSelect').value || '10');
      params.set('page', String(page));
      params.set('journals', selectedIds().join(','));
      return params;
    }

    function updateUrl(params) {
      const url = `${window.location.pathname}?${params.toString()}`;
      window.history.pushState({}, '', url);
    }

    function renderChips(params) {
      const selectedLabels = journals.filter((item) => selectedJournals.has(item.id)).slice(0, 3).map((item) => item.journal);
      const extra = Math.max(0, selectedJournals.size - selectedLabels.length);
      const journalText = selectedLabels.length ? `${selectedLabels.join(' / ')}${extra ? ` +${extra}` : ''}` : '未选择期刊';
      $('activeChips').innerHTML = [
        `关键词：${params.get('k')}`,
        `状态：${params.get('status')}`,
        `每页：${params.get('limit')}`,
        `期刊：${journalText}`,
      ].map((text) => `<span class="chip">${escapeHtml(text)}</span>`).join('');
    }

    function formatDate(value) {
      if (!value) return '';
      return String(value).slice(0, 10);
    }

    function renderTags(value) {
      if (!value) return '';
      let tags = [];
      if (Array.isArray(value)) tags = value.map(String);
      else if (typeof value === 'object') tags = Object.keys(value);
      else tags = [String(value)];
      return tags.slice(0, 8).map((tag) => `<span class="meta">${escapeHtml(tag)}</span>`).join('');
    }

    function safeHref(value) {
      const text = String(value || '').trim();
      if (!text) return '';
      if (text.startsWith('/')) return text;
      try {
        const url = new URL(text);
        if (url.protocol === 'http:' || url.protocol === 'https:') return text;
      } catch (error) {
        return '';
      }
      return '';
    }

    function renderPaper(paper) {
      const links = paper.links || {};
      const statusClass = `status-${String(paper.status || '').replace(/[^a-z_]/g, '')}`;
      const briefHref = safeHref(links.brief_html);
      const localPdfHref = safeHref(links.pdf);
      const sourceHref = safeHref(paper.source_url);
      const sourcePdfHref = safeHref(paper.pdf_url);
      const localLinks = [
        briefHref ? `<a class="link-btn primary" target="_blank" rel="noopener" href="${escapeHtml(briefHref)}">Brief HTML</a>` : '',
        localPdfHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(localPdfHref)}">PDF</a>` : '',
        sourceHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(sourceHref)}">来源</a>` : '',
        sourcePdfHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(sourcePdfHref)}">原始 PDF URL</a>` : '',
      ].filter(Boolean).join('');
      const meta = [
        paper.journal || paper.issn ? `期刊：${paper.journal || paper.issn}` : '期刊：未记录',
        paper.source ? `来源：${paper.source}` : '',
        paper.status ? `状态：${paper.status}` : '',
        paper.search_date ? `检索：${formatDate(paper.search_date)}` : '',
        paper.interpret_date ? `解读：${formatDate(paper.interpret_date)}` : '',
        paper.doi ? `DOI：${paper.doi}` : '',
        paper.pmid ? `PMID：${paper.pmid}` : '',
        paper.arxiv_id ? `arXiv：${paper.arxiv_id}` : '',
      ].filter(Boolean).map((item) => `<span class="meta ${item.includes('状态：') ? statusClass : ''}">${escapeHtml(item)}</span>`).join('');
      return `
        <article class="paper-card">
          <h2 class="paper-title">${escapeHtml(paper.title || paper.paper_id || 'Untitled')}</h2>
          <p class="paper-abstract">${escapeHtml(paper.abstract || '暂无摘要。')}</p>
          <div class="meta-row">${meta}</div>
          <div class="tag-row">${renderTags(paper.matched_tags)}</div>
          <div class="links-row">${localLinks || '<span class="meta">暂无本地 brief/PDF 链接</span>'}</div>
        </article>
      `;
    }

    async function loadPapers(page = currentPage, pushUrl = true) {
      currentPage = Math.max(1, page);
      const params = buildParams(currentPage);
      if (pushUrl) updateUrl(params);
      renderChips(params);
      $('papersList').innerHTML = '<div class="empty">正在加载文献...</div>';
      try {
        const data = await fetchJson(`/api/papers?${params.toString()}`);
        lastTotal = data.total || 0;
        const totalPages = Math.max(1, Math.ceil(lastTotal / data.page_size));
        $('resultSummary').textContent = lastTotal
          ? `显示 ${data.offset + 1}-${Math.min(data.offset + data.items.length, lastTotal)}，共 ${lastTotal} 篇`
          : '没有匹配的文献';
        $('pageInfo').textContent = `${data.page} / ${totalPages}`;
        $('prevBtn').disabled = !data.has_prev;
        $('nextBtn').disabled = !data.has_next;
        $('papersList').innerHTML = data.items.length
          ? data.items.map(renderPaper).join('')
          : '<div class="empty">没有找到匹配的文献，请调整关键词、状态或期刊选择。</div>';
      } catch (error) {
        $('papersList').innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      }
    }

    function bindEvents() {
      $('searchBtn').addEventListener('click', () => loadPapers(1));
      $('refreshBtn').addEventListener('click', () => loadPapers(currentPage));
      $('prevBtn').addEventListener('click', () => loadPapers(currentPage - 1));
      $('nextBtn').addEventListener('click', () => loadPapers(currentPage + 1));
      $('keywordInput').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') loadPapers(1);
      });
      $('statusSelect').addEventListener('change', () => loadPapers(1));
      $('limitSelect').addEventListener('change', () => loadPapers(1));
      $('journalSearch').addEventListener('input', renderJournals);
      $('defaultJournalBtn').addEventListener('click', () => {
        selectedJournals = new Set(journals.filter((item) => item.default_selected).map((item) => item.id));
        renderJournals();
        loadPapers(1);
      });
      $('allJournalBtn').addEventListener('click', () => {
        selectedJournals = new Set(journals.map((item) => item.id));
        renderJournals();
        loadPapers(1);
      });
      $('clearJournalBtn').addEventListener('click', () => {
        selectedJournals = new Set();
        renderJournals();
        loadPapers(1);
      });
      window.addEventListener('popstate', async () => {
        const usedUrlSelection = setDefaultsFromUrl();
        if (!usedUrlSelection) {
          selectedJournals = new Set(journals.filter((item) => item.default_selected).map((item) => item.id));
        }
        renderJournals();
        await loadPapers(currentPage, false);
      });
    }

    async function init() {
      bindEvents();
      const usedUrlSelection = setDefaultsFromUrl();
      await Promise.all([loadStats(), loadJournals(usedUrlSelection)]);
      await loadPapers(currentPage, !usedUrlSelection);
    }

    init();
  </script>
</body>
</html>
"""


def _first(query: dict[str, list[str]], key: str, default: str = '') -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _positive_int(value: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _truthy(value: str) -> bool:
    return str(value or '').lower() in {'1', 'true', 'yes', 'on'}


def _resolve_config_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _prepare_config(args) -> tuple[dict, str]:
    config_path = _resolve_config_path(args.config)
    config = load_config(str(config_path))
    db_path = args.db or config.get('db', {}).get('path', 'data/papers.db')
    db_path = Path(db_path)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    config.setdefault('db', {})['path'] = str(db_path)
    return config, str(config_path)


def _safe_artifact_path(config: dict, path_prefix: str, suffix: str, check_db: bool = True) -> Path:
    if not path_prefix or '\x00' in path_prefix or '\\' in path_prefix:
        raise ValueError('Invalid path prefix')
    prefix_path = Path(path_prefix)
    if prefix_path.is_absolute() or '..' in prefix_path.parts:
        raise PermissionError('Path prefix is outside data root')
    target = (DATA_ROOT / f'{path_prefix}{suffix}').resolve()
    data_root = DATA_ROOT.resolve()
    try:
        target.relative_to(data_root)
    except ValueError as exc:
        raise PermissionError('Path prefix is outside data root') from exc
    if check_db:
        conn = get_conn(config)
        row = conn.execute('SELECT 1 FROM papers WHERE path_prefix = ? LIMIT 1',
                           (path_prefix,)).fetchone()
        if row is None:
            raise FileNotFoundError('Unknown path prefix')
    if not target.is_file():
        raise FileNotFoundError('Artifact not found')
    return target


def _add_links(config: dict, paper: dict) -> dict:
    item = dict(paper)
    path_prefix = item.get('path_prefix')
    links = {}
    if path_prefix:
        encoded = quote(path_prefix, safe='')
        try:
            _safe_artifact_path(config, path_prefix, '.brief.html', check_db=False)
            links['brief_html'] = f'/files/brief/{encoded}'
        except (ValueError, PermissionError, FileNotFoundError):
            pass
        try:
            _safe_artifact_path(config, path_prefix, '.pdf', check_db=False)
            links['pdf'] = f'/files/pdf/{encoded}'
        except (ValueError, PermissionError, FileNotFoundError):
            pass
    item['links'] = links
    return item


def make_handler(config: dict, config_path: str):
    class PaperWebHandler(BaseHTTPRequestHandler):
        server_version = 'PaperIntDBViewer/1.0'

        def _send_bytes(self, body: bytes, status: int = 200, content_type: str = 'application/octet-stream',
                        extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
            self._send_bytes(body, status, 'application/json; charset=utf-8')

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json({'error': message}, status)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                if path in {'/', '/index.html'}:
                    return self._send_bytes(INDEX_HTML.encode('utf-8'), 200, 'text/html; charset=utf-8')
                if path == '/api/papers':
                    return self._handle_papers(parsed.query)
                if path == '/api/journals':
                    return self._handle_journals()
                if path == '/api/stats':
                    return self._handle_stats()
                if path.startswith('/files/brief/'):
                    return self._serve_artifact(path[len('/files/brief/'):], '.brief.html', 'text/html; charset=utf-8')
                if path.startswith('/files/pdf/'):
                    return self._serve_artifact(path[len('/files/pdf/'):], '.pdf', 'application/pdf')
                return self._send_error_json(404, 'Not found')
            except Exception as exc:
                return self._send_error_json(500, str(exc))

        def _handle_papers(self, raw_query: str) -> None:
            query = parse_qs(raw_query, keep_blank_values=True)
            page = _positive_int(_first(query, 'page', '1'), 1)
            limit = _positive_int(_first(query, 'limit', '10'), 10, minimum=1, maximum=100)
            offset = (page - 1) * limit
            journals = _first(query, 'journals', None) if 'journals' in query else None
            if journals is None:
                options = get_journal_options_for_viewer(config, config_path=config_path)
                selected = [item['id'] for item in options if item.get('default_selected')]
            else:
                selected = journals
            result = query_papers_for_viewer(
                config,
                config_path=config_path,
                status=_first(query, 'status', 'interpreted'),
                source=_first(query, 'source', None),
                keyword=_first(query, 'k', 'microbiome'),
                keyword_provenance=_first(query, 'keyword_provenance', 'single'),
                found_by=_first(query, 'found_by', None),
                selected_journals=selected,
                include_no_issn=_truthy(_first(query, 'include_no_issn', 'false')),
                limit=limit,
                offset=offset,
            )
            items = [_add_links(config, item) for item in result['items']]
            total = result['total']
            self._send_json({
                'items': items,
                'total': total,
                'page': page,
                'page_size': limit,
                'offset': offset,
                'has_next': offset + len(items) < total,
                'has_prev': page > 1,
            })

        def _handle_journals(self) -> None:
            options = get_journal_options_for_viewer(config, config_path=config_path)
            self._send_json({
                'items': options,
                'default_selected': [item['id'] for item in options if item.get('default_selected')],
                'no_issn_id': NO_ISSN_JOURNAL_ID,
            })

        def _handle_stats(self) -> None:
            conn = get_conn(config)
            self._send_json(get_stats(conn))

        def _serve_artifact(self, encoded_prefix: str, suffix: str, content_type: str) -> None:
            path_prefix = unquote(encoded_prefix)
            try:
                target = _safe_artifact_path(config, path_prefix, suffix, check_db=True)
            except ValueError as exc:
                return self._send_error_json(400, str(exc))
            except PermissionError as exc:
                return self._send_error_json(403, str(exc))
            except FileNotFoundError as exc:
                return self._send_error_json(404, str(exc))
            body = target.read_bytes()
            self._send_bytes(body, 200, content_type, {
                'Content-Disposition': f'inline; filename="{target.name}"',
            })

    return PaperWebHandler


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='paper_web.py',
        description='Serve a dynamic PaperInt database browser.',
    )
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    config, config_path = _prepare_config(args)
    handler = make_handler(config, config_path)
    server = HTTPServer((args.host, args.port), handler)
    print(f'Serving PaperInt DB viewer at http://{args.host}:{args.port}/')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped.')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
