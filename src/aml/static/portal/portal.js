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
