(function () {
  'use strict';

  const BOT_SPECS = {
    'web-text-extractor': {
      title: 'Web & API Text Extractor',
      slug: 'web-text-extractor',
      badge: 'LIVE · FREE UTILITY',
      badgeClass: 'live',
      price: '$0 (Free Forever · 10 Cloud Extractions / UTC Day + Local Fallback)',
      status: 'available',
      installUrl: 'https://x.ai/bot/k1oyHmw4iS4VBW1U8_3Ol',
      specHtml: `
        <p>Self-onboarding Grok Bot utility template for extracting readable plain text from public documentation pages, regulatory releases, and technical articles.</p>
        <h4>1. Turn-1 Automatic API Key Provisioning</h4>
        <ul>
          <li>Checks for an existing <code>0600</code>-permissioned environment file in <code>/workspace</code>; if absent, slugifies the agent name to match <code>^[A-Za-z0-9_-]{3,64}$</code> (default <code>scraper-bot</code>) and registers an agent identity (<code>wck_...</code> API key) on Turn 1.</li>
          <li>Saves the provisioned credentials inside <code>/workspace</code> with <code>chmod 600</code> (<code>-rw-------</code>) permissions and completes initial verification tasks automatically.</li>
          <li>Verifies operational readiness on Turn 1 by running a live test extraction against <code>https://example.com</code> and reporting the remaining daily quota from <code>X-Quota-Remaining</code>.</li>
        </ul>
        <h4>2. Cloud Static-HTTP Extraction + Local Fallback</h4>
        <ul>
          <li><strong>Free Cloud Tier (<code>format: "text"</code>):</strong> 10 free extractions per UTC day using server-side static HTTP fetch (<code>requests.get</code>) + BeautifulSoup (<code>bs4</code>) plain-text extraction.</li>
          <li><strong>Automatic Local Fallback:</strong> Branches on structured JSON error <code>reason</code> codes (<code>quota_exhausted</code>, <code>tasks_incomplete</code>, <code>format_requires_payment</code>) and falls back cleanly to local <code>curl -sL</code> + Python HTML-to-text parsing (<code>bs4</code> or stdlib <code>html.parser</code>) in <code>/workspace</code>.</li>
        </ul>
      `,
      execHtml: `
        <h4>1. Execution Environment &amp; Telemetry Headers</h4>
        <ul>
          <li><strong>Runtime:</strong> Grok Bot isolated Linux container (<code>/workspace</code>, <code>bash</code>, <code>python3</code>, <code>curl</code>).</li>
          <li><strong>Quota Headers:</strong> Every cloud extraction returns <code>X-Quota-Limit: 10</code>, <code>X-Quota-Remaining</code>, and <code>X-Quota-Reset</code> (UTC midnight epoch).</li>
          <li><strong>Credential Storage:</strong> Stored locally in a <code>0600</code>-permissioned environment file in <code>/workspace</code>; loaded into shell subprocesses via <code>source</code> without echoing secret keys to stdout.</li>
        </ul>
        <h4>2. Network &amp; SSRF Filtering</h4>
        <ul>
          <li>Validates target URLs before transmission and rejects loopback (<code>127.0.0.0/8</code>, <code>::1</code>), RFC1918 private networks (<code>10.0.0.0/8</code>, <code>172.16.0.0/12</code>, <code>192.168.0.0/16</code>), link-local, and cloud metadata endpoints (<code>169.254.169.254</code>) with <code>HTTP 400</code> without consuming daily quota.</li>
        </ul>
      `,
      refusalHtml: `
        <h4>Mandatory Refusal Boundaries (What It Will Never Do)</h4>
        <ul>
          <li><strong>No False Headless-JS Claims:</strong> Explicitly documents that the cloud <code>/api/v1/scrape</code> endpoint performs static HTTP + BeautifulSoup parsing and does not execute client-side JavaScript or headless browser rendering.</li>
          <li><strong>No Secret Exposure:</strong> Never prints the raw <code>wck_...</code> API key in chat responses or logs.</li>
          <li><strong>No Unconfirmed Paid Calls:</strong> Never triggers non-free extraction modes without explicit per-call user confirmation.</li>
          <li><strong>No Internal Network Probing:</strong> Refuses all loopback, private LAN, and cloud metadata SSRF targets.</li>
        </ul>
      `
    },
    'company-buildout-tracker': {
      title: 'Company Deep-Dive & Physical Buildout Tracker',
      slug: 'company-buildout-tracker',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $39 (One-time USD license · Spec preview)',
      status: 'planned',
      specHtml: `
        <p>Primary-source OSINT research specification for tracking the real-world physical and regulatory footprint of robotics, AI data center, aerospace, and advanced manufacturing companies.</p>
        <h4>1. Multi-Source Regulatory &amp; Physical Correlation</h4>
        <ul>
          <li><strong>SEC EDGAR Filings:</strong> Extracts facility capex, property leases, equipment financing, and material agreements from <code>10-K</code>, <code>10-Q</code>, <code>8-K</code> (Items 1.01, 2.01, 2.03), and private-company <code>Form D</code> notices.</li>
          <li><strong>USPTO Patent &amp; Trademark Filings:</strong> Tracks published patent applications, CPC subclass velocity, and hardware/actuator assignee records across parent corporations and subsidiary LLCs.</li>
          <li><strong>Municipal Permits &amp; Job Requisitions:</strong> Cross-references municipal building/electrical permits (e.g., high-voltage switchgear, cleanroom buildouts) with site-specific manufacturing and controls engineering job openings.</li>
        </ul>
        <h4>2. Structured Dossier Output</h4>
        <ul>
          <li>Writes a reproducible Markdown and JSON dossier (<code>/workspace/dossier_&lt;entity&gt;.md</code> and <code>.json</code>) containing primary-source URLs, SEC accession numbers, publication IDs, and UTC retrieval timestamps.</li>
        </ul>
      `,
      execHtml: `
        <h4>1. Primary Data Sources &amp; Endpoints</h4>
        <ul>
          <li><strong>SEC EDGAR API:</strong> <code>https://data.sec.gov/submissions/CIK{10-digit}.json</code> and EDGAR full-text search index, queried with a compliant <code>User-Agent: AppName/Version (contact@domain.com)</code> header and rate-limited below 10 requests/second.</li>
          <li><strong>USPTO Open Data:</strong> Patent examination, publication, and assignment datasets queried by assignee name and inventor cohort.</li>
          <li><strong>Municipal &amp; Career Portals:</strong> Public municipal permit tables and company career endpoints parsed locally in <code>/workspace</code>.</li>
        </ul>
        <h4>2. Local Sandbox Artifacts</h4>
        <ul>
          <li>Saves raw JSON responses under <code>/workspace/cache/&lt;cik_or_slug&gt;/</code> so every line in the final dossier can be audited against the exact upstream payload.</li>
        </ul>
      `,
      refusalHtml: `
        <h4>Mandatory Refusal Boundaries (What It Will Never Do)</h4>
        <ul>
          <li><strong>Never Fabricates Records:</strong> Never invents unfiled municipal permits, unpublished financial figures, or unverified facility capacities.</li>
          <li><strong>Never Conflates Commentary with Primary Filings:</strong> Never presents secondary news articles, blog posts, or social media rumors as primary regulatory filings.</li>
          <li><strong>Zero Financial or Equity Advice:</strong> Never outputs stock buy, sell, or hold recommendations, valuation multiples, or price targets.</li>
        </ul>
      `
    },
    'sec-patent-watchdog': {
      title: 'SEC & Patent Watchdog Digest',
      slug: 'sec-patent-watchdog',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $29 (One-time USD license · Spec preview)',
      status: 'planned',
      specHtml: `
        <p>Scheduled daily or weekly monitoring agent specification that polls SEC EDGAR RSS/JSON feeds and USPTO assignment/publication endpoints for a user-defined watchlist of CIKs, tickers, or assignee entities.</p>
        <h4>1. Deterministic Watchlist State Diffing</h4>
        <ul>
          <li>Reads user-configured targets from <code>/workspace/watchlist.json</code> (CIKs, tickers, patent assignees, and specific form filters such as <code>8-K</code>, <code>10-Q</code>, <code>Form 4</code>, or <code>Form D</code>).</li>
          <li>Compares newly fetched accession numbers and patent publication IDs against the persisted state ledger in <code>/workspace/watchlist_state.json</code>.</li>
          <li>Generates a timestamped delta digest only when one or more new primary filings are detected, and stays completely silent when nothing changed.</li>
        </ul>
      `,
      execHtml: `
        <h4>1. Polling Architecture &amp; Rate Discipline</h4>
        <ul>
          <li><strong>SEC Submissions JSON:</strong> Polls <code>data.sec.gov/submissions/CIK{10-digit}.json</code> with mandatory <code>User-Agent</code> identification and 150ms minimum inter-request spacing.</li>
          <li><strong>USPTO Docket Feeds:</strong> Polls newly published patent applications and assignment transfers matching tracked corporate entities.</li>
          <li><strong>Atomic State Updates:</strong> Writes updated state to <code>/workspace/watchlist_state.json.tmp</code> and atomically renames after digest generation succeeds.</li>
        </ul>
      `,
      refusalHtml: `
        <h4>Mandatory Refusal Boundaries (What It Will Never Do)</h4>
        <ul>
          <li><strong>Zero Noisy Filler Alerts:</strong> Never sends "Checked watchlist, no changes found" filler messages on scheduled runs.</li>
          <li><strong>Never Bypasses Rate Limits or Identification Rules:</strong> Refuses to query SEC EDGAR without a valid contact <code>User-Agent</code> string or above permitted request rates.</li>
          <li><strong>Never Executes Financial Actions:</strong> Strictly read-only filing monitor; never connects to brokerage accounts or executes trades.</li>
        </ul>
      `
    },
    'print-farm-ops': {
      title: '3D Print Farm & Slicer Queue Ops',
      slug: 'print-farm-ops',
      badge: 'IN DEVELOPMENT · PHASE 2 SPEC',
      badgeClass: 'phase2',
      price: 'Planned: $29 (One-time USD license · Spec preview)',
      status: 'planned',
      specHtml: `
        <p>Production queue, filament bill-of-materials (BOM), and OrcaSlicer <code>.3mf</code> profile auditing specification for multi-printer additive manufacturing shops.</p>
        <h4>1. OrcaSlicer <code>.3mf</code> Profile Auditing &amp; Queue Management</h4>
        <ul>
          <li><strong>Archive Inspection:</strong> Unpacks standard <code>.3mf</code> ZIP/XML containers in <code>/workspace</code> to audit nozzle diameter, layer height, wall loop count, infill percentage, support overrides, and filament type (PLA-CF, TPU, PETG, PA-CF/Nylon) across plates.</li>
          <li><strong>Per-Part Unit Costing:</strong> Calculates filament weight (grams + purge waste), spool cost, printer depreciation, and electricity kWh cost per part.</li>
          <li><strong>SKU Inventory Run-Rates:</strong> Cross-references active storefront/eBay SKU inventory counts against daily sales velocity to prioritize print queues.</li>
        </ul>
      `,
      execHtml: `
        <h4>1. Local File Formats &amp; Outputs</h4>
        <ul>
          <li><strong>Inputs:</strong> OrcaSlicer / Bambu Studio <code>.3mf</code> project files, spool inventory JSON/CSV, and storefront SKU order exports placed in <code>/workspace</code>.</li>
          <li><strong>Outputs:</strong> Writes <code>/workspace/farm_queue_report.csv</code>, <code>/workspace/profile_audit_diff.md</code>, and low-stock filament reorder alerts.</li>
          <li><strong>Zero Cloud Lock-In:</strong> Runs 100% locally using Python stdlib <code>zipfile</code>, <code>xml.etree.ElementTree</code>, and <code>csv</code> modules inside <code>/workspace</code>.</li>
        </ul>
      `,
      refusalHtml: `
        <h4>Mandatory Refusal Boundaries (What It Will Never Do)</h4>
        <ul>
          <li><strong>No Unconfirmed Print Starts or Raw G-Code Transmission:</strong> Never sends raw G-code or triggers remote print starts over LAN/MQTT/cloud printer APIs without explicit per-job user confirmation.</li>
          <li><strong>Never Overwrites Master <code>.3mf</code> Files:</strong> Strictly reads master <code>.3mf</code> project files in read-only mode and writes reports or modified copies to separate paths in <code>/workspace</code>.</li>
        </ul>
      `
    },
    'robotics-telemetry-inspector': {
      title: 'Humanoid & Fleet Telemetry Inspector',
      slug: 'robotics-telemetry-inspector',
      badge: 'IN DEVELOPMENT · PHASE 3 SPEC',
      badgeClass: 'phase3',
      price: 'Planned: $49 (One-time USD license · Spec preview)',
      status: 'planned',
      specHtml: `
        <p>Read-only diagnostic analysis specification for mobile robot fleets, humanoid actuator logs, and stationary/mobile lithium battery management systems (BMS).</p>
        <h4>1. Actuator, Joint &amp; BMS Telemetry Diagnostics</h4>
        <ul>
          <li><strong>Actuator Thermal &amp; Torque Logs:</strong> Parses joint encoder, phase current, RMS torque duty cycle, and stator winding temperature logs to flag thermal throttling and harmonic drive wear intervals.</li>
          <li><strong>Battery Pack BMS Cell-Delta Auditing:</strong> Analyzes 16S LiFePO4 stationary packs and mobile robot battery exports for per-cell voltage spread (<code>mV</code> delta under peak load vs. rest), MOSFET temperature spikes, and cycle-life capacity fade.</li>
          <li><strong>Maintenance Schedules:</strong> Outputs prioritized preventive maintenance checklists and component replacement tables in <code>/workspace/telemetry_diagnostics.md</code>.</li>
        </ul>
      `,
      execHtml: `
        <h4>1. Supported Telemetry Exports</h4>
        <ul>
          <li><strong>Inputs:</strong> CSV, JSON, or decoded CAN/RS485 telemetry log files placed in <code>/workspace</code>.</li>
          <li><strong>Analysis Pipeline:</strong> Deterministic Python statistical pass computing min/max/p95 temperatures, cell voltage standard deviation, coulomb-counted charge efficiency, and fault-code timelines.</li>
          <li><strong>Outputs:</strong> Self-contained Markdown report (<code>/workspace/telemetry_diagnostics.md</code>) and structured anomaly CSV (<code>/workspace/anomalies.csv</code>).</li>
        </ul>
      `,
      refusalHtml: `
        <h4>Mandatory Refusal Boundaries (What It Will Never Do)</h4>
        <ul>
          <li><strong>Strictly Read-Only Diagnostics:</strong> Analyzes exported log files only; never connects to live motor controllers to issue motion trajectories or torque commands.</li>
          <li><strong>Zero BMS Register Writes:</strong> Never modifies over-voltage, under-voltage, over-current, or thermal cutoff registers on any BMS.</li>
          <li><strong>Zero Safety-Interlock Overrides:</strong> Never generates scripts or commands that bypass hardware E-stops, torque limits, or thermal shutdown thresholds.</li>
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
    } catch (err) {
      showToast('Copy failed — select and copy manually.');
    }
  }

  // Copy buttons
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const value = btn.getAttribute('data-copy');
      const label = btn.getAttribute('data-copy-label') || '✓ Copied!';
      copyText(value, btn, label);
    });
  });

  // Reusable ARIA Tablist controller (supports ArrowLeft, ArrowRight, Home, End, roving tabindex, and hidden sync)
  function initAriaTablist(tabSelector, paneSelector, dataAttr) {
    const tabs = Array.from(document.querySelectorAll(tabSelector));
    const panes = Array.from(document.querySelectorAll(paneSelector));

    function activateTab(tab, focusTab) {
      const targetId = tab.getAttribute(dataAttr);
      tabs.forEach((t) => {
        const isSelected = t === tab;
        t.classList.toggle('active', isSelected);
        t.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        t.setAttribute('tabindex', isSelected ? '0' : '-1');
      });
      panes.forEach((p) => {
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

    tabs.forEach((tab, idx) => {
      tab.addEventListener('click', () => activateTab(tab, false));
      tab.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          const dir = e.key === 'ArrowRight' ? 1 : -1;
          const nextIdx = (idx + dir + tabs.length) % tabs.length;
          activateTab(tabs[nextIdx], true);
        } else if (e.key === 'Home') {
          e.preventDefault();
          activateTab(tabs[0], true);
        } else if (e.key === 'End') {
          e.preventDefault();
          activateTab(tabs[tabs.length - 1], true);
        }
      });
    });

    return { tabs, activateTab };
  }

  // Initialize Hero Terminal Tabs and Modal Tabs
  initAriaTablist('.term-tab', '.term-pane', 'data-tab');
  const modalTabController = initAriaTablist('.modal-tab', '.modal-pane', 'data-modal-tab');

  // Category Filter Pills (with aria-pressed="true/false")
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

  // Accessible Spec Modal (role="dialog", aria-modal="true", aria-labelledby="modalTitle", #appRoot inert, focus trap)
  const appRoot = document.getElementById('appRoot');
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
      if (spec.status === 'available' && spec.installUrl) {
        modalFooter.innerHTML = `
          <span class="skill-slug">Status: Live Free Utility · Runs in Grok Bot <code>/workspace</code></span>
          <a href="${spec.installUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-primary btn-sm">
            Install Free in Grok Bot →
          </a>
        `;
      } else {
        modalFooter.innerHTML = `
          <span class="skill-slug">Status: Engineering Specification Preview · Not Yet Available for Purchase</span>
          <span class="btn-disabled btn-sm" aria-disabled="true">Not Yet Available</span>
        `;
      }
    }

    if (modalTabController && modalTabController.tabs.length > 0) {
      modalTabController.activateTab(modalTabController.tabs[0], false);
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
      ).filter((el) => !el.hasAttribute('disabled') && !el.closest('[hidden]'));
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
