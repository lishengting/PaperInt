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
      grid-template-columns: 320px minmax(0, 1fr);
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
    .filter-note {
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 12px;
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
    .controls {
      display: grid;
      gap: 12px;
    }
    .controls .primary-btn { width: 100%; }
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
    .journal-panel { text-align: left; }
    .journal-head {
      display: grid;
      gap: 10px;
      margin-bottom: 10px;
    }
    .journal-summary { color: var(--muted); font-size: 12px; }
    .journal-actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .small-btn {
      flex: 1 1 72px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #fff;
      color: var(--primary-dark);
      cursor: pointer;
      height: 30px;
      padding: 0 10px;
      font-size: 12px;
      font-weight: 700;
    }
    .journal-search { margin-bottom: 10px; }
    .journal-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
      padding: 2px 2px 8px;
    }
    .journal-item {
      display: flex;
      align-items: baseline;
      gap: 6px;
      font-size: 13px;
      cursor: pointer;
    }
    .journal-item input { margin-top: 0; }
    .journal-item > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .journal-name { font-weight: 700; }
    .journal-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
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
    .paper-title .text-zh,
    .paper-title .text-main {
      font-weight: 800;
    }
    .paper-title .text-en {
      margin-top: 4px;
      color: var(--muted);
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0;
    }
    .paper-authors {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      color: var(--muted);
      margin: -4px 0 12px;
      font-size: 13px;
    }
    .paper-authors-label {
      color: #5f6389;
      font-weight: 700;
    }
    .paper-abstract {
      color: #353966;
      margin: 0 0 14px;
    }
    .paper-abstract .text-zh,
    .paper-abstract .text-en,
    .paper-abstract .text-main {
      display: -webkit-box;
      -webkit-line-clamp: 4;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .paper-abstract .text-en {
      margin-top: 6px;
      color: #5f6389;
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
    .failure-panel {
      margin-top: 12px;
      border: 1px solid rgba(214, 69, 69, 0.22);
      border-radius: 16px;
      background: #fff7f7;
      color: #6f1d1d;
      padding: 12px;
    }
    .failure-title {
      margin-bottom: 8px;
      font-weight: 800;
    }
    .failure-row {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      gap: 8px;
      margin-top: 6px;
      font-size: 12px;
    }
    .failure-label {
      color: var(--danger);
      font-weight: 800;
    }
    .failure-value {
      min-width: 0;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
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
    }
    @media (max-width: 620px) {
      .main { padding: 18px; }
      .result-head { align-items: flex-start; flex-direction: column; }
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
          <div class="brand-subtitle" data-i18n="appSubtitle">文献数据库浏览器</div>
        </div>
      </div>
      <div class="side-card">
        <h3 data-i18n="dbStatus">数据库状态</h3>
        <div class="stats-grid" id="statsGrid"></div>
      </div>
      <div class="side-card">
        <h3 data-i18n="basicFilters">基础筛选</h3>
        <div class="controls">
          <div class="field">
            <label for="keywordInput" data-i18n="keywordLabel">关键词</label>
            <input id="keywordInput" class="input" value="microbiome" placeholder="microbiome">
          </div>
          <div class="field">
            <label for="statusSelect" data-i18n="statusLabelText">状态</label>
            <select id="statusSelect">
              <option value="ALL">全部</option>
              <option value="searched">未下载</option>
              <option value="downloaded">未解读</option>
              <option value="interpreted" selected>已解读</option>
              <option value="download_failed">下载失败</option>
              <option value="interpret_failed">解读失败</option>
            </select>
          </div>
          <div class="field">
            <label for="limitSelect" data-i18n="limitLabel">每页数量</label>
            <select id="limitSelect">
              <option>5</option>
              <option selected>10</option>
              <option>20</option>
              <option>50</option>
              <option>100</option>
            </select>
          </div>
          <button class="primary-btn" id="searchBtn" data-i18n="searchButton">搜索</button>
        </div>
      </div>
      <div class="side-card journal-panel">
        <h3 data-i18n="journalsTitle">期刊 / ISSN</h3>
        <p class="filter-note" data-i18n="journalNote">默认选中 Nature、Science、Cell 及其子刊配置中匹配到的数据库期刊。没有 ISSN 的条目统一归入预印版。</p>
        <div class="journal-head">
          <div class="journal-summary" id="journalSummary" data-i18n="loadingJournals">正在载入期刊列表...</div>
          <div class="journal-actions">
            <button class="small-btn" id="defaultJournalBtn" data-i18n="defaultCnsButton">默认 CNS</button>
            <button class="small-btn" id="allJournalBtn" data-i18n="selectAllButton">全选</button>
            <button class="small-btn" id="clearJournalBtn" data-i18n="clearButton">清空</button>
          </div>
        </div>
        <input id="journalSearch" class="input journal-search" placeholder="搜索期刊名、ISSN 或别名" data-i18n-placeholder="journalSearchPlaceholder">
        <div class="journal-list" id="journalList"></div>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <span data-i18n="topbarDb">本地 SQLite</span>
        <span data-i18n="topbarApi">安全文件 API</span>
        <span data-i18n="topbarPagination">动态分页</span>
        <a href="#" id="langSwitch">English</a>
      </div>
      <section class="hero">
        <h1 data-i18n="heroTitle">探索已检索和解读的生物医学文献</h1>
        <p data-i18n="heroSubtitle">按关键词、状态、期刊和 ISSN 组合筛选论文，并通过 HTTP API 打开本地生成的 interpret/brief HTML 与 PDF。</p>
      </section>
      <div class="chips" id="activeChips"></div>
      <section class="results">
        <div class="result-head">
          <div id="resultSummary" data-i18n="readyToLoad">准备加载结果</div>
          <button class="ghost-btn" id="refreshBtn" data-i18n="refreshButton">刷新</button>
        </div>
        <div id="papersList"></div>
        <div class="pagination">
          <button class="ghost-btn" id="prevBtn" data-i18n="prevButton">上一页</button>
          <span id="pageInfo">1 / 1</span>
          <button class="ghost-btn" id="nextBtn" data-i18n="nextButton">下一页</button>
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
    let currentLang = 'zh';

    const $ = (id) => document.getElementById(id);
    const I18N = {
      zh: {
        htmlLang: 'zh-CN',
        title: 'PaperInt 文献浏览器',
        appSubtitle: '文献数据库浏览器',
        dbStatus: '数据库状态',
        basicFilters: '基础筛选',
        keywordLabel: '关键词',
        statusLabelText: '状态',
        limitLabel: '每页数量',
        searchButton: '搜索',
        journalsTitle: '期刊 / ISSN',
        journalNote: '默认选中 Nature、Science、Cell 及其子刊配置中匹配到的数据库期刊。没有 ISSN 的条目统一归入预印版。',
        loadingJournals: '正在载入期刊列表...',
        defaultCnsButton: '默认 CNS',
        selectAllButton: '全选',
        clearButton: '清空',
        journalSearchPlaceholder: '搜索期刊名、ISSN 或别名',
        topbarDb: '本地 SQLite',
        topbarApi: '安全文件 API',
        topbarPagination: '动态分页',
        heroTitle: '探索已检索和解读的生物医学文献',
        heroSubtitle: '按关键词、状态、期刊和 ISSN 组合筛选论文，并通过 HTTP API 打开本地生成的 interpret/brief HTML 与 PDF。',
        readyToLoad: '准备加载结果',
        refreshButton: '刷新',
        prevButton: '上一页',
        nextButton: '下一页',
        langSwitch: 'English',
        total: '总数',
        selectedJournals: (selected, total) => `已选 ${selected} / ${total} 个期刊选项`,
        noIssn: '无 ISSN',
        paperUnit: '篇',
        noMatchingJournals: '没有匹配的期刊选项。',
        noJournalsSelected: '未选择期刊',
        chipKeyword: '关键词',
        chipStatus: '状态',
        chipPerPage: '每页',
        chipJournals: '期刊',
        loadingPapers: '正在加载文献...',
        noMatchingPapersSummary: '没有匹配的文献',
        noMatchingPapersBody: '没有找到匹配的文献，请调整关键词、状态或期刊选择。',
        resultSummary: (start, end, total) => `显示 ${start}-${end}，共 ${total} 篇`,
        authors: '作者',
        journal: '期刊',
        notRecorded: '未记录',
        source: '来源',
        status: '状态',
        searched: '检索',
        interpreted: '解读',
        noAbstract: '暂无摘要。',
        sourceLink: '来源',
        originalPdfUrl: '原始 PDF URL',
        noLocalLinks: '暂无本地 HTML/PDF 链接',
        briefChinese: 'Brief 中文',
        interpretChinese: 'Interpret 中文',
        downloadFailureReason: '下载失败原因',
        interpretFailureReason: '解读失败原因',
        failurePhase: '阶段',
        failureCategory: '分类',
        failureSubtype: '子类',
        failureTags: '标签',
        failureDetails: '详情',
        failureError: '错误',
        statuses: {
          ALL: '全部',
          searched: '未下载',
          downloaded: '未解读',
          interpreted: '已解读',
          download_failed: '下载失败',
          interpret_failed: '解读失败',
        },
      },
      en: {
        htmlLang: 'en',
        title: 'PaperInt Literature Viewer',
        appSubtitle: 'Literature database viewer',
        dbStatus: 'Database status',
        basicFilters: 'Basic filters',
        keywordLabel: 'Keyword',
        statusLabelText: 'Status',
        limitLabel: 'Per page',
        searchButton: 'Search',
        journalsTitle: 'Journals / ISSN',
        journalNote: 'Default selection includes database journals matching Nature, Science, Cell, and configured sub-journals. Items without ISSN are grouped as preprints.',
        loadingJournals: 'Loading journal list...',
        defaultCnsButton: 'Default CNS',
        selectAllButton: 'Select all',
        clearButton: 'Clear',
        journalSearchPlaceholder: 'Search journal name, ISSN, or alias',
        topbarDb: 'Local SQLite',
        topbarApi: 'Safe file API',
        topbarPagination: 'Dynamic pagination',
        heroTitle: 'Explore searched and interpreted biomedical papers',
        heroSubtitle: 'Filter papers by keyword, status, journal, and ISSN, then open locally generated interpret/brief HTML and PDFs through the HTTP API.',
        readyToLoad: 'Ready to load results',
        refreshButton: 'Refresh',
        prevButton: 'Previous',
        nextButton: 'Next',
        langSwitch: '中文',
        total: 'Total',
        selectedJournals: (selected, total) => `Selected ${selected} / ${total} journal options`,
        noIssn: 'No ISSN',
        paperUnit: 'papers',
        noMatchingJournals: 'No matching journal options.',
        noJournalsSelected: 'No journals selected',
        chipKeyword: 'Keyword',
        chipStatus: 'Status',
        chipPerPage: 'Per page',
        chipJournals: 'Journals',
        loadingPapers: 'Loading papers...',
        noMatchingPapersSummary: 'No matching papers',
        noMatchingPapersBody: 'No matching papers found. Adjust keyword, status, or journal selection.',
        resultSummary: (start, end, total) => `Showing ${start}-${end} of ${total} papers`,
        authors: 'Authors',
        journal: 'Journal',
        notRecorded: 'Not recorded',
        source: 'Source',
        status: 'Status',
        searched: 'Searched',
        interpreted: 'Interpreted',
        noAbstract: 'No abstract available.',
        sourceLink: 'Source',
        originalPdfUrl: 'Original PDF URL',
        noLocalLinks: 'No local HTML/PDF links',
        briefChinese: 'Brief Chinese',
        interpretChinese: 'Interpret Chinese',
        downloadFailureReason: 'Download failure reason',
        interpretFailureReason: 'Interpretation failure reason',
        failurePhase: 'Phase',
        failureCategory: 'Category',
        failureSubtype: 'Subtype',
        failureTags: 'Tags',
        failureDetails: 'Details',
        failureError: 'Error',
        statuses: {
          ALL: 'All',
          searched: 'Not downloaded',
          downloaded: 'Not interpreted',
          interpreted: 'Interpreted',
          download_failed: 'Download failed',
          interpret_failed: 'Interpret failed',
        },
      },
    };

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

    function normalizeLang(value) {
      return value === 'en' ? 'en' : 'zh';
    }

    function isEn() {
      return currentLang === 'en';
    }

    function t(key) {
      const langData = I18N[currentLang] || I18N.zh;
      return langData[key] || I18N.zh[key] || key;
    }

    function statusLabel(value) {
      const langData = I18N[currentLang] || I18N.zh;
      return (langData.statuses || {})[value] || value;
    }

    function labeled(label, value) {
      return isEn() ? `${label}: ${value}` : `${label}：${value}`;
    }

    function updateStatusOptions() {
      Array.from($('statusSelect').options).forEach((option) => {
        option.textContent = statusLabel(option.value);
      });
    }

    function applyI18n() {
      document.documentElement.lang = t('htmlLang');
      document.title = t('title');
      document.querySelectorAll('[data-i18n]').forEach((node) => {
        node.textContent = t(node.dataset.i18n);
      });
      document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
        node.placeholder = t(node.dataset.i18nPlaceholder);
      });
      updateStatusOptions();
      $('langSwitch').textContent = t('langSwitch');
    }

    function setDefaultsFromUrl() {
      const params = new URLSearchParams(window.location.search);
      currentLang = normalizeLang(params.get('lang'));
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
        const total = Object.values(stats).reduce((sum, value) => sum + Number(value || 0), 0);
        const items = [
          [t('total'), total],
          [statusLabel('searched'), stats.searched || 0],
          [statusLabel('downloaded'), stats.downloaded || 0],
          [statusLabel('interpreted'), stats.interpreted || 0],
          [statusLabel('download_failed'), stats.download_failed || 0],
          [statusLabel('interpret_failed'), stats.interpret_failed || 0],
        ];
        $('statsGrid').innerHTML = items.map(([label, value]) => `
          <div class="stat">
            <div class="stat-value">${escapeHtml(value)}</div>
            <div class="stat-label">${escapeHtml(label)}</div>
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
      $('journalSummary').textContent = t('selectedJournals')(selectedJournals.size, journals.length);
      $('journalList').innerHTML = visible.map((item) => {
        const checked = selectedJournals.has(item.id) ? 'checked' : '';
        const paperCount = isEn() ? `${item.count} ${item.count === 1 ? 'paper' : 'papers'}` : `${item.count} ${t('paperUnit')}`;
        const meta = item.is_preprint ? `${t('noIssn')} · ${paperCount}` : `${item.issn || ''} · ${paperCount}`;
        return `
          <label class="journal-item">
            <input type="checkbox" data-id="${escapeHtml(item.id)}" ${checked}>
            <span class="journal-name">${escapeHtml(item.journal)}</span>
            <span class="journal-meta">${escapeHtml(meta)}</span>
          </label>
        `;
      }).join('') || `<div class="empty">${escapeHtml(t('noMatchingJournals'))}</div>`;
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
      params.set('k', $('keywordInput').value.trim());
      params.set('status', $('statusSelect').value || 'interpreted');
      params.set('limit', $('limitSelect').value || '10');
      params.set('page', String(page));
      params.set('journals', selectedIds().join(','));
      if (isEn()) params.set('lang', 'en');
      return params;
    }

    function updateUrl(params) {
      const url = `${window.location.pathname}?${params.toString()}`;
      window.history.pushState({}, '', url);
    }

    function renderChips(params) {
      const selectedLabels = journals.filter((item) => selectedJournals.has(item.id)).slice(0, 3).map((item) => item.journal);
      const extra = Math.max(0, selectedJournals.size - selectedLabels.length);
      const journalText = selectedLabels.length ? `${selectedLabels.join(' / ')}${extra ? ` +${extra}` : ''}` : t('noJournalsSelected');
      $('activeChips').innerHTML = [
        labeled(t('chipKeyword'), params.get('k')),
        labeled(t('chipStatus'), statusLabel(params.get('status'))),
        labeled(t('chipPerPage'), params.get('limit')),
        labeled(t('chipJournals'), journalText),
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

    function renderBilingualText(en, zh, fallback, className) {
      const enText = String(en || '').trim();
      const zhText = String(zh || '').trim();
      const mainText = enText || fallback;
      if (isEn()) {
        return `
          <div class="${className}">
            <div class="text-main">${escapeHtml(mainText)}</div>
          </div>
        `;
      }
      if (zhText && enText) {
        return `
          <div class="${className} bilingual">
            <div class="text-zh">${escapeHtml(zhText)}</div>
            <div class="text-en">${escapeHtml(enText)}</div>
          </div>
        `;
      }
      return `
        <div class="${className}">
          <div class="text-main">${escapeHtml(mainText)}</div>
        </div>
      `;
    }

    function formatFailureValue(value) {
      if (value === null || value === undefined || value === '') return '';
      if (Array.isArray(value)) return value.map(formatFailureValue).filter(Boolean).join(isEn() ? ', ' : '、');
      if (typeof value === 'object') {
        return Object.entries(value)
          .map(([key, item]) => [key, formatFailureValue(item)])
          .filter(([, item]) => item)
          .map(([key, item]) => `${key}: ${item}`)
          .join(isEn() ? '; ' : '；');
      }
      return String(value).trim();
    }

    function renderFailureDetails(paper) {
      const status = String(paper.status || '');
      if (status !== 'download_failed' && status !== 'interpret_failed') return '';
      const rows = [
        [t('failurePhase'), paper.failure_phase],
        [t('failureCategory'), paper.failure_category],
        [t('failureSubtype'), paper.failure_subtype],
        [t('failureTags'), paper.failure_tags],
        [t('failureDetails'), paper.failure_metadata],
        [t('failureError'), paper.error_message],
      ].map(([label, value]) => [label, formatFailureValue(value)])
        .filter(([, value]) => value);
      if (!rows.length) return '';
      const title = status === 'download_failed' ? t('downloadFailureReason') : t('interpretFailureReason');
      return `
        <div class="failure-panel">
          <div class="failure-title">${escapeHtml(title)}</div>
          ${rows.map(([label, value]) => `
            <div class="failure-row">
              <span class="failure-label">${escapeHtml(label)}</span>
              <span class="failure-value">${escapeHtml(value)}</span>
            </div>
          `).join('')}
        </div>
      `;
    }

    function renderPaper(paper) {
      const links = paper.links || {};
      const statusClass = `status-${String(paper.status || '').replace(/[^a-z_]/g, '')}`;
      const interpretEnHref = safeHref(links.interpret_html_en);
      const interpretZhHref = safeHref(links.interpret_html_zh);
      const briefEnHref = safeHref(links.brief_html_en);
      const briefZhHref = safeHref(links.brief_html_zh);
      const localPdfHref = safeHref(links.pdf);
      const sourceHref = safeHref(paper.source_url);
      const sourcePdfHref = safeHref(paper.pdf_url);
      const localLinks = [
        briefEnHref ? `<a class="link-btn primary" target="_blank" rel="noopener" href="${escapeHtml(briefEnHref)}">Brief EN</a>` : '',
        briefZhHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(briefZhHref)}">${escapeHtml(t('briefChinese'))}</a>` : '',
        interpretEnHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(interpretEnHref)}">Interpret EN</a>` : '',
        interpretZhHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(interpretZhHref)}">${escapeHtml(t('interpretChinese'))}</a>` : '',
        localPdfHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(localPdfHref)}">PDF</a>` : '',
        sourceHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(sourceHref)}">${escapeHtml(t('sourceLink'))}</a>` : '',
        sourcePdfHref ? `<a class="link-btn" target="_blank" rel="noopener" href="${escapeHtml(sourcePdfHref)}">${escapeHtml(t('originalPdfUrl'))}</a>` : '',
      ].filter(Boolean).join('');
      const authors = String(paper.authors || '').trim();
      const authorsHtml = authors
        ? `<div class="paper-authors"><span class="paper-authors-label">${escapeHtml(labeled(t('authors'), ''))}</span>${escapeHtml(authors)}</div>`
        : '';
      const metaItems = [
        { text: paper.journal || paper.issn ? labeled(t('journal'), paper.journal || paper.issn) : labeled(t('journal'), t('notRecorded')) },
        { text: paper.source ? labeled(t('source'), paper.source) : '' },
        { text: paper.status ? labeled(t('status'), statusLabel(paper.status)) : '', className: statusClass },
        { text: paper.search_date ? labeled(t('searched'), formatDate(paper.search_date)) : '' },
        { text: paper.interpret_date ? labeled(t('interpreted'), formatDate(paper.interpret_date)) : '' },
        { text: paper.doi ? labeled('DOI', paper.doi) : '' },
        { text: paper.pmid ? labeled('PMID', paper.pmid) : '' },
        { text: paper.arxiv_id ? labeled('arXiv', paper.arxiv_id) : '' },
      ].filter((item) => item.text);
      const meta = metaItems.map((item) => `<span class="meta ${item.className || ''}">${escapeHtml(item.text)}</span>`).join('');
      const titleHtml = renderBilingualText(
        paper.title || paper.paper_id || '',
        paper.title_zh,
        'Untitled',
        'paper-title'
      );
      const abstractHtml = renderBilingualText(
        paper.abstract || '',
        paper.abstract_zh,
        t('noAbstract'),
        'paper-abstract'
      );
      const failureHtml = renderFailureDetails(paper);
      return `
        <article class="paper-card">
          ${titleHtml}
          ${authorsHtml}
          ${abstractHtml}
          <div class="meta-row">${meta}</div>
          ${failureHtml}
          <div class="tag-row">${renderTags(paper.matched_tags)}</div>
          <div class="links-row">${localLinks || `<span class="meta">${escapeHtml(t('noLocalLinks'))}</span>`}</div>
        </article>
      `;
    }

    async function loadPapers(page = currentPage, pushUrl = true) {
      currentPage = Math.max(1, page);
      const params = buildParams(currentPage);
      if (pushUrl) updateUrl(params);
      renderChips(params);
      $('papersList').innerHTML = `<div class="empty">${escapeHtml(t('loadingPapers'))}</div>`;
      try {
        const data = await fetchJson(`/api/papers?${params.toString()}`);
        lastTotal = data.total || 0;
        const totalPages = Math.max(1, Math.ceil(lastTotal / data.page_size));
        $('resultSummary').textContent = lastTotal
          ? t('resultSummary')(data.offset + 1, Math.min(data.offset + data.items.length, lastTotal), lastTotal)
          : t('noMatchingPapersSummary');
        $('pageInfo').textContent = `${data.page} / ${totalPages}`;
        $('prevBtn').disabled = !data.has_prev;
        $('nextBtn').disabled = !data.has_next;
        $('papersList').innerHTML = data.items.length
          ? data.items.map(renderPaper).join('')
          : `<div class="empty">${escapeHtml(t('noMatchingPapersBody'))}</div>`;
      } catch (error) {
        $('papersList').innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      }
    }

    function bindEvents() {
      $('langSwitch').addEventListener('click', (event) => {
        event.preventDefault();
        currentLang = isEn() ? 'zh' : 'en';
        applyI18n();
        loadStats();
        renderJournals();
        loadPapers(currentPage);
      });
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
        applyI18n();
        if (!usedUrlSelection) {
          selectedJournals = new Set(journals.filter((item) => item.default_selected).map((item) => item.id));
        }
        renderJournals();
        await loadStats();
        await loadPapers(currentPage, false);
      });
    }

    async function init() {
      const usedUrlSelection = setDefaultsFromUrl();
      applyI18n();
      bindEvents();
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
        html_links = [
            ('interpret_html_en', '.interpret.html', 'interpret/en'),
            ('interpret_html_zh', '.interpret.zh.html', 'interpret/zh'),
            ('brief_html_en', '.brief.html', 'brief/en'),
            ('brief_html_zh', '.brief.zh.html', 'brief/zh'),
        ]
        for key, suffix, route in html_links:
            try:
                _safe_artifact_path(config, path_prefix, suffix, check_db=False)
                links[key] = f'/files/{route}/{encoded}'
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
                if path.startswith('/files/interpret/en/'):
                    return self._serve_artifact(path[len('/files/interpret/en/'):], '.interpret.html', 'text/html; charset=utf-8')
                if path.startswith('/files/interpret/zh/'):
                    return self._serve_artifact(path[len('/files/interpret/zh/'):], '.interpret.zh.html', 'text/html; charset=utf-8')
                if path.startswith('/files/brief/en/'):
                    return self._serve_artifact(path[len('/files/brief/en/'):], '.brief.html', 'text/html; charset=utf-8')
                if path.startswith('/files/brief/zh/'):
                    return self._serve_artifact(path[len('/files/brief/zh/'):], '.brief.zh.html', 'text/html; charset=utf-8')
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
