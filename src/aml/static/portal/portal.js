/* ============================================================
   GRAFOMEM Cloud Portal — Application Logic
   ============================================================ */

(() => {
  'use strict';

  // ── State ───────────────────────────────────────────────────
  let token = localStorage.getItem('gfm_token');
  let tenantData = null;
  let keyRevealed = false;

  // ── API ─────────────────────────────────────────────────────
  const API = '';  // Same origin

  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...opts.headers };
    if (token && !opts.noAuth) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    const res = await fetch(`${API}${path}`, {
      method: opts.method || 'GET',
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || err.message || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // ── Auth ────────────────────────────────────────────────────
  async function signup(name, email, password, plan) {
    const data = await api('/v1/portal/signup', {
      method: 'POST',
      body: { name, email, password, plan },
      noAuth: true,
    });
    token = data.token;
    localStorage.setItem('gfm_token', token);
    if (data.api_key) localStorage.setItem('gfm_api_key', data.api_key);
    tenantData = data;
    showDashboard();
    loadDashboard();
    toast('Account created! Welcome to GRAFOMEM.', 'success');
  }

  async function login(email, password) {
    const data = await api('/v1/portal/login', {
      method: 'POST',
      body: { email, password },
      noAuth: true,
    });
    token = data.token;
    localStorage.setItem('gfm_token', token);
    if (data.api_key) localStorage.setItem('gfm_api_key', data.api_key);
    tenantData = data;
    showDashboard();
    loadDashboard();
    toast('Logged in successfully.', 'success');
  }

  function logout() {
    token = null;
    tenantData = null;
    keyRevealed = false;
    localStorage.removeItem('gfm_token');
    localStorage.removeItem('gfm_api_key');
    showAuth();
    toast('Logged out.');
  }

  // ── Dashboard ───────────────────────────────────────────────
  async function loadDashboard() {
    try {
      const data = await api('/v1/portal/me');
      tenantData = data;
      renderDashboard(data);
    } catch (e) {
      if (e.message.includes('401') || e.message.includes('Invalid') || e.message.includes('expired')) {
        logout();
        return;
      }
      toast(e.message, 'error');
    }
  }

  function renderDashboard(d) {
    // Sidebar
    $('sidebar-name').textContent = d.name;
    $('sidebar-plan').textContent = d.plan;

    // Overview stats
    $('stat-plan').textContent = capitalize(d.plan);
    $('stat-ops').textContent = formatNumber(d.usage?.total_operations || 0);

    if (d.compliance?.conformance_rate != null) {
      $('stat-m8').textContent = d.compliance.conformance_rate.toFixed(3);
      $('stat-caps').textContent = `${d.compliance.capabilities?.length || 0}/10`;
    } else {
      $('stat-m8').textContent = '—';
      $('stat-caps').textContent = '—';
    }

    // Quick start API key
    const qsKeys = document.querySelectorAll('#qs-key, .code-highlight');
    qsKeys.forEach(el => {
      if (el.id === 'qs-key') el.textContent = d.api_key;
    });

    // API Key section
    if (!keyRevealed) {
      $('api-key-display').textContent = maskKey(d.api_key);
    } else {
      $('api-key-display').textContent = d.api_key;
    }

    // Usage
    if (d.usage) {
      $('usage-writes').textContent = formatNumber(d.usage.writes);
      $('usage-reads').textContent = formatNumber(d.usage.reads);
      $('usage-deletes').textContent = formatNumber(d.usage.deletes);
      $('usage-supersedes').textContent = formatNumber(d.usage.supersedes);
      $('usage-bytes').textContent = formatBytes(d.usage.total_bytes);
      $('usage-total').textContent = formatNumber(d.usage.total_operations);
    }

    // Compliance
    renderCompliance(d.compliance);

    // Billing
    renderBilling(d);
  }

  // ── Compliance ──────────────────────────────────────────────
  function renderCompliance(c) {
    const badge = $('m8-badge');
    const score = $('m8-score');
    const status = $('m8-status');

    if (!c || c.conformance_rate == null) {
      score.textContent = '—';
      status.textContent = '';
      badge.className = 'm8-badge';
      $('capabilities-grid').innerHTML = '<div class="cap-placeholder">No audit data available. Run <code>grafomem conformance</code> to generate.</div>';
      $('badge-preview').style.display = 'none';
      $('badge-placeholder').style.display = 'block';
      return;
    }

    const rate = c.conformance_rate;
    const passed = rate >= 0.95;
    score.textContent = rate.toFixed(3);
    status.textContent = passed ? '✅ PASS' : '❌ FAIL';
    status.style.color = passed ? 'var(--success)' : 'var(--danger)';
    badge.className = `m8-badge ${passed ? 'pass' : 'fail'}`;

    // Capabilities
    const caps = c.capabilities || [];
    const allCaps = ['audit', 'bi_temporal', 'cryptographic_provenance', 'hard_delete',
                     'multi_tenant', 'provenance', 'supersession_chain',
                     'conflict_detection', 'cross_session_propagation', 'concurrency_control'];
    const grid = $('capabilities-grid');
    grid.innerHTML = allCaps.map(cap => {
      const has = caps.includes(cap);
      return `<div class="cap-item">
        <span class="cap-icon ${has ? 'cap-pass' : 'cap-fail'}">${has ? '✓' : '✕'}</span>
        ${cap}
      </div>`;
    }).join('');

    // Badge preview
    if (tenantData) {
      const badgeUrl = `${API}/v1/cloud/compliance/badge/${tenantData.tenant_id}.svg`;
      $('badge-preview').src = badgeUrl;
      $('badge-preview').style.display = 'block';
      $('badge-placeholder').style.display = 'none';
    }
  }

  // ── Billing ─────────────────────────────────────────────────
  function renderBilling(d) {
    const plan = d.plan || 'starter';
    $('current-plan-badge').textContent = capitalize(plan);
    $('billing-status').textContent = d.billing?.status === 'active' ? 'Active' :
                                      d.billing?.status || 'Active';

    // Update plan buttons
    const plans = ['starter', 'pro', 'enterprise'];
    plans.forEach(p => {
      const btn = $(`btn-${p}`);
      if (!btn) return;
      if (p === plan) {
        btn.textContent = 'Current Plan';
        btn.disabled = true;
        btn.classList.remove('btn-primary');
      } else if (p === 'enterprise') {
        btn.textContent = 'Contact Us';
        btn.disabled = false;
      } else {
        btn.textContent = plan === 'pro' && p === 'starter' ? 'Downgrade' : 'Upgrade';
        btn.disabled = false;
        if (p === 'pro') btn.classList.add('btn-primary');
      }
    });
  }

  async function upgradePlan(plan) {
    try {
      const data = await api('/v1/portal/upgrade', {
        method: 'POST',
        body: { plan },
      });
      if (data.checkout_url) {
        window.location.href = data.checkout_url;
      } else {
        toast('Upgrade initiated. Redirecting...', 'success');
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  // ── Key Management ──────────────────────────────────────────
  function revealKey() {
    if (!tenantData) return;
    keyRevealed = !keyRevealed;
    $('api-key-display').textContent = keyRevealed ? tenantData.api_key : maskKey(tenantData.api_key);
    $('reveal-key-btn').textContent = keyRevealed ? 'Hide' : 'Reveal';
  }

  async function copyKey() {
    if (!tenantData) return;
    try {
      await navigator.clipboard.writeText(tenantData.api_key);
      toast('API key copied to clipboard.', 'success');
    } catch {
      toast('Failed to copy. Please copy manually.', 'error');
    }
  }

  async function rotateKey() {
    if (!confirm('Are you sure? The old key will stop working immediately. All integrations using it will break.')) return;
    try {
      const data = await api('/v1/portal/rotate-key', { method: 'POST' });
      tenantData.api_key = data.api_key;
      $('api-key-display').textContent = data.api_key;
      keyRevealed = true;
      $('reveal-key-btn').textContent = 'Hide';
      toast('API key rotated successfully. Copy your new key!', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  // ── Badge Copy ──────────────────────────────────────────────
  function copyBadgeMd() {
    if (!tenantData) return;
    const url = `${window.location.origin}/v1/cloud/compliance/badge/${tenantData.tenant_id}.svg`;
    const md = `[![GMP Conformance](${url})](https://grafomem.com)`;
    navigator.clipboard.writeText(md).then(
      () => toast('Markdown copied!', 'success'),
      () => toast('Copy failed', 'error')
    );
  }

  function copyBadgeHtml() {
    if (!tenantData) return;
    const url = `${window.location.origin}/v1/cloud/compliance/badge/${tenantData.tenant_id}.svg`;
    const html = `<a href="https://grafomem.com"><img src="${url}" alt="GMP Conformance" /></a>`;
    navigator.clipboard.writeText(html).then(
      () => toast('HTML copied!', 'success'),
      () => toast('Copy failed', 'error')
    );
  }

  // ── View switching ──────────────────────────────────────────
  function showAuth() {
    $('auth-view').style.display = '';
    $('dashboard-view').style.display = 'none';
  }

  function showDashboard() {
    $('auth-view').style.display = 'none';
    $('dashboard-view').style.display = 'flex';
  }

  function showSection(name) {
    document.querySelectorAll('.section').forEach(s => s.style.display = 'none');
    const section = $(`section-${name}`);
    if (section) section.style.display = '';

    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const nav = $(`nav-${name}`);
    if (nav) nav.classList.add('active');

    // Close mobile sidebar
    $('sidebar').classList.remove('open');
  }

  // ── Formatters ──────────────────────────────────────────────
  function formatNumber(n) {
    return n.toLocaleString('en-US');
  }

  function formatBytes(b) {
    if (b === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(b) / Math.log(1024));
    return `${(b / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
  }

  function maskKey(key) {
    if (!key) return 'gfm_••••••••••••';
    return key.substring(0, 8) + '••••••••••••••••';
  }

  function capitalize(s) {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
  }

  // ── Toast ───────────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg, type = '') {
    const el = $('toast');
    el.textContent = msg;
    el.className = `toast visible ${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.className = 'toast';
    }, 4000);
  }

  // ── Helpers ─────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }

  // ── Decision Trail / Audit Console ──────────────────────────
  let dtPage = 0;
  const dtLimit = 20;
  let dtDecisions = [];

  async function loadDecisionStats() {
    try {
      const stats = await api('/v1/decisions/stats');
      $('dt-stat-total').textContent = formatNumber(stats.total || 0);
      $('dt-stat-models').textContent = stats.models_used ?? '—';
      $('dt-stat-latency').textContent = stats.avg_latency_ms ? `${stats.avg_latency_ms}ms` : '—';
      $('dt-stat-tokens').textContent = stats.total_tokens ? formatNumber(stats.total_tokens) : '—';
    } catch (e) {
      // Decision Trail not available — leave defaults
    }
  }

  async function loadDecisions() {
    const params = new URLSearchParams();
    const model = $('dt-filter-model')?.value?.trim();
    const store = $('dt-filter-store')?.value?.trim();
    const session = $('dt-filter-session')?.value?.trim();

    if (model) params.set('model_id', model);
    if (store) params.set('store_id', store);
    if (session) params.set('session_id', session);
    params.set('limit', dtLimit);
    params.set('offset', dtPage * dtLimit);

    try {
      const data = await api(`/v1/decisions/?${params.toString()}`);
      dtDecisions = data.decisions || [];
      renderDecisionTable(dtDecisions, data.count);

      // Pagination
      $('dt-prev-btn').disabled = dtPage === 0;
      $('dt-next-btn').disabled = data.count < dtLimit;
      $('dt-page-info').textContent = `Page ${dtPage + 1}`;
    } catch (e) {
      $('dt-table-body').innerHTML = `<tr class="dt-empty-row"><td colspan="8">Unable to load decisions: ${e.message}</td></tr>`;
      $('dt-count').textContent = 'Error';
    }
  }

  function renderDecisionTable(decisions, count) {
    const tbody = $('dt-table-body');
    $('dt-count').textContent = `${count} result${count !== 1 ? 's' : ''}`;

    if (!decisions.length) {
      tbody.innerHTML = '<tr class="dt-empty-row"><td colspan="8">No decisions match your filters.</td></tr>';
      return;
    }

    tbody.innerHTML = decisions.map(d => {
      const time = new Date(d.created_at).toLocaleString('en-GB', {
        month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
      const factsCount = (d.retrieved_fact_refs || []).length;
      const signed = d.signature != null;
      const query = (d.query || '').length > 50 ? d.query.substring(0, 50) + '…' : d.query;
      const tokens = d.output_tokens != null ? formatNumber(d.output_tokens) : '—';
      const latency = d.latency_ms != null ? `${d.latency_ms}ms` : '—';

      return `<tr data-id="${d.decision_id}">
        <td class="dt-time">${time}</td>
        <td class="dt-model">${d.model_id}</td>
        <td class="dt-query" title="${escapeHtml(d.query)}">${escapeHtml(query)}</td>
        <td class="dt-facts-count">${factsCount}</td>
        <td class="dt-tokens">${tokens}</td>
        <td class="dt-latency">${latency}</td>
        <td><span class="dt-signed-badge ${signed ? 'signed' : 'unsigned'}">${signed ? '🔏 Signed' : '—'}</span></td>
        <td><button class="btn btn-sm dt-view-btn" data-id="${d.decision_id}">View</button></td>
      </tr>`;
    }).join('');

    // Attach click handlers
    tbody.querySelectorAll('tr[data-id]').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('.dt-view-btn')) return;
        showDecisionDetail(row.dataset.id);
      });
    });
    tbody.querySelectorAll('.dt-view-btn').forEach(btn => {
      btn.addEventListener('click', () => showDecisionDetail(btn.dataset.id));
    });
  }

  async function showDecisionDetail(decisionId) {
    const panel = $('dt-detail-panel');
    panel.style.display = '';

    // Find in local cache first
    let d = dtDecisions.find(x => x.decision_id === decisionId);

    // If not found locally, fetch from API
    if (!d) {
      try {
        d = await api(`/v1/decisions/${decisionId}`);
      } catch (e) {
        toast(`Decision not found: ${e.message}`, 'error');
        return;
      }
    }

    // Meta tags
    const meta = $('dt-detail-meta');
    const time = new Date(d.created_at).toLocaleString('en-GB');
    meta.innerHTML = [
      `<span class="dt-meta-tag"><span class="label">ID</span> ${d.decision_id.substring(0, 12)}…</span>`,
      `<span class="dt-meta-tag"><span class="label">Time</span> ${time}</span>`,
      `<span class="dt-meta-tag"><span class="label">Model</span> ${d.model_id}</span>`,
      `<span class="dt-meta-tag"><span class="label">Store</span> ${d.store_id}</span>`,
      d.session_id ? `<span class="dt-meta-tag"><span class="label">Session</span> ${d.session_id.substring(0, 12)}…</span>` : '',
      d.latency_ms != null ? `<span class="dt-meta-tag"><span class="label">Latency</span> ${d.latency_ms}ms</span>` : '',
      d.output_tokens != null ? `<span class="dt-meta-tag"><span class="label">Tokens</span> ${formatNumber(d.output_tokens)}</span>` : '',
    ].filter(Boolean).join('');

    // Query
    $('dt-detail-query').textContent = d.query;

    // Facts
    const factsDiv = $('dt-detail-facts');
    const refs = d.retrieved_fact_refs || [];
    const contents = d.retrieved_contents || [];
    const scores = d.retrieval_scores || [];
    if (refs.length === 0) {
      factsDiv.innerHTML = '<div class="dim">No facts retrieved</div>';
    } else {
      factsDiv.innerHTML = refs.map((ref, i) => `
        <div class="dt-fact-item">
          <span class="dt-fact-ref">#${ref}</span>
          <span class="dt-fact-content">${escapeHtml(contents[i] || '—')}</span>
          ${scores[i] != null ? `<span class="dt-fact-score">${scores[i].toFixed(3)}</span>` : ''}
        </div>
      `).join('');
    }

    // Output
    $('dt-detail-output').textContent = d.raw_output;

    // Provenance
    const provSection = $('dt-detail-provenance-section');
    const provBadge = $('dt-provenance-badge');
    if (d.signature) {
      provSection.style.display = '';
      provBadge.innerHTML = `
        <span class="prov-icon">🔏</span>
        <div class="prov-details">
          <span class="prov-label">Ed25519 Signed</span>
          <span class="prov-hash">sig: ${d.signature.substring(0, 32)}…</span>
          <span class="prov-hash">pub: ${(d.public_key || '').substring(0, 32)}…</span>
        </div>
      `;
    } else {
      provSection.style.display = 'none';
    }

    // Try replay
    const replaySection = $('dt-replay-section');
    const replayDeleted = $('dt-replay-deleted');
    try {
      const replay = await api(`/v1/decisions/${decisionId}/replay`);
      if (replay.facts_since_deleted && replay.facts_since_deleted.length > 0) {
        replaySection.style.display = '';
        replayDeleted.innerHTML = replay.facts_since_deleted.map(ref => {
          const fact = (replay.facts_used || []).find(f => f.ref === ref);
          return `
            <div class="dt-fact-item deleted">
              <span class="dt-fact-ref">#${ref}</span>
              <span class="dt-fact-content">${escapeHtml(fact?.content || '[Content no longer available]')}</span>
            </div>
          `;
        }).join('');
      } else {
        replaySection.style.display = 'none';
      }
    } catch {
      replaySection.style.display = 'none';
    }

    // Scroll to panel
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function exportDecisions() {
    window.open(`${API}/v1/decisions/export`, '_blank');
  }

  // ── Regulatory Reports ──────────────────────────────────────
  let rrReports = [];
  let rrCurrentId = null;

  async function loadReportStats() {
    try {
      const stats = await api('/v1/reports/stats');
      $('rr-stat-total').textContent = formatNumber(stats.total || 0);
      $('rr-stat-complete').textContent = formatNumber(stats.complete || 0);
      $('rr-stat-last').textContent = stats.last_report
        ? new Date(stats.last_report).toLocaleDateString('en-GB', { month: 'short', day: '2-digit', year: 'numeric' })
        : '—';
    } catch (e) { /* ignore */ }
  }

  async function generateReport(type) {
    const days = parseInt($('rr-period')?.value || '30', 10);
    try {
      toast(`Generating ${type} report…`, 'info');
      const r = await api('/v1/reports/generate', {
        method: 'POST',
        body: { report_type: type, period_days: days },
      });
      toast(`Report generated: ${r.title}`, 'success');
      loadReportStats();
      loadReportList();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadReportList() {
    try {
      const data = await api('/v1/reports/');
      rrReports = data.reports || [];
      renderReportList(rrReports, data.count);
    } catch (e) {
      $('rr-table-body').innerHTML = `<tr class="dt-empty-row"><td colspan="6">${e.message}</td></tr>`;
    }
  }

  function renderReportList(reports, count) {
    const tbody = $('rr-table-body');
    $('rr-count').textContent = `${count} report${count !== 1 ? 's' : ''}`;

    if (!reports.length) {
      tbody.innerHTML = '<tr class="dt-empty-row"><td colspan="6">No reports yet. Generate one above.</td></tr>';
      return;
    }

    const findingColors = {
      COMPLIANT: 'var(--success)', PARTIAL: 'var(--warning)',
      INSUFFICIENT_DATA: 'var(--text-muted)', FAILED: 'var(--danger)',
    };
    const typeLabels = {
      eu_ai_act: '🇪🇺 EU AI Act', gdpr: '🔒 GDPR',
      dora: '🏦 DORA', full_audit: '📊 Full Audit',
    };

    tbody.innerHTML = reports.map(r => {
      const date = new Date(r.created_at).toLocaleDateString('en-GB', { month: 'short', day: '2-digit' });
      const finding = r.overall_finding || r.status;
      const fc = findingColors[finding] || 'var(--text-muted)';
      const period = `${new Date(r.period_start).toLocaleDateString('en-GB', { month: 'short', day: '2-digit' })} → ${new Date(r.period_end).toLocaleDateString('en-GB', { month: 'short', day: '2-digit' })}`;
      const sizeKb = (r.file_size_bytes / 1024).toFixed(1);

      return `<tr data-rid="${r.report_id}">
        <td class="dt-time">${date}</td>
        <td>${typeLabels[r.report_type] || r.report_type}</td>
        <td><span class="dt-signed-badge" style="color:${fc};background:rgba(255,255,255,0.05)">${finding}</span></td>
        <td style="font-size:0.72rem">${period}</td>
        <td class="dt-facts-count">${sizeKb} KB</td>
        <td><button class="btn btn-sm dt-view-btn" data-rid="${r.report_id}">View</button></td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('tr[data-rid]').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('.dt-view-btn')) return;
        showReportDetail(row.dataset.rid);
      });
    });
    tbody.querySelectorAll('.dt-view-btn[data-rid]').forEach(btn => {
      btn.addEventListener('click', () => showReportDetail(btn.dataset.rid));
    });
  }

  async function showReportDetail(reportId) {
    rrCurrentId = reportId;
    const panel = $('rr-detail-panel');
    panel.style.display = '';

    let r;
    try { r = await api(`/v1/reports/${reportId}`); } catch { toast('Report not found', 'error'); return; }

    $('rr-detail-title').textContent = r.title;

    const findingColors = {
      COMPLIANT: 'var(--success)', PARTIAL: 'var(--warning)',
      INSUFFICIENT_DATA: 'var(--text-muted)',
    };
    const overall = r.content.overall_finding || r.status;
    const oc = findingColors[overall] || 'var(--text-muted)';

    $('rr-detail-meta').innerHTML = [
      `<span class="dt-meta-tag" style="color:${oc};font-weight:600"><span class="label">Finding</span> ${overall}</span>`,
      `<span class="dt-meta-tag"><span class="label">Framework</span> ${r.content.framework || r.report_type}</span>`,
      r.content.regulation ? `<span class="dt-meta-tag"><span class="label">Regulation</span> ${r.content.regulation}</span>` : '',
      `<span class="dt-meta-tag"><span class="label">Hash</span> ${(r.content_hash || '').substring(0, 16)}…</span>`,
    ].filter(Boolean).join('');

    // Render sections
    const sections = r.content.sections || {};
    const fwSections = r.content.frameworks;
    const container = $('rr-detail-sections');

    if (Object.keys(sections).length > 0) {
      container.innerHTML = renderReportSections(sections);
    } else if (fwSections) {
      // Full audit — render each framework
      let html = '';
      for (const [fwKey, fw] of Object.entries(fwSections)) {
        const fwFinding = fw.overall_finding || 'UNKNOWN';
        const ffc = findingColors[fwFinding] || 'var(--text-muted)';
        html += `<div style="margin-bottom:1.5rem"><h3 style="color:var(--text-secondary);margin-bottom:0.5rem">${fw.framework} <span class="dt-signed-badge" style="color:${ffc};background:rgba(255,255,255,0.05);font-size:0.72rem">${fwFinding}</span></h3>`;
        html += renderReportSections(fw.sections || {});
        html += `</div>`;
      }
      container.innerHTML = html;
    } else {
      container.innerHTML = `<pre class="dt-detail-code">${JSON.stringify(r.content, null, 2)}</pre>`;
    }

    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderReportSections(sections) {
    const findingColors = {
      COMPLIANT: 'var(--success)', PARTIAL: 'var(--warning)',
      INSUFFICIENT_DATA: 'var(--text-muted)',
    };
    const findingIcons = { COMPLIANT: '✅', PARTIAL: '⚠️', INSUFFICIENT_DATA: '❓' };

    return Object.entries(sections).map(([key, sec]) => {
      const f = sec.finding || 'UNKNOWN';
      const fc = findingColors[f] || 'var(--text-muted)';
      const fi = findingIcons[f] || '❓';
      const evidence = sec.compliance_evidence || {};

      const evidenceHtml = Object.entries(evidence).map(([ek, ev]) => {
        let val = ev;
        if (typeof ev === 'boolean') val = ev ? '✓ Yes' : '✕ No';
        else if (Array.isArray(ev)) val = ev.join(', ') || '—';
        else if (ev === null || ev === undefined) val = '—';
        return `<div style="display:flex;justify-content:space-between;padding:0.3rem 0;border-bottom:1px solid rgba(255,255,255,0.04)">
          <span style="color:var(--text-muted);font-size:0.75rem">${ek.replace(/_/g, ' ')}</span>
          <span style="font-size:0.75rem;text-align:right;max-width:60%">${val}</span>
        </div>`;
      }).join('');

      return `<div class="dt-detail-section" style="border-left:3px solid ${fc}">
        <h3 style="display:flex;align-items:center;gap:0.5rem">${fi} ${sec.title || key} <span class="dt-signed-badge" style="color:${fc};background:rgba(255,255,255,0.05);font-size:0.7rem">${f}</span></h3>
        <p style="color:var(--text-muted);font-size:0.78rem;margin:0.3rem 0 0.6rem">${sec.requirement || ''}</p>
        ${evidenceHtml}
      </div>`;
    }).join('');
  }

  // ── Governance Gateway ──────────────────────────────────────
  async function loadGovernanceStats() {
    try {
      const stats = await api('/v1/governance/stats');
      $('gov-stat-active').textContent = formatNumber(stats.policies_active || 0);
      $('gov-stat-evals').textContent = formatNumber(stats.evaluations_total || 0);
      $('gov-stat-denied').textContent = formatNumber(stats.evaluations_denied || 0);
      $('gov-stat-escalated').textContent = formatNumber(stats.evaluations_escalated || 0);
    } catch (e) { /* Governance not available */ }
  }

  async function loadGovernancePolicies() {
    try {
      const data = await api('/v1/governance/policies');
      renderGovernancePolicies(data.policies, data.count);
    } catch (e) {
      $('gov-table-body').innerHTML = `<tr class="dt-empty-row"><td colspan="6">${e.message}</td></tr>`;
    }
  }

  function renderGovernancePolicies(policies, count) {
    const tbody = $('gov-table-body');
    $('gov-count').textContent = `${count} polic${count !== 1 ? 'ies' : 'y'}`;

    if (!policies.length) {
      tbody.innerHTML = '<tr class="dt-empty-row"><td colspan="6">No policies. Create or seed defaults.</td></tr>';
      return;
    }

    const actionColors = { deny: 'var(--danger)', escalate: 'var(--warning)', log_only: 'var(--primary)', allow: 'var(--success)' };

    tbody.innerHTML = policies.map(p => `
      <tr>
        <td class="dt-facts-count">${p.priority}</td>
        <td><strong>${escapeHtml(p.name)}</strong><br><span class="dim" style="font-size:0.72rem">${escapeHtml(p.description).substring(0, 60)}</span></td>
        <td class="dt-model">${p.policy_type}</td>
        <td><span class="dt-signed-badge" style="color:${actionColors[p.action] || 'var(--text-muted)'};background:rgba(255,255,255,0.05)">${p.action}</span></td>
        <td><span class="dt-signed-badge ${p.enabled ? 'signed' : 'unsigned'}" style="cursor:pointer" data-toggle-id="${p.policy_id}" data-enabled="${p.enabled}">${p.enabled ? '✓ ON' : '✕ OFF'}</span></td>
        <td><button class="btn btn-sm" style="color:var(--danger);font-size:0.72rem" data-del-id="${p.policy_id}">✕</button></td>
      </tr>
    `).join('');

    // Toggle enable/disable
    tbody.querySelectorAll('[data-toggle-id]').forEach(el => {
      el.addEventListener('click', async () => {
        const id = el.dataset.toggleId;
        const nowEnabled = el.dataset.enabled === 'true';
        await api(`/v1/governance/policies/${id}`, {
          method: 'PUT', body: { enabled: !nowEnabled },
        });
        toast(nowEnabled ? 'Policy disabled' : 'Policy enabled', 'success');
        loadGovernancePolicies();
        loadGovernanceStats();
      });
    });

    // Delete
    tbody.querySelectorAll('[data-del-id]').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Delete this policy?')) return;
        await api(`/v1/governance/policies/${btn.dataset.delId}`, { method: 'DELETE' });
        toast('Policy deleted', 'success');
        loadGovernancePolicies();
        loadGovernanceStats();
      });
    });
  }

  async function createGovernancePolicy() {
    const name = $('gov-name')?.value?.trim();
    if (!name) { toast('Policy name required', 'error'); return; }

    let config = {};
    const cfgStr = $('gov-config')?.value?.trim();
    if (cfgStr) {
      try { config = JSON.parse(cfgStr); }
      catch { toast('Invalid JSON in config', 'error'); return; }
    }

    try {
      await api('/v1/governance/policies', {
        method: 'POST',
        body: {
          name,
          description: '',
          policy_type: $('gov-type').value,
          action: $('gov-action').value,
          config,
        },
      });
      toast('Policy created!', 'success');
      $('gov-name').value = '';
      $('gov-config').value = '';
      loadGovernancePolicies();
      loadGovernanceStats();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function seedGovernanceDefaults() {
    try {
      const r = await api('/v1/governance/seed-defaults', { method: 'POST' });
      toast(`Seeded ${r.seeded} default policies`, 'success');
      loadGovernancePolicies();
      loadGovernanceStats();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function testGovernanceEval() {
    let ctx = {};
    const ctxStr = $('gov-test-ctx')?.value?.trim();
    if (ctxStr) {
      try { ctx = JSON.parse(ctxStr); }
      catch { toast('Invalid JSON context', 'error'); return; }
    }

    try {
      const result = await api('/v1/governance/evaluate', {
        method: 'POST',
        body: { operation: $('gov-test-op').value, context: ctx },
      });

      $('gov-test-results').style.display = '';
      const s = result.summary;
      $('gov-test-summary').innerHTML = [
        `<span class="dt-meta-tag" style="background:${result.allowed ? 'var(--success-glow)' : 'var(--danger-glow)'}"><span class="label">Verdict</span> ${result.allowed ? '✅ ALLOWED' : '❌ BLOCKED'}</span>`,
        `<span class="dt-meta-tag"><span class="label">Allowed</span> ${s.allowed}</span>`,
        `<span class="dt-meta-tag" style="color:var(--danger)"><span class="label">Denied</span> ${s.denied}</span>`,
        `<span class="dt-meta-tag" style="color:var(--warning)"><span class="label">Escalated</span> ${s.escalated}</span>`,
        `<span class="dt-meta-tag"><span class="label">Logged</span> ${s.logged}</span>`,
      ].join('');

      const resultColors = { allowed: 'var(--success)', denied: 'var(--danger)', escalated: 'var(--warning)', logged: 'var(--primary)' };
      $('gov-test-details').innerHTML = result.evaluations.map(e => `
        <div class="dt-fact-item" style="border-left-color:${resultColors[e.result] || 'var(--border)'}">
          <span class="dt-fact-ref" style="color:${resultColors[e.result]}">${e.result.toUpperCase()}</span>
          <span class="dt-fact-content"><strong>${escapeHtml(e.policy_name)}</strong><br>${escapeHtml(e.detail)}</span>
        </div>
      `).join('');

      loadGovernanceStats();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadGovernanceLogs() {
    try {
      const data = await api('/v1/governance/logs?limit=20');
      const tbody = $('gov-log-body');
      if (!data.logs.length) {
        tbody.innerHTML = '<tr class="dt-empty-row"><td colspan="5">No logs yet.</td></tr>';
        return;
      }

      const resultColors = { allowed: 'var(--success)', denied: 'var(--danger)', escalated: 'var(--warning)', logged: 'var(--primary)' };
      tbody.innerHTML = data.logs.map(l => {
        const time = new Date(l.created_at).toLocaleString('en-GB', {
          month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        return `<tr>
          <td class="dt-time">${time}</td>
          <td>${escapeHtml(l.policy_name)}</td>
          <td class="dt-model">${l.operation}</td>
          <td><span class="dt-signed-badge" style="color:${resultColors[l.result] || 'var(--text-muted)'};background:rgba(255,255,255,0.05)">${l.result}</span></td>
          <td style="font-size:0.75rem;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(l.detail)}">${escapeHtml(l.detail)}</td>
        </tr>`;
      }).join('');
    } catch (e) { /* ignore */ }
  }

  // ── Erasure Proof ──────────────────────────────────────────
  let epCerts = [];

  async function loadErasureStats() {
    try {
      const stats = await api('/v1/erasure/stats');
      $('ep-stat-total').textContent = formatNumber(stats.total || 0);
      $('ep-stat-scrubbed').textContent = formatNumber(stats.total_scrubbed || 0);
      $('ep-stat-signed').textContent = formatNumber(stats.signed_count || 0);
    } catch (e) {
      // Erasure Proof not available
    }
  }

  async function loadErasureCerts() {
    try {
      const data = await api('/v1/erasure/');
      epCerts = data.certificates || [];
      renderErasureTable(epCerts, data.count);
    } catch (e) {
      $('ep-table-body').innerHTML = `<tr class="dt-empty-row"><td colspan="7">Unable to load certificates: ${e.message}</td></tr>`;
    }
  }

  function renderErasureTable(certs, count) {
    const tbody = $('ep-table-body');
    $('ep-count').textContent = `${count} certificate${count !== 1 ? 's' : ''}`;

    if (!certs.length) {
      tbody.innerHTML = '<tr class="dt-empty-row"><td colspan="7">No erasure certificates yet.</td></tr>';
      return;
    }

    tbody.innerHTML = certs.map(c => {
      const time = new Date(c.erasure_completed_at).toLocaleString('en-GB', {
        month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
      });
      const hash = c.fact_content_hash ? c.fact_content_hash.substring(0, 12) + '…' : '—';
      const signed = c.verified;

      return `<tr data-cert-id="${c.certificate_id}">
        <td class="dt-time">${time}</td>
        <td class="dt-facts-count">#${c.fact_ref}</td>
        <td class="dt-model" style="font-size:0.72rem">${hash}</td>
        <td class="dt-facts-count">${c.decision_records_scrubbed}</td>
        <td><span class="dt-signed-badge ${signed ? 'signed' : 'unsigned'}">${signed ? '🔏 Signed' : '—'}</span></td>
        <td style="font-size:0.75rem">${escapeHtml(c.legal_basis || '').substring(0, 30)}</td>
        <td><button class="btn btn-sm dt-view-btn" data-cert-id="${c.certificate_id}">View</button></td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('tr[data-cert-id]').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('.dt-view-btn')) return;
        showCertDetail(row.dataset.certId);
      });
    });
    tbody.querySelectorAll('.dt-view-btn[data-cert-id]').forEach(btn => {
      btn.addEventListener('click', () => showCertDetail(btn.dataset.certId));
    });
  }

  async function issueCertificate() {
    const factRef = parseInt($('ep-fact-ref')?.value, 10);
    if (isNaN(factRef)) {
      toast('Please enter a valid Fact Ref number.', 'error');
      return;
    }

    const content = $('ep-fact-content')?.value?.trim() || null;
    const requestedBy = $('ep-requested-by')?.value || 'data_subject';

    try {
      const cert = await api('/v1/erasure/issue', {
        method: 'POST',
        body: {
          fact_ref: factRef,
          fact_content: content,
          requested_by: requestedBy,
        },
      });
      toast(`Erasure certificate issued: ${cert.certificate_id.substring(0, 12)}…`, 'success');
      $('ep-fact-ref').value = '';
      $('ep-fact-content').value = '';
      loadErasureStats();
      loadErasureCerts();
    } catch (e) {
      toast(`Failed to issue certificate: ${e.message}`, 'error');
    }
  }

  async function showCertDetail(certId) {
    const panel = $('ep-detail-panel');
    panel.style.display = '';

    let c = epCerts.find(x => x.certificate_id === certId);
    if (!c) {
      try { c = await api(`/v1/erasure/${certId}`); } catch { toast('Certificate not found', 'error'); return; }
    }

    const meta = $('ep-detail-meta');
    const time = new Date(c.erasure_completed_at).toLocaleString('en-GB');
    meta.innerHTML = [
      `<span class="dt-meta-tag"><span class="label">ID</span> ${c.certificate_id.substring(0, 16)}…</span>`,
      `<span class="dt-meta-tag"><span class="label">Issued</span> ${time}</span>`,
      `<span class="dt-meta-tag"><span class="label">Fact</span> #${c.fact_ref}</span>`,
      `<span class="dt-meta-tag"><span class="label">Scrubbed</span> ${c.decision_records_scrubbed} decisions</span>`,
      c.requested_by ? `<span class="dt-meta-tag"><span class="label">By</span> ${c.requested_by}</span>` : '',
      c.fact_content_hash ? `<span class="dt-meta-tag"><span class="label">Hash</span> ${c.fact_content_hash}</span>` : '',
    ].filter(Boolean).join('');

    // Verify signature
    const badge = $('ep-verify-badge');
    try {
      const result = await api(`/v1/erasure/${certId}/verify`);
      if (result.valid) {
        badge.innerHTML = `
          <span class="prov-icon">✅</span>
          <div class="prov-details">
            <span class="prov-label">Signature Verified</span>
            <span class="prov-hash">${result.detail}</span>
          </div>`;
        badge.style.borderColor = 'rgba(16, 185, 129, 0.3)';
      } else {
        badge.innerHTML = `
          <span class="prov-icon">❌</span>
          <div class="prov-details">
            <span class="prov-label" style="color:var(--danger)">Verification Failed</span>
            <span class="prov-hash">${result.detail}</span>
          </div>`;
        badge.style.borderColor = 'rgba(239, 68, 68, 0.3)';
      }
    } catch {
      badge.innerHTML = '<span class="prov-hash">Verification unavailable</span>';
    }

    $('ep-scrubbed-ids').textContent = (c.scrubbed_decision_ids || []).length > 0
      ? c.scrubbed_decision_ids.join('\n')
      : 'No decision records were affected.';

    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Init ────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    // Auth tabs
    $('tab-login').addEventListener('click', () => {
      $('tab-login').classList.add('active');
      $('tab-signup').classList.remove('active');
      $('login-form').style.display = '';
      $('signup-form').style.display = 'none';
      $('auth-error').className = 'error-msg';
    });

    $('tab-signup').addEventListener('click', () => {
      $('tab-signup').classList.add('active');
      $('tab-login').classList.remove('active');
      $('signup-form').style.display = '';
      $('login-form').style.display = 'none';
      $('auth-error').className = 'error-msg';
    });

    // Login form
    $('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('login-btn');
      btn.disabled = true;
      btn.textContent = 'Logging in...';
      try {
        await login(
          $('login-email').value,
          $('login-password').value,
        );
        $('auth-error').className = 'error-msg';
      } catch (err) {
        $('auth-error').textContent = err.message;
        $('auth-error').className = 'error-msg visible';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Login';
      }
    });

    // Signup form
    $('signup-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('signup-btn');
      btn.disabled = true;
      btn.textContent = 'Creating...';
      try {
        await signup(
          $('signup-name').value,
          $('signup-email').value,
          $('signup-password').value,
          $('signup-plan').value,
        );
        $('auth-error').className = 'error-msg';
      } catch (err) {
        $('auth-error').textContent = err.message;
        $('auth-error').className = 'error-msg visible';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Create Account';
      }
    });

    // Sidebar navigation — with Decision Trail loader
    document.querySelectorAll('.nav-item[data-section]').forEach(nav => {
      nav.addEventListener('click', () => {
        showSection(nav.dataset.section);
        if (nav.dataset.section === 'decisions') {
          loadDecisionStats();
          loadDecisions();
        }
        if (nav.dataset.section === 'erasure') {
          loadErasureStats();
          loadErasureCerts();
        }
        if (nav.dataset.section === 'reports') {
          loadReportStats();
          loadReportList();
        }
        if (nav.dataset.section === 'governance') {
          loadGovernanceStats();
          loadGovernancePolicies();
          loadGovernanceLogs();
        }
      });
    });

    // Logout
    $('logout-btn').addEventListener('click', logout);

    // API Key actions
    $('reveal-key-btn').addEventListener('click', revealKey);
    $('copy-key-btn').addEventListener('click', copyKey);
    $('rotate-key-btn').addEventListener('click', rotateKey);

    // Billing upgrade
    $('btn-starter')?.addEventListener('click', () => upgradePlan('starter'));
    $('btn-pro')?.addEventListener('click', () => upgradePlan('pro'));

    // Badge copy
    $('copy-badge-md').addEventListener('click', copyBadgeMd);
    $('copy-badge-html').addEventListener('click', copyBadgeHtml);

    // Mobile menu
    $('mobile-menu-btn').addEventListener('click', () => {
      $('sidebar').classList.toggle('open');
    });

    // Decision Trail events
    $('dt-filter-btn')?.addEventListener('click', () => { dtPage = 0; loadDecisions(); });
    $('dt-clear-btn')?.addEventListener('click', () => {
      $('dt-filter-model').value = '';
      $('dt-filter-store').value = '';
      $('dt-filter-session').value = '';
      dtPage = 0;
      loadDecisions();
    });
    $('dt-export-btn')?.addEventListener('click', exportDecisions);
    $('dt-prev-btn')?.addEventListener('click', () => { if (dtPage > 0) { dtPage--; loadDecisions(); } });
    $('dt-next-btn')?.addEventListener('click', () => { dtPage++; loadDecisions(); });
    $('dt-close-detail')?.addEventListener('click', () => { $('dt-detail-panel').style.display = 'none'; });

    // Erasure Proof events
    $('ep-issue-btn')?.addEventListener('click', issueCertificate);
    $('ep-close-detail')?.addEventListener('click', () => { $('ep-detail-panel').style.display = 'none'; });

    // Governance Gateway events
    $('gov-create-btn')?.addEventListener('click', createGovernancePolicy);
    $('gov-seed-btn')?.addEventListener('click', seedGovernanceDefaults);
    $('gov-test-btn')?.addEventListener('click', testGovernanceEval);
    $('gov-refresh-logs')?.addEventListener('click', loadGovernanceLogs);

    // Regulatory Reports events
    document.querySelectorAll('[data-rr-gen]').forEach(btn => {
      btn.addEventListener('click', () => generateReport(btn.dataset.rrGen));
    });
    $('rr-close-detail')?.addEventListener('click', () => { $('rr-detail-panel').style.display = 'none'; });
    $('rr-download-btn')?.addEventListener('click', () => {
      if (rrCurrentId) window.open(`${API}/v1/reports/${rrCurrentId}/download`, '_blank');
    });

    // PDF download button
    $('rr-download-pdf-btn')?.addEventListener('click', () => {
      if (rrCurrentId) window.open(`${API}/v1/reports/${rrCurrentId}/download/pdf`, '_blank');
    });

    // ---- Webhooks Section ----
    async function loadWebhooks() {
      try {
        const data = await api('/v1/webhooks/');
        const hooks = data.webhooks || [];
        $('wh-count').textContent = `${hooks.length} webhook${hooks.length !== 1 ? 's' : ''}`;
        const body = $('wh-table-body');
        if (!hooks.length) {
          body.innerHTML = '<tr class="dt-empty-row"><td colspan="4">No webhooks registered yet. Create one above.</td></tr>';
          return;
        }
        body.innerHTML = hooks.map(h => `
          <tr>
            <td style="font-family:var(--font-mono);font-size:0.8rem;max-width:250px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(h.url)}</td>
            <td style="font-size:0.8rem">${(h.events||[]).map(e => `<span class="tag tag-sm">${e}</span>`).join(' ')}</td>
            <td>${h.enabled ? '<span class="status-badge success">Active</span>' : '<span class="status-badge">Disabled</span>'}</td>
            <td>
              <button class="btn btn-sm" onclick="window._whDeliveries('${h.webhook_id}')">📋 Log</button>
              <button class="btn btn-sm" onclick="window._whTest('${h.webhook_id}')">🔔 Test</button>
              <button class="btn btn-sm btn-danger" onclick="window._whDelete('${h.webhook_id}')">✕</button>
            </td>
          </tr>
        `).join('');
      } catch (e) { console.error('loadWebhooks:', e); }
    }

    window._whDeliveries = async function(whId) {
      try {
        const data = await api(`/v1/webhooks/${whId}/deliveries`);
        const deliveries = data.deliveries || [];
        const panel = $('wh-delivery-panel');
        panel.style.display = '';
        const body = $('wh-delivery-body');
        if (!deliveries.length) {
          body.innerHTML = '<tr class="dt-empty-row"><td colspan="5">No deliveries yet.</td></tr>';
          return;
        }
        body.innerHTML = deliveries.map(d => {
          const statusColor = d.status === 'delivered' ? 'success' : d.status === 'failed' ? 'danger' : 'warning';
          return `<tr>
            <td>${new Date(d.created_at).toLocaleString()}</td>
            <td><span class="tag tag-sm">${d.event_type}</span></td>
            <td><span class="status-badge ${statusColor}">${d.status}</span></td>
            <td>${d.response_code || '—'}</td>
            <td>${d.attempts}</td>
          </tr>`;
        }).join('');
      } catch (e) { console.error('deliveries:', e); }
    };

    window._whTest = async function(whId) {
      try {
        await api(`/v1/webhooks/${whId}/test`, { method: 'POST' });
        toast('Test event sent!', 'success');
        setTimeout(() => window._whDeliveries(whId), 1500);
      } catch (e) { toast('Test failed: ' + e.message, 'error'); }
    };

    window._whDelete = async function(whId) {
      if (!confirm('Delete this webhook?')) return;
      try {
        await api(`/v1/webhooks/${whId}`, { method: 'DELETE' });
        toast('Webhook deleted', 'success');
        loadWebhooks();
      } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
    };

    $('wh-register-btn')?.addEventListener('click', async () => {
      const url = $('wh-url').value.trim();
      const desc = $('wh-desc').value.trim();
      const events = Array.from(document.querySelectorAll('.wh-event:checked')).map(c => c.value);
      if (!url) { toast('URL is required', 'error'); return; }
      if (!events.length) { toast('Select at least one event', 'error'); return; }
      try {
        const result = await api('/v1/webhooks/', {
          method: 'POST',
          body: JSON.stringify({ url, events, description: desc }),
        });
        if (result.secret) {
          $('wh-secret-value').textContent = result.secret;
          $('wh-secret-display').style.display = '';
        }
        toast('Webhook registered!', 'success');
        $('wh-url').value = '';
        $('wh-desc').value = '';
        loadWebhooks();
      } catch (e) { toast('Registration failed: ' + e.message, 'error'); }
    });

    $('wh-close-deliveries')?.addEventListener('click', () => {
      $('wh-delivery-panel').style.display = 'none';
    });

    // Auto-load webhooks when section shown
    const navWh = document.getElementById('nav-webhooks');
    if (navWh) {
      navWh.addEventListener('click', () => loadWebhooks());
    }

    // Check for upgrade success
    if (window.location.search.includes('upgraded=true')) {
      toast('Plan upgraded successfully! 🎉', 'success');
      window.history.replaceState({}, '', '/portal');
    }

    // Init: check token
    if (token) {
      showDashboard();
      loadDashboard();
    } else {
      showAuth();
    }
  });
})();

