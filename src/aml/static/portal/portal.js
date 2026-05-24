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

    // Sidebar navigation
    document.querySelectorAll('.nav-item[data-section]').forEach(nav => {
      nav.addEventListener('click', () => showSection(nav.dataset.section));
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
