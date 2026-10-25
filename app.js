(function () {
  'use strict';

  const BOT_SPECS = {
    'web-text-extractor': {
      title: 'Web & API Text Extractor',
      slug: 'getting-started + web-extractor',
      badge: 'LIVE · FREE UTILITY',
      badgeClass: 'live',
      price: '$0 (Free Forever · 10/UTC Day + Local Fallback)',
      shareUrl: 'https://x.ai/bot/k1oyHmw4iS4VBW1U8_3Ol',
      specHtml: `
        <p>Self-onboarding Grok Bot utility for clean web page and technical documentation text extraction inside your isolated <code>/workspace</code> Linux sandbox.</p>
        <h4>Included Skills (<code>getting-started</code> + <code>web-extractor</code>)</h4>
        <ul>
          <li><strong>Turn-1 Key Provisioning (<code>getting-started</code>):</strong> Checks for <code>/workspace/.wattcoin_env</code> (<code>0600</code> permissions); if absent, registers an agent key (<code>wck_...</code>) against <code>https://www.wattcoin.org/api/v1/agents/register</code>, completes initial readiness tasks, and runs a live verification extraction against <code>https://example.com</code>.</li>
          <li><strong>10 Free Cloud Extractions / UTC Day (<code>web-extractor</code>):</strong> Calls <code>POST https://www.wattcoin.org/api/v1/scrape</code> with <code>format: "text"</code> and reports <code>X-Quota-Limit: 10</code>, <code>X-Quota-Remaining</code>, and <code>X-Quota-Reset</code> headers.</li>
          <li><strong>Paid Modes Disclosure:</strong> <code>format: "html"</code> and <code>format: "json"</code> are paid per-call modes on the registry (see <code>https://www.wattcoin.org/llms.txt</code> for paid-tier specs); this free utility uses <code>format: "text"</code> (10/day free) and never invokes paid modes without explicit user confirmation.</li>
        </ul>
      `,
      execHtml: `
        <p>Executes inside your Grok Bot Linux container using standard POSIX utilities (<code>curl</code>, <code>chmod 600</code>) and Python 3 HTML-to-text parsing.</p>
        <h4>Execution Pipeline &amp; Fallback</h4>
        <ul>
          <li><strong>Cloud Static HTTP + BeautifulSoup:</strong> Server-side <code>requests.get</code> + BeautifulSoup (<code>bs4</code>) readable text extraction (15s timeout, 2 MB response cap).</li>
          <li><strong>Deterministic Error Branching:</strong> Branches on structured JSON error <code>reason</code> codes (<code>quota_exhausted</code>, <code>tasks_incomplete</code>, <code>format_requires_payment</code>).</li>
          <li><strong>Automatic Local Fallback:</strong> When the 10/day cloud quota is exhausted, automatically falls back to local <code>curl -sL</code> + Python HTML-to-text extraction (<code>bs4</code> or stdlib <code>html.parser</code>) in <code>/workspace</code>.</li>
        </ul>
      `,
      refusalHtml: `
        <p>Strict operational guardrails enforced on every invocation:</p>
        <h4>Mandatory Refusal Boundaries</h4>
        <ul>
          <li><strong>No False Headless-JS Claims:</strong> Never claims headless browser or client-side JavaScript SPA rendering on the static HTTP cloud endpoint (<code>https://www.wattcoin.org/api/v1/scrape</code>).</li>
          <li><strong>Zero Secret Leakage:</strong> Never prints or echoes your <code>wck_...</code> API key in chat output; keeps credentials in <code>/workspace/.wattcoin_env</code> (<code>chmod 600</code>).</li>
          <li><strong>SSRF &amp; Private Network Rejection:</strong> Rejects loopback (<code>127.0.0.0/8</code>, <code>::1</code>), RFC1918 private IPs (<code>10.0.0.0/8</code>, <code>172.16.0.0/12</code>, <code>192.168.0.0/16</code>), link-local, and cloud metadata (<code>169.254.169.254</code>) with <code>HTTP 400</code>.</li>
          <li><strong>No Unconfirmed Paid Calls:</strong> <code>format: "html"</code> and <code>format: "json"</code> are paid per-call modes on the registry (see <code>https://www.wattcoin.org/llms.txt</code> for paid-tier specs); this free utility uses <code>format: "text"</code> (10/day free) and never invokes paid modes without explicit user confirmation.</li>
        </ul>
      `
    },
    'company-buildout-tracker': {
      title: 'Company Deep-Dive & Physical Buildout Tracker',
      slug: 'company-buildout-tracker',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $39 USD (One-Time License · Spec Preview)',
      shareUrl: null,
      specHtml: `
        <p>Primary-source corporate infrastructure and physical facility buildout tracker for robotics companies, AI data centers, aerospace manufacturers, and industrial hardware operators.</p>
        <h4>What It Does (Phase 2 Specification)</h4>
        <ul>
          <li><strong>SEC EDGAR Regulatory Filings:</strong> Queries official SEC EDGAR submissions JSON endpoints for <code>10-K</code>, <code>10-Q</code>, <code>8-K</code> (Item 1.01 material agreements &amp; Item 2.01 asset acquisitions), and private-company <code>Form D</code> capital raises.</li>
          <li><strong>USPTO Patent &amp; Trademark Tracking:</strong> Cross-references newly published patent applications, CPC classifications, and assignee transfers across parent and subsidiary entities.</li>
          <li><strong>Municipal Permits &amp; Hiring Velocity:</strong> Correlates municipal building/electrical permits, industrial zoning filings, and facility-specific engineering job requisitions.</li>
          <li><strong>Cited Intelligence Dossier:</strong> Writes <code>/workspace/dossier_&lt;entity&gt;.md</code> and <code>.json</code> with exact accession numbers, filing dates, and primary URLs.</li>
        </ul>
      `,
      execHtml: `
        <p>Runs locally inside <code>/workspace</code> using compliant public-record queries and deterministic document parsers.</p>
        <h4>Required Inputs &amp; Data Sources</h4>
        <ul>
          <li><strong>Target Identifier:</strong> Company legal name, stock ticker, or 10-digit SEC CIK.</li>
          <li><strong>SEC EDGAR Compliance:</strong> Requires a valid <code>User-Agent: OrgName/1.0 (contact@domain.com)</code> header and enforces <code>&lt;= 10 requests/second</code> rate limiting against <code>data.sec.gov</code>.</li>
          <li><strong>Primary Endpoints:</strong> SEC EDGAR Submissions API, USPTO Patent Examination/Assignment datasets, and public municipal permit portals.</li>
        </ul>
      `,
      refusalHtml: `
        <p>Hard OSINT and financial compliance boundaries:</p>
        <h4>Mandatory Refusal Boundaries</h4>
        <ul>
          <li><strong>Zero Fabricated Filings:</strong> Never invents unfiled permits, unpublished financial figures, or unverified facility capacities.</li>
          <li><strong>Primary Sources Over Rumors:</strong> Never presents secondary news commentary or social media speculation as primary regulatory filings.</li>
          <li><strong>Zero Investment Advice:</strong> Never outputs stock buy, sell, or hold ratings, valuation multiples, or price targets.</li>
        </ul>
      `
    },
    'sec-patent-watchdog': {
      title: 'SEC & Patent Watchdog Digest',
      slug: 'sec-patent-watchdog',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $29 USD (One-Time License · Spec Preview)',
      shareUrl: null,
      specHtml: `
        <p>Scheduled daily or weekly delta monitoring agent for SEC EDGAR filings and USPTO patent dockets across a user-defined watchlist of CIKs, tickers, and assignees.</p>
        <h4>What It Does (Phase 2 Specification)</h4>
        <ul>
          <li><strong>Watchlist Polling:</strong> Reads tracked CIKs, tickers, and patent assignees from <code>/workspace/watchlist.json</code>.</li>
          <li><strong>Stateful Delta Diffing:</strong> Compares newly published SEC accession numbers and USPTO publication IDs against <code>/workspace/watchlist_state.json</code>.</li>
          <li><strong>Zero-Noise Digest Generation:</strong> Emits a structured Markdown delta brief only when verified new primary filings appear, and remains completely silent when zero new filings exist.</li>
        </ul>
      `,
      execHtml: `
        <p>Designed for Grok Bot scheduled routines with persistent local state in <code>/workspace</code>.</p>
        <h4>Execution &amp; State Files</h4>
        <ul>
          <li><strong>Watchlist Config:</strong> <code>/workspace/watchlist.json</code> (array of CIKs, tickers, and USPTO assignee names).</li>
          <li><strong>Checkpoint Ledger:</strong> <code>/workspace/watchlist_state.json</code> (stores last-seen accession numbers and UTC check timestamps).</li>
          <li><strong>Rate-Limit Discipline:</strong> Enforces SEC EDGAR fair-access throttling (<code>&lt;= 10 req/sec</code>) and mandatory <code>User-Agent</code> identification.</li>
        </ul>
      `,
      refusalHtml: `
        <p>Strict anti-noise and compliance rules:</p>
        <h4>Mandatory Refusal Boundaries</h4>
        <ul>
          <li><strong>No Empty Filler Alerts:</strong> Never sends noisy "no change today" notifications when the filing delta is zero.</li>
          <li><strong>No Rate-Limit Bypass:</strong> Never spoofs headers or bypasses SEC EDGAR rate limits.</li>
          <li><strong>No Financial Execution:</strong> Never connects to brokerage APIs or executes trades.</li>
        </ul>
      `
    },
    'print-farm-ops': {
      title: '3D Print Farm & Slicer Queue Ops',
      slug: 'print-farm-ops',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $29 USD (One-Time License · Spec Preview)',
      shareUrl: null,
      specHtml: `
        <p>Local production queue auditor, OrcaSlicer <code>.3mf</code> profile verifier, and filament unit-economics calculator for multi-printer additive manufacturing shops.</p>
        <h4>What It Does (Phase 2 Specification)</h4>
        <ul>
          <li><strong>OrcaSlicer <code>.3mf</code> Inspection:</strong> Unpacks <code>.3mf</code> ZIP/XML containers in read-only mode to verify nozzle diameter, layer height, wall loops, infill density, and support settings across plates.</li>
          <li><strong>Filament BOM &amp; Spool Ledger:</strong> Tracks PLA-CF, PETG, TPU, and PA-CF spool weights, desiccant replacement intervals, and per-part gram consumption.</li>
          <li><strong>Unit Cost &amp; SKU Run-Rate Tables:</strong> Computes material cost, machine hours, electricity kWh cost, and storefront reorder priorities in <code>/workspace/farm_queue_report.csv</code>.</li>
        </ul>
      `,
      execHtml: `
        <p>Executes 100% locally inside <code>/workspace</code> using Python's <code>zipfile</code>, <code>xml.etree.ElementTree</code>, and <code>csv</code> modules.</p>
        <h4>Supported Inputs &amp; Outputs</h4>
        <ul>
          <li><strong>Inputs:</strong> OrcaSlicer / Bambu Studio <code>.3mf</code> project files and SKU inventory CSVs placed in <code>/workspace</code>.</li>
          <li><strong>Outputs:</strong> Audited plate configuration tables and <code>/workspace/farm_queue_report.csv</code>.</li>
        </ul>
      `,
      refusalHtml: `
        <p>Physical shop and file-integrity safety boundaries:</p>
        <h4>Mandatory Refusal Boundaries</h4>
        <ul>
          <li><strong>Zero Unconfirmed Printer Actuation:</strong> Never transmits raw G-code or triggers remote print starts over LAN/MQTT/cloud APIs without explicit per-job user confirmation.</li>
          <li><strong>Non-Destructive Auditing:</strong> Never overwrites or mutates master <code>.3mf</code> project archives in place.</li>
        </ul>
      `
    },
    'robotics-telemetry-inspector': {
      title: 'Humanoid & Fleet Telemetry Inspector',
      slug: 'robotics-telemetry-inspector',
      badge: 'IN DEVELOPMENT · PHASE 3 SPEC',
      badgeClass: 'phase3',
      price: 'Planned: $49 USD (One-Time License · Spec Preview)',
      shareUrl: null,
      specHtml: `
        <p>Read-only diagnostic analyzer for robotic joint actuator logs, harmonic-drive duty cycles, and 16S LiFePO4 / mobile robot battery management system (BMS) telemetry.</p>
        <h4>What It Does (Phase 3 Specification)</h4>
        <ul>
          <li><strong>Actuator Thermal &amp; Torque Analysis:</strong> Parses CSV/JSON joint logs to flag stator winding temperature spikes, RMS torque overload, and duty-cycle anomalies.</li>
          <li><strong>16S LiFePO4 &amp; Fleet BMS Diagnostics:</strong> Computes per-cell voltage delta (<code>mV</code> spread under load vs. rest), MOSFET thermal rise, and cycle-life state-of-health trends.</li>
          <li><strong>Preventive Maintenance Dossier:</strong> Generates timestamped anomaly reports and maintenance schedules in <code>/workspace/telemetry_diagnostics.md</code>.</li>
        </ul>
      `,
      execHtml: `
        <p>Runs offline against exported telemetry logs in <code>/workspace</code> with zero hardware write access.</p>
        <h4>Supported Telemetry Formats</h4>
        <ul>
          <li><strong>Inputs:</strong> Actuator CSV/JSON logs, decoded CAN/RS485 frame exports, and 16S BMS cell-voltage time series in <code>/workspace</code>.</li>
          <li><strong>Outputs:</strong> <code>/workspace/telemetry_diagnostics.md</code> + structured anomaly summary JSON.</li>
        </ul>
      `,
      refusalHtml: `
        <p>Strict hardware safety boundaries:</p>
        <h4>Mandatory Refusal Boundaries</h4>
        <ul>
          <li><strong>Strictly Read-Only Telemetry:</strong> Never issues motor or actuator motion commands, torque setpoints, or CAN bus frames.</li>
          <li><strong>No BMS Register Writes:</strong> Never modifies BMS protection thresholds, balancing parameters, or hardware safety interlocks.</li>
        </ul>
      `
    }
  };

  function showToast(msg) {
    const toast = document.getElementById('copy-toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(window.__toastTimer);
    window.__toastTimer = setTimeout(() => {
      toast.classList.remove('show');
    }, 2600);
  }

  async function copyText(text, triggerBtn, feedbackLabel) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      if (triggerBtn) {
        const originalHtml = triggerBtn.getAttribute('data-original-html') || triggerBtn.innerHTML;
        if (!triggerBtn.getAttribute('data-original-html')) {
          triggerBtn.setAttribute('data-original-html', originalHtml);
        }
        triggerBtn.innerHTML = feedbackLabel || '✓ Copied';
        setTimeout(() => {
          triggerBtn.innerHTML = originalHtml;
        }, 2000);
      }
      showToast('Copied to clipboard: ' + (text.length > 80 ? text.slice(0, 80) + '…' : text));
    } catch (_) {
      showToast('Copy failed — select and copy manually.');
    }
  }

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const value = btn.getAttribute('data-copy');
      const label = btn.getAttribute('data-copy-label') || '✓ Copied!';
      copyText(value, btn, label);
    });
  });

  // Hero Terminal Tabs (ARIA tablist + keyboard arrow navigation)
  const termTabs = Array.from(document.querySelectorAll('.term-tab'));
  const termPanes = Array.from(document.querySelectorAll('.term-pane'));

  function activateTermTab(tab, focusTab) {
    const targetId = tab.getAttribute('data-tab');
    termTabs.forEach((t) => {
      const isSelected = t === tab;
      t.classList.toggle('active', isSelected);
      t.setAttribute('aria-selected', isSelected ? 'true' : 'false');
      t.setAttribute('tabindex', isSelected ? '0' : '-1');
    });
    termPanes.forEach((p) => {
      const isMatch = p.id === targetId;
      p.classList.toggle('active', isMatch);
      if (isMatch) {
        p.removeAttribute('hidden');
      } else {
        p.setAttribute('hidden', '');
      }
    });
    if (focusTab) tab.focus();
  }

  termTabs.forEach((tab, idx) => {
    tab.addEventListener('click', () => activateTermTab(tab, false));
    tab.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const dir = e.key === 'ArrowRight' ? 1 : -1;
        const nextIdx = (idx + dir + termTabs.length) % termTabs.length;
        activateTermTab(termTabs[nextIdx], true);
      } else if (e.key === 'Home') {
        e.preventDefault();
        activateTermTab(termTabs[0], true);
      } else if (e.key === 'End') {
        e.preventDefault();
        activateTermTab(termTabs[termTabs.length - 1], true);
      }
    });
  });

  // Category Filter Pills (with aria-pressed)
  const filterPills = document.querySelectorAll('.filter-pill');
  const botCards = document.querySelectorAll('.bot-card');
  filterPills.forEach((pill) => {
    pill.addEventListener('click', () => {
      const category = pill.getAttribute('data-filter');
      filterPills.forEach((p) => {
        const isActive = p === pill;
        p.classList.toggle('active', isActive);
        p.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      });

      botCards.forEach((card) => {
        const cats = (card.getAttribute('data-categories') || '').split(/\s+/);
        if (category === 'all' || cats.includes(category)) {
          card.classList.remove('hidden');
        } else {
          card.classList.add('hidden');
        }
      });
    });
  });

  // Spec Modal with 3 ARIA Tabs + Background Inert + Focus Trap
  const modalBackdrop = document.getElementById('spec-modal');
  const modalPanel = document.getElementById('modalPanel');
  const modalTitle = document.getElementById('modalTitle');
  const modalBadge = document.getElementById('modal-badge');
  const modalSlug = document.getElementById('modal-slug');
  const modalPaneSpec = document.getElementById('modal-pane-spec');
  const modalPaneExec = document.getElementById('modal-pane-exec');
  const modalPaneRefusal = document.getElementById('modal-pane-refusal');
  const modalFooter = document.getElementById('modal-footer');
  const modalCloseBtn = document.getElementById('modal-close');
  const modalTabs = Array.from(document.querySelectorAll('.modal-tab'));
  const modalPanes = Array.from(document.querySelectorAll('.modal-pane'));
  const appRoot = document.getElementById('appRoot');
  let lastFocusedTrigger = null;

  function setBackgroundInert(isInert) {
    if (!appRoot) return;
    if (isInert) {
      appRoot.setAttribute('inert', '');
      appRoot.setAttribute('aria-hidden', 'true');
    } else {
      appRoot.removeAttribute('inert');
      appRoot.removeAttribute('aria-hidden');
    }
  }

  function activateModalTab(tab, focusTab) {
    const targetId = tab.getAttribute('data-modal-tab');
    modalTabs.forEach((t) => {
      const isSelected = t === tab;
      t.classList.toggle('active', isSelected);
      t.setAttribute('aria-selected', isSelected ? 'true' : 'false');
      t.setAttribute('tabindex', isSelected ? '0' : '-1');
    });
    modalPanes.forEach((p) => {
      const isMatch = p.id === targetId;
      p.classList.toggle('active', isMatch);
      if (isMatch) {
        p.removeAttribute('hidden');
      } else {
        p.setAttribute('hidden', '');
      }
    });
    if (focusTab) tab.focus();
  }

  modalTabs.forEach((tab, idx) => {
    tab.addEventListener('click', () => activateModalTab(tab, false));
    tab.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const dir = e.key === 'ArrowRight' ? 1 : -1;
        const nextIdx = (idx + dir + modalTabs.length) % modalTabs.length;
        activateModalTab(modalTabs[nextIdx], true);
      } else if (e.key === 'Home') {
        e.preventDefault();
        activateModalTab(modalTabs[0], true);
      } else if (e.key === 'End') {
        e.preventDefault();
        activateModalTab(modalTabs[modalTabs.length - 1], true);
      }
    });
  });

  function openSpecModal(botKey, triggerEl) {
    const spec = BOT_SPECS[botKey];
    if (!spec || !modalBackdrop) return;
    lastFocusedTrigger = triggerEl || document.activeElement;
    modalTitle.textContent = spec.title;
    modalBadge.textContent = spec.badge;
    modalBadge.className = 'phase-badge ' + (spec.badgeClass || 'phase2');
    modalSlug.textContent = spec.slug + ' · ' + spec.price;

    if (modalPaneSpec) modalPaneSpec.innerHTML = spec.specHtml;
    if (modalPaneExec) modalPaneExec.innerHTML = spec.execHtml;
    if (modalPaneRefusal) modalPaneRefusal.innerHTML = spec.refusalHtml;

    if (modalFooter) {
      if (spec.shareUrl) {
        modalFooter.innerHTML = `
          <span class="skill-slug">Status: LIVE · One-Click Grok Bot Import</span>
          <a href="${spec.shareUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-primary btn-sm">Install Free in Grok Bot →</a>
        `;
      } else {
        modalFooter.innerHTML = `
          <span class="skill-slug">Status: IN DEVELOPMENT (Specification Preview Only)</span>
          <span class="btn-disabled btn-sm" aria-disabled="true">Not Yet Available for Purchase</span>
        `;
      }
    }

    if (modalTabs.length > 0) {
      activateModalTab(modalTabs[0], false);
    }

    modalBackdrop.classList.add('open');
    if (modalPanel) modalPanel.scrollTop = 0;
    modalBackdrop.setAttribute('aria-hidden', 'false');
    setBackgroundInert(true);
    if (modalCloseBtn) modalCloseBtn.focus();
  }

  function closeSpecModal() {
    if (!modalBackdrop || !modalBackdrop.classList.contains('open')) return;
    modalBackdrop.classList.remove('open');
    modalBackdrop.setAttribute('aria-hidden', 'true');
    setBackgroundInert(false);
    if (lastFocusedTrigger && typeof lastFocusedTrigger.focus === 'function') {
      lastFocusedTrigger.focus();
    }
    lastFocusedTrigger = null;
  }

  document.querySelectorAll('[data-open-spec]').forEach((btn) => {
    btn.addEventListener('click', () => {
      openSpecModal(btn.getAttribute('data-open-spec'), btn);
    });
  });

  if (modalCloseBtn) {
    modalCloseBtn.addEventListener('click', closeSpecModal);
  }
  if (modalBackdrop) {
    modalBackdrop.addEventListener('click', (e) => {
      if (e.target === modalBackdrop) closeSpecModal();
    });
  }

  document.addEventListener('keydown', (e) => {
    if (!modalBackdrop || !modalBackdrop.classList.contains('open')) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeSpecModal();
    } else if (e.key === 'Tab' && modalPanel) {
      const focusables = Array.from(
        modalPanel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
      ).filter((el) => !el.hasAttribute('disabled') && !el.hidden && el.offsetParent !== null && el.tabIndex >= 0);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });
})();