// ==================== ORCHESTRATOR ====================

(function orchestratorModule() {
  const BASE = '';

  function apiKey() {
    return localStorage.getItem('grafomem_api_key') || '';
  }

  function authHeaders() {
    const token = localStorage.getItem('gfm_token');
    return {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
  }

  // ---- Sub-tab switching ----
  document.querySelectorAll('.orch-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.orch-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.orchTab;
      document.querySelectorAll('.orch-panel').forEach(p => p.style.display = 'none');
      const panel = document.getElementById(`orch-panel-${tab}`);
      if (panel) panel.style.display = '';
    });
  });

  // ---- Load stats ----
  async function loadOrchStats() {
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/stats`, { headers: authHeaders() });
      if (!res.ok) return;
      const d = await res.json();
      document.getElementById('orch-stat-agents').textContent = d.agents_active || 0;
      document.getElementById('orch-stat-workflows').textContent = d.workflows_total || 0;
      document.getElementById('orch-stat-steps').textContent = d.steps_total || 0;
      document.getElementById('orch-stat-tokens').textContent = (d.total_tokens || 0).toLocaleString();
    } catch (e) { console.warn('Orch stats:', e); }
  }

  // ---- Agents ----
  async function loadAgents() {
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/agents`, { headers: authHeaders() });
      if (!res.ok) return;
      const d = await res.json();
      const list = document.getElementById('agents-list');
      if (!d.agents || d.agents.length === 0) {
        list.innerHTML = '<p class="empty-state">No agents defined yet. Click "+ New Agent" to create one.</p>';
        return;
      }
      list.innerHTML = d.agents.map(a => `
        <div class="item-card">
          <div class="item-info">
            <div class="item-name">${a.name} <span class="provider-badge">${a.role}</span></div>
            <div class="item-meta">Model: ${a.model_id} · Max steps: ${a.max_steps} · Temp: ${a.temperature}</div>
          </div>
          <div class="item-actions">
            <button class="btn btn-sm" onclick="testAgent('${a.agent_id}')">Test</button>
            <button class="btn btn-sm btn-danger" onclick="deleteAgent('${a.agent_id}')">Delete</button>
          </div>
        </div>
      `).join('');
    } catch (e) { console.warn('Load agents:', e); }
  }

  // Create agent
  const btnCreateAgent = document.getElementById('btn-create-agent');
  const createAgentForm = document.getElementById('create-agent-form');
  const btnCancelAgent = document.getElementById('btn-cancel-agent');
  const btnSaveAgent = document.getElementById('btn-save-agent');

  if (btnCreateAgent) btnCreateAgent.addEventListener('click', () => {
    createAgentForm.style.display = '';
  });
  if (btnCancelAgent) btnCancelAgent.addEventListener('click', () => {
    createAgentForm.style.display = 'none';
  });
  if (btnSaveAgent) btnSaveAgent.addEventListener('click', async () => {
    const stores = document.getElementById('agent-stores').value.split(',').map(s => s.trim()).filter(Boolean);
    const tools = document.getElementById('agent-tools').value.split(',').map(s => s.trim()).filter(Boolean);
    const body = {
      name: document.getElementById('agent-name').value,
      role: document.getElementById('agent-role').value,
      model_id: document.getElementById('agent-model').value,
      system_prompt: document.getElementById('agent-prompt').value,
      temperature: parseFloat(document.getElementById('agent-temp').value) || 0.7,
      memory_stores: stores,
      tools: tools,
    };
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/agents`, {
        method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
      });
      if (res.ok) {
        createAgentForm.style.display = 'none';
        loadAgents();
        loadOrchStats();
        showToast('Agent created successfully');
      } else {
        const err = await res.json();
        showToast(err.detail || 'Failed to create agent', true);
      }
    } catch (e) { showToast('Failed to create agent', true); }
  });

  // Delete agent
  window.deleteAgent = async function(id) {
    if (!confirm('Delete this agent?')) return;
    try {
      await fetch(`${BASE}/v1/orchestrator/agents/${id}`, { method: 'DELETE', headers: authHeaders() });
      loadAgents();
      loadOrchStats();
      showToast('Agent deleted');
    } catch (e) { showToast('Failed to delete', true); }
  };

  // Test agent (ad-hoc step)
  window.testAgent = async function(id) {
    const input = prompt('Enter test input for the agent:');
    if (!input) return;
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/step`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ agent_id: id, input_text: input }),
      });
      const data = await res.json();
      if (res.ok) {
        alert(`Status: ${data.status}\nOutput: ${data.raw_output || '(none)'}\nTokens: ${data.tokens_used}`);
      } else {
        alert(`Error: ${data.detail || JSON.stringify(data)}`);
      }
    } catch (e) { alert('Test failed: ' + e.message); }
  };

  // ---- Workflows ----
  async function loadWorkflows() {
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/workflows`, { headers: authHeaders() });
      if (!res.ok) return;
      const d = await res.json();
      const list = document.getElementById('workflows-list');
      if (!d.workflows || d.workflows.length === 0) {
        list.innerHTML = '<p class="empty-state">No workflows yet. Click "+ New Workflow" to create one.</p>';
        return;
      }
      list.innerHTML = d.workflows.map(w => `
        <div class="item-card" onclick="viewWorkflow('${w.workflow_id}')" style="cursor:pointer">
          <div class="item-info">
            <div class="item-name">${w.name} <span class="status-badge ${w.status}">${w.status}</span></div>
            <div class="item-meta">Mode: ${w.mode} · Steps: ${w.current_step}/${w.max_total_steps} · Tokens: ${(w.total_tokens||0).toLocaleString()}</div>
          </div>
          <div class="item-actions">
            <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); terminateWorkflow('${w.workflow_id}')">Terminate</button>
          </div>
        </div>
      `).join('');
    } catch (e) { console.warn('Load workflows:', e); }
  }

  // Create workflow
  const btnCreateWf = document.getElementById('btn-create-workflow');
  const createWfForm = document.getElementById('create-workflow-form');
  const btnCancelWf = document.getElementById('btn-cancel-workflow');
  const btnSaveWf = document.getElementById('btn-save-workflow');

  if (btnCreateWf) btnCreateWf.addEventListener('click', () => { createWfForm.style.display = ''; });
  if (btnCancelWf) btnCancelWf.addEventListener('click', () => { createWfForm.style.display = 'none'; });
  if (btnSaveWf) btnSaveWf.addEventListener('click', async () => {
    const agentIds = document.getElementById('wf-agents').value.split(',').map(s => s.trim()).filter(Boolean);
    const body = {
      name: document.getElementById('wf-name').value,
      mode: document.getElementById('wf-mode').value,
      agent_ids: agentIds,
      description: document.getElementById('wf-desc').value,
    };
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/workflows`, {
        method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
      });
      if (res.ok) {
        createWfForm.style.display = 'none';
        loadWorkflows();
        loadOrchStats();
        showToast('Workflow created');
      } else {
        const err = await res.json();
        showToast(err.detail || 'Failed', true);
      }
    } catch (e) { showToast('Failed to create workflow', true); }
  });

  // View workflow detail
  window.viewWorkflow = async function(id) {
    try {
      const res = await fetch(`${BASE}/v1/orchestrator/workflows/${id}`, { headers: authHeaders() });
      if (!res.ok) return;
      const w = await res.json();
      document.getElementById('workflow-detail').style.display = '';
      document.getElementById('wf-detail-name').textContent = w.name;
      const badge = document.getElementById('wf-detail-status');
      badge.textContent = w.status;
      badge.className = `status-badge ${w.status}`;

      // Render step timeline
      const timeline = document.getElementById('step-timeline');
      if (!w.steps || w.steps.length === 0) {
        timeline.innerHTML = '<p class="empty-state">No steps executed yet. Click "Execute" to run.</p>';
      } else {
        timeline.innerHTML = w.steps.map(s => `
          <div class="step-card ${s.status}" data-step="${s.step_number}">
            <div class="step-header">
              <span class="step-agent">${s.agent_id.substring(0,8)}…</span>
              <span class="step-meta">${s.tokens_used} tokens · ${s.latency_ms}ms</span>
            </div>
            <div class="step-gov">
              ${s.governance_allowed
                ? '<span class="gov-allowed">✓ Governance: Allowed</span>'
                : '<span class="gov-denied">✘ Governance: ' + s.status + '</span>'
              }
            </div>
            ${s.raw_output ? `<div class="step-output">${escapeHtml(s.raw_output.substring(0, 500))}</div>` : ''}
          </div>
        `).join('');
      }

      // Wire run button — SSE streaming
      document.getElementById('btn-run-workflow').onclick = async () => {
        const input = document.getElementById('wf-run-input').value;
        if (!input) { showToast('Enter input text', true); return; }

        const timeline = document.getElementById('step-timeline');
        let totalTokens = 0;
        let currentStepEl = null;
        const stepCards = {};

        // Show live status bar
        timeline.innerHTML = `
          <div class="stream-status-bar" id="stream-bar">
            <span class="stream-dot live"></span>
            <span class="stream-label">Executing workflow…</span>
            <span class="stream-tokens" id="stream-tokens">0 tokens</span>
            <span class="stream-elapsed" id="stream-elapsed">0.0s</span>
          </div>
        `;

        const startTime = Date.now();
        const elapsedTimer = setInterval(() => {
          const el = document.getElementById('stream-elapsed');
          if (el) el.textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
        }, 100);

        try {
          const res = await fetch(`${BASE}/v1/orchestrator/workflows/${id}/stream`, {
            method: 'POST',
            headers: { ...authHeaders(), 'Accept': 'text/event-stream' },
            body: JSON.stringify({ input_text: input }),
          });

          if (!res.ok) {
            clearInterval(elapsedTimer);
            const err = await res.json();
            showToast(err.detail || 'Execution failed', true);
            return;
          }

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            let eventType = null;
            let eventData = null;

            for (const line of lines) {
              if (line.startsWith('event: ')) {
                eventType = line.substring(7).trim();
              } else if (line.startsWith('data: ')) {
                try {
                  eventData = JSON.parse(line.substring(6));
                } catch (e) { continue; }

                if (!eventType || !eventData) continue;

                // ── Handle events ──
                const stepIdx = eventData.step_index;
                const agentName = eventData.agent_name || '';

                if (eventType === 'step.started') {
                  const card = document.createElement('div');
                  card.className = 'step-card-live running';
                  card.setAttribute('data-step', stepIdx != null ? stepIdx : '?');
                  card.innerHTML = `
                    <div class="step-live-header">
                      <div>
                        <span class="step-live-agent">${escapeHtml(agentName)}</span>
                        <span class="step-live-role">${escapeHtml(eventData.agent_role || '')}</span>
                      </div>
                      <span class="step-live-meta" id="step-meta-${stepIdx}"></span>
                    </div>
                    <div class="step-stages" id="step-stages-${stepIdx}">
                      <span class="stage-pill active" id="stage-gov-${stepIdx}"><span class="stage-icon">🛡️</span> Governance</span>
                      <span class="stage-pill" id="stage-mem-${stepIdx}"><span class="stage-icon">🧠</span> Memory</span>
                      <span class="stage-pill" id="stage-llm-${stepIdx}"><span class="stage-icon">🤖</span> LLM</span>
                      <span class="stage-pill" id="stage-done-${stepIdx}"><span class="stage-icon">✓</span> Done</span>
                    </div>
                  `;
                  timeline.appendChild(card);
                  stepCards[stepIdx] = card;
                  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }

                else if (eventType === 'step.governance_pass') {
                  const pill = document.getElementById(`stage-gov-${stepIdx}`);
                  if (pill) { pill.className = 'stage-pill done'; pill.querySelector('.stage-icon').textContent = '✓'; }
                  const memPill = document.getElementById(`stage-mem-${stepIdx}`);
                  if (memPill) memPill.className = 'stage-pill active';
                }

                else if (eventType === 'step.governance_deny') {
                  const pill = document.getElementById(`stage-gov-${stepIdx}`);
                  if (pill) { pill.className = 'stage-pill fail'; pill.querySelector('.stage-icon').textContent = '✘'; }
                  if (stepCards[stepIdx]) stepCards[stepIdx].className = 'step-card-live denied';
                }

                else if (eventType === 'step.memory_retrieve') {
                  const memPill = document.getElementById(`stage-mem-${stepIdx}`);
                  if (memPill) {
                    memPill.className = 'stage-pill done';
                    memPill.querySelector('.stage-icon').textContent = '✓';
                    memPill.textContent = '';
                    memPill.innerHTML = `<span class="stage-icon">✓</span> ${eventData.facts_found} facts`;
                  }
                  const llmPill = document.getElementById(`stage-llm-${stepIdx}`);
                  if (llmPill) llmPill.className = 'stage-pill active';
                }

                else if (eventType === 'step.llm_start') {
                  const llmPill = document.getElementById(`stage-llm-${stepIdx}`);
                  if (llmPill) {
                    llmPill.className = 'stage-pill active';
                    llmPill.innerHTML = `<span class="stage-icon">⏳</span> Inferring…`;
                  }
                }

                else if (eventType === 'step.llm_complete') {
                  const llmPill = document.getElementById(`stage-llm-${stepIdx}`);
                  if (llmPill) {
                    llmPill.className = 'stage-pill done';
                    llmPill.innerHTML = `<span class="stage-icon">✓</span> ${eventData.tokens_used} tok · ${eventData.latency_ms}ms`;
                  }
                  totalTokens += (eventData.tokens_used || 0);
                  const tokEl = document.getElementById('stream-tokens');
                  if (tokEl) tokEl.textContent = totalTokens + ' tokens';

                  // Show output preview
                  if (eventData.output_preview && stepCards[stepIdx]) {
                    const existing = stepCards[stepIdx].querySelector('.step-live-output');
                    if (!existing) {
                      const outDiv = document.createElement('div');
                      outDiv.className = 'step-live-output';
                      outDiv.textContent = eventData.output_preview;
                      stepCards[stepIdx].appendChild(outDiv);
                    }
                  }
                }

                else if (eventType === 'step.tool_call') {
                  // Add tool pill if not exists
                  const stages = document.getElementById(`step-stages-${stepIdx}`);
                  if (stages) {
                    const toolPill = document.createElement('span');
                    toolPill.className = 'stage-pill done';
                    toolPill.innerHTML = `<span class="stage-icon">🔧</span> ${escapeHtml(eventData.tool_name || 'tool')}`;
                    const donePill = document.getElementById(`stage-done-${stepIdx}`);
                    if (donePill) stages.insertBefore(toolPill, donePill);
                  }
                }

                else if (eventType === 'step.complete') {
                  const donePill = document.getElementById(`stage-done-${stepIdx}`);
                  if (donePill) { donePill.className = 'stage-pill done'; }
                  if (stepCards[stepIdx]) stepCards[stepIdx].className = 'step-card-live completed';

                  // Update meta
                  const meta = document.getElementById(`step-meta-${stepIdx}`);
                  if (meta) meta.textContent = `${eventData.tokens_used || 0} tokens · ${eventData.latency_ms || 0}ms`;
                }

                else if (eventType === 'workflow.complete') {
                  clearInterval(elapsedTimer);
                  const bar = document.getElementById('stream-bar');
                  if (bar) {
                    bar.querySelector('.stream-dot').className = 'stream-dot';
                    bar.querySelector('.stream-label').textContent = 'Workflow completed';
                  }

                  const banner = document.createElement('div');
                  banner.className = 'stream-complete-banner';
                  banner.innerHTML = `
                    <span class="stream-complete-icon">✅</span>
                    <span class="stream-complete-text">Workflow ${escapeHtml(eventData.status || 'completed')}</span>
                    <span class="stream-complete-stats">${eventData.total_steps || 0} steps · ${eventData.total_tokens || 0} tokens · ${eventData.duration_ms || 0}ms</span>
                  `;
                  timeline.appendChild(banner);
                  loadOrchStats();
                }

                else if (eventType === 'workflow.error') {
                  clearInterval(elapsedTimer);
                  const bar = document.getElementById('stream-bar');
                  if (bar) {
                    bar.querySelector('.stream-dot').className = 'stream-dot error';
                    bar.querySelector('.stream-label').textContent = 'Workflow failed';
                  }

                  const banner = document.createElement('div');
                  banner.className = 'stream-complete-banner error';
                  banner.innerHTML = `
                    <span class="stream-complete-icon">❌</span>
                    <span class="stream-complete-text">Error: ${escapeHtml(eventData.error || 'Unknown')}</span>
                  `;
                  timeline.appendChild(banner);
                }

                eventType = null;
                eventData = null;
              }
            }
          }
        } catch (e) {
          clearInterval(elapsedTimer);
          showToast('Streaming failed: ' + e.message, true);
        }
      };
    } catch (e) { showToast('Failed to load workflow', true); }
  };

  window.terminateWorkflow = async function(id) {
    if (!confirm('Terminate this workflow?')) return;
    try {
      await fetch(`${BASE}/v1/orchestrator/workflows/${id}/terminate`, { method: 'POST', headers: authHeaders() });
      loadWorkflows();
      loadOrchStats();
      showToast('Workflow terminated');
    } catch (e) { showToast('Failed', true); }
  };

  // ---- Providers ----
  async function loadProviders() {
    try {
      const res = await fetch(`${BASE}/v1/llm/providers`, { headers: authHeaders() });
      if (!res.ok) return;
      const d = await res.json();
      const list = document.getElementById('providers-list');
      if (!d.providers || d.providers.length === 0) {
        list.innerHTML = '<p class="empty-state">No LLM providers configured. Click "+ Add Provider" to register one.</p>';
        return;
      }
      list.innerHTML = d.providers.map(p => `
        <div class="provider-card">
          <div class="item-info">
            <div class="item-name">${p.model_id} <span class="provider-badge">${p.provider}</span></div>
            <div class="item-meta">API Key: ${p.api_key_set ? '••••••' : 'Not set'} · Temp: ${p.default_temperature} · Max tokens: ${p.max_tokens}</div>
          </div>
          <div class="item-actions">
            <button class="btn btn-sm btn-danger" onclick="deleteProvider('${p.model_id}')">Remove</button>
          </div>
        </div>
      `).join('');
    } catch (e) { console.warn('Load providers:', e); }
  }

  // Add provider
  const btnAddProv = document.getElementById('btn-add-provider');
  const addProvForm = document.getElementById('add-provider-form');
  const btnCancelProv = document.getElementById('btn-cancel-provider');
  const btnSaveProv = document.getElementById('btn-save-provider');

  if (btnAddProv) btnAddProv.addEventListener('click', () => { addProvForm.style.display = ''; });
  if (btnCancelProv) btnCancelProv.addEventListener('click', () => { addProvForm.style.display = 'none'; });
  if (btnSaveProv) btnSaveProv.addEventListener('click', async () => {
    const body = {
      provider: document.getElementById('provider-type').value,
      model_id: document.getElementById('provider-model').value,
      api_key: document.getElementById('provider-key').value || null,
      base_url: document.getElementById('provider-url').value || null,
    };
    try {
      const res = await fetch(`${BASE}/v1/llm/providers`, {
        method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
      });
      if (res.ok) {
        addProvForm.style.display = 'none';
        loadProviders();
        showToast('Provider registered');
      } else {
        const err = await res.json();
        showToast(err.detail || 'Failed', true);
      }
    } catch (e) { showToast('Failed to register provider', true); }
  });

  window.deleteProvider = async function(modelId) {
    if (!confirm('Remove this provider?')) return;
    try {
      await fetch(`${BASE}/v1/llm/providers/${modelId}`, { method: 'DELETE', headers: authHeaders() });
      loadProviders();
      showToast('Provider removed');
    } catch (e) { showToast('Failed', true); }
  };

  // ---- Toast helper (use existing if available) ----
  function showToast(msg, isError) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.className = 'toast visible' + (isError ? ' error' : '');
    setTimeout(() => { t.className = 'toast'; }, 3000);
  }

  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ---- Auto-load when orchestrator section is shown ----
  const navOrch = document.getElementById('nav-orchestrator');
  if (navOrch) {
    navOrch.addEventListener('click', () => {
      loadOrchStats();
      loadAgents();
      loadWorkflows();
      loadProviders();
    });
  }

  // ---- Sprint 13: Monitoring tab auto-refresh ----
  let monitoringInterval = null;

  function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m ${Math.floor(seconds % 60)}s`;
  }

  async function refreshMonitoring() {
    try {
      const resp = await fetch('/v1/monitoring/stats', {
        headers: { 'Authorization': `Bearer ${localStorage.getItem('gfm_api_key') || ''}` }
      });
      if (!resp.ok) return;
      const d = await resp.json();

      // System status
      const statusEl = document.getElementById('mon-status');
      if (statusEl) {
        statusEl.textContent = (d.status || 'ok').toUpperCase();
        statusEl.style.color = d.status === 'ok' ? 'var(--accent-emerald)' : 'var(--accent-amber)';
      }
      const uptimeEl = document.getElementById('mon-uptime');
      if (uptimeEl) uptimeEl.textContent = formatUptime(d.uptime_seconds || 0);
      const verEl = document.getElementById('mon-version');
      if (verEl) verEl.textContent = d.version || '—';
      const storesEl = document.getElementById('mon-stores');
      if (storesEl) storesEl.textContent = (d.stores && d.stores.active != null) ? d.stores.active : '—';

      // Pool stats
      const pool = d.pool || {};
      const poolSize = document.getElementById('mon-pool-size');
      if (poolSize) poolSize.textContent = pool.pooled ? pool.pool_size : 'N/A';
      const poolAvail = document.getElementById('mon-pool-avail');
      if (poolAvail) poolAvail.textContent = pool.pooled ? pool.pool_available : 'N/A';
      const poolWait = document.getElementById('mon-pool-waiting');
      if (poolWait) {
        poolWait.textContent = pool.pooled ? pool.requests_waiting : 'N/A';
        if (pool.requests_waiting > 0) poolWait.style.color = 'var(--accent-rose)';
        else poolWait.style.color = 'var(--accent-amber)';
      }
      const poolRange = document.getElementById('mon-pool-range');
      if (poolRange) poolRange.textContent = pool.pooled ? `${pool.pool_min} / ${pool.pool_max}` : 'N/A';

      // Pool utilization bar
      const bar = document.getElementById('mon-pool-bar');
      const barLabel = document.getElementById('mon-pool-bar-label');
      if (bar && pool.pooled && pool.pool_max > 0) {
        const used = pool.pool_size - pool.pool_available;
        const pct = Math.min(100, Math.round((used / pool.pool_max) * 100));
        bar.style.width = pct + '%';
        if (barLabel) barLabel.textContent = `${used} / ${pool.pool_max} in use (${pct}%)`;
        if (pct > 80) bar.style.background = 'linear-gradient(90deg,var(--accent-amber),var(--accent-rose))';
        else bar.style.background = 'linear-gradient(90deg,var(--accent-emerald),var(--accent-cyan))';
      }

      // Metrics
      const m = d.metrics || {};
      if (m.available) {
        const gov = m.governance || {};
        const govAllow = document.getElementById('mon-gov-allow');
        if (govAllow) govAllow.textContent = Math.round(gov.allow || 0);
        const govDeny = document.getElementById('mon-gov-deny');
        if (govDeny) govDeny.textContent = Math.round(gov.deny || 0);
        const govEsc = document.getElementById('mon-gov-escalate');
        if (govEsc) govEsc.textContent = Math.round(gov.escalate || 0);

        const wf = m.workflows || {};
        const wfComp = document.getElementById('mon-wf-completed');
        if (wfComp) wfComp.textContent = Math.round(wf.completed || 0);
        const wfFail = document.getElementById('mon-wf-failed');
        if (wfFail) wfFail.textContent = Math.round(wf.failed || 0);
        const wfTerm = document.getElementById('mon-wf-terminated');
        if (wfTerm) wfTerm.textContent = Math.round(wf.terminated || 0);

        const opsMem = document.getElementById('mon-ops-memory');
        if (opsMem) opsMem.textContent = Math.round(m.memory_operations || 0);
        const opsDec = document.getElementById('mon-ops-decisions');
        if (opsDec) opsDec.textContent = Math.round(m.decisions_logged || 0);
        const opsEr = document.getElementById('mon-ops-erasure');
        if (opsEr) opsEr.textContent = Math.round(m.erasure_certificates || 0);
        const opsWh = document.getElementById('mon-ops-webhooks');
        if (opsWh) opsWh.textContent = Math.round(m.webhooks_dispatched || 0);
        const opsSso = document.getElementById('mon-ops-sso');
        if (opsSso) opsSso.textContent = Math.round(m.sso_logins || 0);
      }

      // Last updated
      const lastEl = document.getElementById('mon-last-updated');
      if (lastEl) lastEl.textContent = `Last refreshed: ${new Date().toLocaleTimeString()} · Auto-refresh every 5s`;
    } catch (e) {
      console.warn('Monitoring refresh failed:', e);
    }
  }

  const navMon = document.getElementById('nav-monitoring');
  if (navMon) {
    navMon.addEventListener('click', () => {
      refreshMonitoring();
      clearInterval(monitoringInterval);
      monitoringInterval = setInterval(refreshMonitoring, 5000);
    });
  }

  // Stop polling when switching away from monitoring
  document.querySelectorAll('.nav-item').forEach(nav => {
    nav.addEventListener('click', () => {
      if (nav.dataset.section !== 'monitoring' && monitoringInterval) {
        clearInterval(monitoringInterval);
        monitoringInterval = null;
      }
    });
  });
})();
