#!/usr/bin/env python3
"""3D Print Farm & Slicer Queue Ops (strictly read-only, offline, stdlib + bot_common).

Audits local slicer projects and farm tables across four buckets:
  1. .3mf / .gcode.3mf / .gcode slicer-profile and thermal / mechanical safety audit (OrcaSlicer, Bambu Studio,
     PrusaSlicer / SuperSlicer, standalone G-code comment blocks), with hardened ZIP handling
  2. filament spool inventory, moisture / desiccant hygiene and BOM depletion against the queued work
  3. per-part unit economics (filament, electricity, machine, labour) and margin audit
  4. storefront SKU run-rate, stock-out risk and a greedy multi-printer queue schedule (--queue-csv)
It makes no network request, never sends G-code or starts / stops / heats anything, never modifies a .3mf or .gcode
file, and refuses credentials, API keys, seed phrases and printer LAN access codes (exit 10, value never echoed).
Exit codes: 0 clean, 1 MEDIUM (or LOW/INFO with --fail-on low/info), 2 HIGH, 3 CRITICAL, 10 bad input / refusal,
11 degraded (unreadable / unsafe / malformed input, or nothing to audit).

Tables (CSV or JSON array of objects; header names are case-insensitive):
  --spools   spool_id,material,brand,color,initial_g,remaining_g,cost_usd,cost_per_kg,reorder_threshold_g,dried_at,
             last_desiccant_change,abrasive,hygroscopic,max_nozzle_c,min_nozzle_c,bed_min_c,bed_max_c,requires_enclosure
  --orders   sku,name,plate_file,material,grams_per_unit,print_hours_per_unit,units_per_plate,post_proc_mins,on_hand,
             open_orders,daily_sales_velocity,lead_time_days,safety_stock_days,sale_price_usd,marketplace_fee_pct,
             shipping_cost_usd,structural
  --printers printer_id,model,nozzle_mm,nozzle_type,enclosure,max_nozzle_c,max_bed_c,watts_avg,
             hourly_machine_rate_usd,daily_capacity_hrs,status,loaded_materials
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import types
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))


def _make_fallback_bc():
    """Minimal inline stand-in for lib/bot_common.py so the script also works standalone."""
    m = types.SimpleNamespace()
    m.SEV = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    m.EXIT_OK, m.EXIT_MEDIUM, m.EXIT_HIGH, m.EXIT_CRITICAL, m.EXIT_USAGE, m.EXIT_DEGRADED = 0, 1, 2, 3, 10, 11
    sev_exit = {"MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    def utc_now():
        return datetime.datetime.now(datetime.timezone.utc)

    class Finding:
        def __init__(self, sev, code, msg, evidence=None, key=None):
            if sev not in m.SEV:
                raise ValueError("bad severity %r" % (sev,))
            self.sev, self.code, self.msg, self.evidence, self.key = sev, code, msg, evidence or {}, key or code

        def to_dict(self):
            return {"sev": self.sev, "code": self.code, "key": self.key, "msg": self.msg, "evidence": self.evidence}

        @classmethod
        def from_dict(cls, d):
            return cls(d["sev"], d["code"], d.get("msg", ""), d.get("evidence"), d.get("key"))

        def __repr__(self):
            return "Finding(%s %s)" % (self.sev, self.key)

    def sort_findings(findings):
        return sorted(findings, key=lambda f: (-m.SEV[f.sev], f.code, f.key))

    def exit_code(findings, degraded=False, fail_on="medium"):
        threshold = m.SEV[fail_on.upper()]
        worst = max(findings, key=lambda f: m.SEV[f.sev], default=None)
        if worst is not None and m.SEV[worst.sev] >= threshold and worst.sev in sev_exit:
            return sev_exit[worst.sev]
        return 11 if degraded else 0

    def snapshot_header(tool, endpoints=None, fallbacks=None, now=None):
        now = now or utc_now()
        lines = ["snapshot   %s | tool=%s" % (now.strftime("%Y-%m-%d %H:%M:%S UTC"), tool)]
        if endpoints:
            lines.append("endpoints  " + ", ".join(sorted(endpoints)))
        if fallbacks:
            lines.append("fallbacks  " + ", ".join(fallbacks))
        return "\n".join(lines)

    class StateStore:
        def __init__(self, path):
            self.path = path

        def load(self):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                return {"schema": 1, "items": {}}
            if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("items"), dict):
                return {"schema": 1, "items": {}}
            return doc

        def get(self, name):
            return self.load()["items"].get(name)

        def put(self, name, snapshot):
            doc = self.load()
            doc["items"][name] = snapshot
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    m.utc_now, m.Finding, m.sort_findings, m.exit_code = utc_now, Finding, sort_findings, exit_code
    m.snapshot_header, m.StateStore = snapshot_header, StateStore
    return m




try:
    import bot_common as bc  # noqa: E402
except ImportError:
    bc = _make_fallback_bc()

TOOL = "print-farm-ops"
VERSION = "1.0.0"
STATE_KEY = "print-farm-ops"
SCOPE_NOTE = ("SCOPE_NOTE: 100% offline, local, read-only inspection of the .3mf / .gcode.3mf / .gcode files and the "
              "spool, order and printer tables you supply. No network request is made (no Bambu Cloud, MQTT, Moonraker, "
              "Klipper, OctoPrint or PrusaLink call), no G-code is sent, no print is started, stopped or heated, and it never "
              "modifies or overwrites any .3mf or .gcode file in place; --queue-csv only writes a new CSV and "
              "refuses .3mf / .gcode targets and any input path. Thermal and structural findings are heuristic checks of "
              "slicer metadata, not mechanical-engineering certification and not a substitute for hardware fire-safety "
              "protection (thermal-runaway cut-offs, smoke / heat detection, an unattended-print policy). Cost, margin "
              "and reorder figures are arithmetic on the numbers you supply, not financial, tax or accounting advice. "
              "Credentials, API keys and printer LAN access codes are refused and never read or echoed.")

SEV_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
MAX_FILE_BYTES = 5_000_000          # spool / order / printer tables, state file
MAX_ROWS = 5000
MAX_DISCOVERED = 500
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build"}
# ZIP hardening limits (read at call time so tests can lower them)
MAX_ENTRIES = 500
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_RATIO = 100.0
MIN_RATIO_BYTES = 64 * 1024         # tiny entries can legitimately compress >100:1
MEMBER_CAP = 8 * 1024 * 1024        # settings / xml members are read up to this many bytes
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 512 * 1024
HARDENED = {"hardened_steel", "tungsten_carbide", "diamond"}
NOZZLE_TYPES = ("hardened_steel", "brass", "stainless_steel", "tungsten_carbide", "diamond", "unknown")
DEFAULT_CAPACITY_HRS = 20.0
LOW_STOCK_HIGH_G = 150.0


class UsageError(Exception):
    pass


class RowError(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def visible(s, limit=100):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s))
    return s if len(s) <= limit else s[:limit - 3] + "..."


def sha16(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def clean(o):
    """Make evidence JSON-safe: non-finite floats become None, sets become sorted lists."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (set, frozenset)):
        return [clean(v) for v in sorted(o, key=str)]
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def mk(sev, code, msg, key=None, **ev):
    return bc.Finding(sev, code, msg, clean(ev), key=key or code)


def fnum(v):
    """float or None (blank); ValueError on anything else, including nan / inf."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip()
        if not s:
            return None
        s = s.lstrip("$").rstrip("%").strip()
        x = float(s)
    if not math.isfinite(x):
        raise ValueError("not finite")
    return x


def truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "t")


def tristate(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "n", "off", "f", "none"):
        return False
    return truthy(v)


def r2(x):
    return None if x is None else (x if not isinstance(x, float) or not math.isfinite(x) else round(x, 2))


def fd(x, nd=1):
    """Format a number for reports; None -> '-', inf -> 'inf'."""
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    if isinstance(x, float):
        return ("%." + str(nd) + "f") % x
    return str(x)


def usd(x):
    return "-" if x is None else ("-$%.2f" % -x if x < 0 else "$%.2f" % x)


def norm_key(k):
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


# ---------------------------------------------------------------- credential / access-code refusal
SECRET_PATTERNS = (
    ("OpenAI/Anthropic-style API key (sk-)", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}")),
    ("WattCoin key (wck_)", re.compile(r"(?<![A-Za-z0-9])wck_[A-Za-z0-9]{6,}")),
    ("WattCoin license key (wcl_)", re.compile(r"(?<![A-Za-z0-9])wcl_[A-Za-z0-9]{6,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{12,})")),
    ("JWT / bearer token (eyJ)", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}")),
    ("64-hex private key", re.compile(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")),
    ("cloud access key", re.compile(r"AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9\-]{10,}")),
)
_CRED_NAMES = (r"lan[_\- ]?access[_\- ]?code|access[_\- ]?code|octoprint[_\- ]?api[_\- ]?key|moonraker[_\- ]?(?:api[_\- ]?)?key|"
               r"printhost[_\- ]?(?:api[_\- ]?key|apikey|password)|api[_\- ]?key|apikey|api[_\- ]?token|auth[_\- ]?token|"
               r"bearer[_\- ]?token|private[_\- ]?key|seed[_\- ]?phrase|mnemonic|passwd|password")
CRED_FLAG_RE = re.compile(r"^-{0,2}(?:%s)$" % _CRED_NAMES, re.I)
CRED_ASSIGN_RE = re.compile(r"(?:^|[\s,;&?\"'])-{0,2}(?:%s)\s*[=:]\s*\S" % _CRED_NAMES, re.I)
SEED_WORD_RE = re.compile(r"^[a-z]{3,8}$")


def _is_seed_phrase(s):
    words = s.split()
    return len(words) in (12, 15, 18, 21, 24) and all(SEED_WORD_RE.match(w) for w in words)


def secret_kind(value, allow_seed=True):
    """Name of the credential class `value` looks like, else None. The value itself is never returned."""
    s = str(value)
    for kind, rx in SECRET_PATTERNS:
        if rx.search(s):
            return kind
    if CRED_FLAG_RE.match(s.strip()) or CRED_ASSIGN_RE.search(s):
        return "printer access code / API key assignment"
    if allow_seed and _is_seed_phrase(s.strip()):
        return "seed phrase"
    return None


def scan_argv(argv):
    """-> (1-based index, kind) of the first credential-looking CLI token, else None."""
    run = 0
    for i, tok in enumerate(argv):
        kind = secret_kind(tok)
        if kind:
            return i + 1, kind
        run = run + 1 if SEED_WORD_RE.match(tok) else 0
        if run >= 12:
            return i + 1, "seed phrase"
    return None


def scan_rows(rows, label):
    for n, row in enumerate(rows, 1):
        for k, v in row.items():
            if v is None or v == "" or isinstance(v, (int, float, bool)):
                continue
            if CRED_FLAG_RE.match(str(k)) and str(v).strip():
                raise UsageError("refusing to run: %s row %d has a credential column (%s); value not echoed"
                                 % (label, n, visible(k, 40)))
            kind = secret_kind(v if not isinstance(v, (list, dict)) else json.dumps(v), allow_seed=False)
            if kind:
                raise UsageError("refusing to run: %s row %d column %s looks like a %s; value not echoed"
                                 % (label, n, visible(k, 40), kind))


# ---------------------------------------------------------------- materials
def norm_material(s):
    t = re.sub(r"[\s_]+", "-", str(s or "").strip().upper())
    t = re.sub(r"-?\d{2,3}[AD]$", "", t)       # TPU-95A -> TPU
    t = re.sub(r"\+$", "", t)                  # PLA+ -> PLA
    return re.sub(r"-{2,}", "-", t).strip("-")


_ABRASIVE_RE = re.compile(r"(?<![A-Z])(?:CF|GF)(?![A-Z])|CARBON|GLASS|GLOW|LUMINOUS|PHOSPHOR|METAL|BRONZE|COPPER|STEEL|BRASS|ALUMIN",
                          re.I)
_CFGF_RE = re.compile(r"(?<![A-Z])(?:CF|GF)(?![A-Z])|CARBON|GLASS", re.I)
_ENCLOSED_RE = re.compile(r"^(?:ABS|ASA|PC|PA\d*|PAHT|PPA|PPS)(?:-|$)")
HEAVY_ABRASIVE = {"PA6-CF", "PAHT-CF", "PPA-CF"}


def is_abrasive(*names):
    return any(_ABRASIVE_RE.search(str(n or "").upper().replace("_", " ")) for n in names)


def is_cfgf(*names):
    return any(_CFGF_RE.search(str(n or "").upper().replace("_", " ")) for n in names)


def needs_enclosure(mat):
    return bool(_ENCLOSED_RE.match(norm_material(mat)))


# material: (nozzle_min, nozzle_max, bed_min, bed_max, nozzle_typical, bed_typical)
ENVELOPE = {
    "PLA": (185, 235, 0, 75, 215, 60), "PLA-CF": (190, 240, 0, 75, 220, 60),
    "PETG": (215, 265, 50, 100, 245, 75), "PETG-CF": (230, 275, 55, 100, 255, 80),
    "TPU": (200, 250, 0, 80, 225, 45), "ABS": (230, 285, 80, 115, 260, 95), "ASA": (235, 290, 80, 115, 260, 95),
    "PC": (255, 310, 90, 125, 275, 100), "PA": (240, 290, 50, 100, 270, 80), "PA-CF": (255, 305, 60, 110, 280, 90),
    "PAHT-CF": (270, 315, 80, 115, 290, 100), "PPA-CF": (280, 325, 80, 125, 300, 100),
    "PVA": (180, 225, 0, 75, 200, 55), "BVOH": (180, 230, 0, 75, 210, 55),
}
ENV_ALIAS = {"PA6-CF": "PA-CF", "PA12-CF": "PA-CF", "PA6": "PA", "PA12": "PA", "PA-GF": "PA-CF", "PA6-GF": "PA-CF",
             "PETG-GF": "PETG-CF", "PLA-GF": "PLA-CF", "PC-CF": "PC", "PC-ABS": "ABS"}
DEGRADE_C = {"PLA": 250, "PLA-CF": 250, "TPU": 250, "PVA": 240, "BVOH": 240}
DEFAULT_COST_PER_KG = {"PLA": 20.0, "PLA-CF": 38.0, "PETG": 22.0, "PETG-CF": 42.0, "TPU": 34.0, "ABS": 22.0, "ASA": 28.0,
                       "PC": 40.0, "PA-CF": 85.0, "PAHT-CF": 95.0, "PPA-CF": 110.0, "PVA": 60.0, "BVOH": 80.0}


def lookup_env(mat):
    m = norm_material(mat)
    for cand in (m, ENV_ALIAS.get(m), re.sub(r"-(?:CF|GF)$", "", m)):
        if cand in ENVELOPE:
            return cand, ENVELOPE[cand]
    return None, None


def dry_policy(mat, hygro_flag):
    """(max_days_since_dried, severity) or None when the material needs no drying cadence."""
    m = norm_material(mat)
    if hygro_flag is False:
        return None
    if re.match(r"^(?:PA\d*|PAHT|PPA|PVA|BVOH|PC|TPU)(?:-|$)", m):
        return 7, "HIGH"
    if re.match(r"^(?:PETG|ASA)(?:-|$)", m):
        return 14, "MEDIUM"
    if hygro_flag:
        return 14, "MEDIUM"
    return None


def norm_nozzle_type(*vals):
    for v in vals:
        s = str(v or "").lower()
        if not s:
            continue
        if "harden" in s:
            return "hardened_steel"
        if "tungsten" in s or "carbide" in s:
            return "tungsten_carbide"
        if "diamond" in s or "ruby" in s:
            return "diamond"
        if "stainless" in s:
            return "stainless_steel"
        if "brass" in s:
            return "brass"
    return "unknown"


def norm_pattern(s):
    t = re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    return {"adaptivecubic": "adaptive_cubic", "3dhoneycomb": "3d_honeycomb", "crosshatch": "cross_hatch",
            "zigzag": "rectilinear", "line": "rectilinear", "alignedrectilinear": "rectilinear", "lines": "rectilinear",
            "trihexagon": "triangles", "supportcubic": "cubic"}.get(t, t)


def norm_model(s):
    t = str(s or "").lower()
    t = re.sub(r"bambu\s*lab|bambu|prusa|creality|voron|research|0\.\d+\s*(?:mm)?\s*nozzle|nozzle", "", t)
    t = t.replace("carbon", "c")
    return re.sub(r"[^a-z0-9]", "", t)


_OPEN_FRAME_RE = re.compile(r"(?:\bA1(?:\s*mini)?\b|\bA1M\b|ender[\s\-]*3|\bMK[34]\w*|\bP1P\b|\bSV0\d|\bi3\b)", re.I)
BBL_MODEL_IDS = {"BL-P001": "Bambu Lab X1 Carbon", "BL-P002": "Bambu Lab X1", "BL-P003": "Bambu Lab X1E",
                 "C11": "Bambu Lab P1P", "C12": "Bambu Lab P1S", "N1": "Bambu Lab A1 mini", "N2S": "Bambu Lab A1",
                 "O1D": "Bambu Lab H2D"}


# ---------------------------------------------------------------- table loading
def load_table(path, plural):
    """-> (list of normalised-key dict rows | None, error | None)."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, "file larger than %d bytes" % MAX_FILE_BYTES
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return None, "unreadable (%s)" % visible(e, 100)
    if path.lower().endswith(".json") or text.lstrip()[:1] in ("[", "{"):
        try:
            doc = json.loads(text)
        except ValueError as e:
            return None, "malformed JSON (%s)" % visible(e, 80)
        if isinstance(doc, dict):
            doc = next((doc[k] for k in (plural, "items", "rows", "data") if isinstance(doc.get(k), list)), None)
        if not isinstance(doc, list) or not all(isinstance(x, dict) for x in doc):
            return None, "expected a JSON array of objects (or an object holding one under %r)" % plural
        rows = [{norm_key(k): v for k, v in x.items()} for x in doc]
    else:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return None, "empty CSV"
        try:
            rows = [{norm_key(k): v for k, v in r.items() if k is not None} for r in reader]
        except csv.Error as e:
            return None, "malformed CSV (%s)" % visible(e, 80)
    if len(rows) > MAX_ROWS:
        return None, "more than %d rows" % MAX_ROWS
    return rows, None


def _txt(v):
    return "" if v is None else str(v).strip()


def num(r, key, default=None, lo=0.0, hi=1e7):
    v = r.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        x = fnum(v)
    except (ValueError, TypeError):
        raise RowError("%s=%r is not a number" % (key, visible(v, 30)))
    if x < lo or x > hi:
        raise RowError("%s=%s is outside %g..%g" % (key, fd(x, 2), lo, hi))
    return x


def parse_date(r, key):
    v = _txt(r.get(key))
    if not v or v.lower() in ("none", "null", "n/a", "-", "never"):
        return None
    try:
        d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise RowError("%s=%r is not an ISO date" % (key, visible(v, 30)))
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def parse_spool(r):
    sid, mat = _txt(r.get("spool_id")), norm_material(r.get("material"))
    if not sid:
        raise RowError("missing spool_id")
    if not mat:
        raise RowError("spool %s: missing material" % visible(sid, 30))
    initial = num(r, "initial_g")
    remaining = num(r, "remaining_g", initial)
    if remaining is None:
        raise RowError("spool %s: remaining_g is required" % visible(sid, 30))
    cost_kg = num(r, "cost_per_kg")
    cost_usd = num(r, "cost_usd")
    if cost_kg is None and cost_usd is not None and initial:
        cost_kg = cost_usd / (initial / 1000.0)
    return {"spool_id": sid, "material": mat, "brand": _txt(r.get("brand")), "color": _txt(r.get("color")),
            "initial_g": initial, "remaining_g": remaining, "cost_per_kg": cost_kg, "cost_usd": cost_usd,
            "reorder_threshold_g": num(r, "reorder_threshold_g", 0.0), "dried_at": parse_date(r, "dried_at"),
            "last_desiccant_change": parse_date(r, "last_desiccant_change"),
            "abrasive": tristate(r.get("abrasive")), "hygroscopic": tristate(r.get("hygroscopic")),
            "max_nozzle_c": num(r, "max_nozzle_c"), "min_nozzle_c": num(r, "min_nozzle_c"),
            "bed_min_c": num(r, "bed_min_c"), "bed_max_c": num(r, "bed_max_c"),
            "requires_enclosure": tristate(r.get("requires_enclosure"))}


def parse_order(r):
    sku = _txt(r.get("sku"))
    if not sku:
        raise RowError("missing sku")
    upp = num(r, "units_per_plate", 1.0, lo=1.0)
    return {"sku": sku, "name": _txt(r.get("name")) or sku, "plate_file": _txt(r.get("plate_file")),
            "material": norm_material(r.get("material")), "grams_per_unit": num(r, "grams_per_unit"),
            "print_hours_per_unit": num(r, "print_hours_per_unit"), "units_per_plate": upp,
            "post_proc_mins": num(r, "post_proc_mins", 0.0), "on_hand": num(r, "on_hand", 0.0, lo=-1e7),
            "open_orders": num(r, "open_orders", 0.0), "velocity": num(r, "daily_sales_velocity", 0.0),
            "lead_time_days": num(r, "lead_time_days", 0.0), "safety_stock_days": num(r, "safety_stock_days", 0.0),
            "sale_price_usd": num(r, "sale_price_usd"), "marketplace_fee_pct": num(r, "marketplace_fee_pct", 0.0, hi=100.0),
            "shipping_cost_usd": num(r, "shipping_cost_usd", 0.0), "structural": bool(tristate(r.get("structural")))}


def parse_printer(r):
    pid = _txt(r.get("printer_id"))
    if not pid:
        raise RowError("missing printer_id")
    lm = r.get("loaded_materials")
    if isinstance(lm, str):
        lm = re.split(r"[|;/,]", lm)
    mats = sorted({norm_material(x) for x in (lm or []) if norm_material(x)})
    status = _txt(r.get("status")).lower() or "idle"
    return {"printer_id": pid, "model": _txt(r.get("model")), "nozzle_mm": num(r, "nozzle_mm"),
            "nozzle_type": norm_nozzle_type(r.get("nozzle_type")), "enclosure": bool(tristate(r.get("enclosure"))),
            "max_nozzle_c": num(r, "max_nozzle_c"), "max_bed_c": num(r, "max_bed_c"), "watts_avg": num(r, "watts_avg"),
            "rate_hr": num(r, "hourly_machine_rate_usd"),
            "capacity_hrs": num(r, "daily_capacity_hrs", DEFAULT_CAPACITY_HRS, hi=24.0), "status": status,
            "online": status in ("idle", "printing", "online", "ready", "running"), "loaded_materials": mats}


def parse_rows(rows, parser, idkey, label, degraded):
    out, seen = [], set()
    for n, r in enumerate(rows, 1):
        try:
            item = parser(r)
            if item[idkey] in seen:
                raise RowError("duplicate %s %s" % (idkey, visible(item[idkey], 30)))
            seen.add(item[idkey])
            out.append(item)
        except RowError as e:
            degraded.append("%s row %d skipped: %s" % (label, n, e))
    return out



# ---------------------------------------------------------------- archive hardening
def check_archive(zf):
    """Reasons this archive is unsafe to read (empty list = safe). Nothing is ever extracted to disk."""
    problems = []
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        problems.append("%d entries (limit %d)" % (len(infos), MAX_ENTRIES))
    total = 0
    for zi in infos:
        name = zi.filename
        norm = name.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/") or re.match(r"^[A-Za-z]:", norm):
            problems.append("path traversal entry %r" % visible(name, 60))
        if stat.S_ISLNK((zi.external_attr >> 16) & 0xFFFF):
            problems.append("symlink entry %r" % visible(name, 60))
        if zi.flag_bits & 0x1:
            problems.append("encrypted entry %r" % visible(name, 60))
        if zi.file_size > MAX_ENTRY_BYTES:
            problems.append("entry %r expands to %d bytes (limit %d)" % (visible(name, 60), zi.file_size, MAX_ENTRY_BYTES))
        total += zi.file_size
        if zi.file_size >= MIN_RATIO_BYTES and (zi.compress_size == 0 or zi.file_size / zi.compress_size > MAX_RATIO):
            ratio = "inf" if zi.compress_size == 0 else "%.0f:1" % (zi.file_size / zi.compress_size)
            problems.append("entry %r compression ratio %s (limit %d:1)" % (visible(name, 60), ratio, MAX_RATIO))
    if total > MAX_TOTAL_BYTES:
        problems.append("total uncompressed size %d bytes (limit %d)" % (total, MAX_TOTAL_BYTES))
    return problems[:8]


class _BoundedReader(object):
    """File-like wrapper that caps total bytes and refuses XML DTD / entity declarations (billion-laughs guard)."""

    def __init__(self, fh, cap):
        self.fh, self.left, self.first = fh, cap, True

    def read(self, n=-1):
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.fh.read(n)
        self.left -= len(data)
        if self.first:
            self.first = False
            if re.search(rb"<!(?:DOCTYPE|ENTITY)", data[:65536], re.I):
                raise ValueError("XML DTD / entity declarations are not accepted")
        return data


def read_member(zf, zi, cap=None):
    cap = cap or MEMBER_CAP
    with zf.open(zi, "r") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise ValueError("member %r larger than %d bytes" % (visible(zi.filename, 60), cap))
    return data


def safe_xml(data):
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", data[:65536], re.I):
        raise ValueError("XML DTD / entity declarations are not accepted")
    return ET.fromstring(data)


def head_tail(fh, head=HEAD_BYTES, tail=TAIL_BYTES, limit=None):
    """Stream `fh`, keeping only the first `head` and last `tail` bytes -> (text, total_bytes)."""
    h = fh.read(head)
    buf, total = b"", len(h)
    while True:
        chunk = fh.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if limit is not None and total > limit:
            raise ValueError("stream larger than %d bytes" % limit)
        buf = (buf + chunk)[-tail:]
    t = buf.decode("utf-8", "replace")
    if total > head + tail and "\n" in t:
        t = t.split("\n", 1)[1]
    return h.decode("utf-8", "replace") + "\n" + t, total


# ---------------------------------------------------------------- slicer config parsing
_KV_RE = re.compile(r"^;\s*([a-z][a-z0-9_]*)\s*=\s*(.*?)\s*$", re.M)


def parse_kv(text):
    """`; key = value` comment lines (PrusaSlicer / SuperSlicer / OrcaSlicer / Bambu config blocks, Slic3r_PE.config)."""
    kv = {}
    for m in _KV_RE.finditer(text):
        kv.setdefault(m.group(1), m.group(2))
    return kv


def parse_ini(text):
    kv = {}
    for line in text.splitlines():
        m = re.match(r"^\s*;?\s*([a-z][a-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            kv.setdefault(m.group(1), m.group(2))
    return kv


def as_list(v, sep=r"[;,]"):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip().strip('"') for x in v]
    return [p.strip().strip('"') for p in re.split(sep, str(v)) if p.strip()]


def as_nums(v):
    out = []
    for p in as_list(v, r"[;,\s]+"):
        try:
            out.append(fnum(p))
        except ValueError:
            out.append(None)
    return out


def first_num(v):
    for x in as_nums(v):
        if x is not None:
            return x
    return None


def pick(cfg, *keys):
    for k in keys:
        v = cfg.get(k)
        if v is not None and v != "" and v != []:
            return v
    return None


def parse_duration_hrs(s):
    if not s:
        return None
    m = re.fullmatch(r"\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*", s)
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 24 + h + mi / 60.0 + sec / 3600.0


def parse_gcode_header(text):
    """Slicer name + time / filament totals from the free-form comment lines of a G-code head/tail."""
    out = {}
    m = re.search(r"generated by (PrusaSlicer|SuperSlicer|Slic3r\w*)\s+([\w.+\-]+)", text, re.I)
    if m:
        out["slicer"] = m.group(1)
    else:
        m = re.search(r"^;\s*(OrcaSlicer|BambuStudio|Bambu Studio)\b", text, re.M)
        if m:
            out["slicer"] = m.group(1).replace(" ", "")
        elif re.search(r"^;\s*Generated with Cura", text, re.M | re.I):
            out["slicer"] = "Cura"
    m = (re.search(r"total estimated time:\s*([0-9dhms ]+)", text)
         or re.search(r"estimated printing time \([^)]*\)\s*=\s*([0-9dhms ]+)", text))
    if m:
        out["hours"] = parse_duration_hrs(m.group(1).strip())
    elif re.search(r"^;TIME:(\d+)", text, re.M):
        out["hours"] = int(re.search(r"^;TIME:(\d+)", text, re.M).group(1)) / 3600.0
    m = re.search(r"total filament weight \[g\]\s*:\s*([\d.]+)", text) or re.search(r"total filament used \[g\]\s*=\s*([\d.]+)", text)
    per = re.search(r"^;\s*filament used \[g\]\s*=\s*([\d.,\s]+)$", text, re.M)
    if per:
        out["per_filament_g"] = [x if x is not None else 0.0 for x in as_nums(per.group(1))]
    if m:
        out["grams"] = float(m.group(1))
    elif per:
        out["grams"] = sum(out["per_filament_g"])
    m = re.search(r"total filament length \[mm\]\s*:\s*([\d.]+)", text)
    per_mm = re.search(r"^;\s*filament used \[mm\]\s*=\s*([\d.,\s]+)$", text, re.M)
    if m:
        out["meters"] = float(m.group(1)) / 1000.0
    elif per_mm:
        out["meters"] = sum(x or 0.0 for x in as_nums(per_mm.group(1))) / 1000.0
    else:
        m = re.search(r"^;Filament used:\s*([\d.]+)m", text, re.M)
        if m:
            out["meters"] = float(m.group(1))
    return out


def slicer_from_app(s):
    s = str(s or "")
    for name in ("OrcaSlicer", "BambuStudio", "PrusaSlicer", "SuperSlicer", "Slic3r", "Cura"):
        if name.lower() in s.lower().replace(" ", ""):
            return name
    return None


def bed_temps(cfg):
    """Per-filament bed temperature list for the active plate type (Orca / Bambu) or `bed_temperature` (Prusa)."""
    bt = str(pick(cfg, "curr_bed_type") or "").lower()
    order = []
    if "cool" in bt:
        order = ["cool_plate_temp"]
    elif "engineering" in bt:
        order = ["eng_plate_temp"]
    elif "textured" in bt:
        order = ["textured_plate_temp"]
    elif "smooth" in bt or "high temp" in bt or "hot" in bt:
        order = ["hot_plate_temp"]
    for k in order + ["bed_temperature"]:
        if pick(cfg, k) is not None:
            return as_nums(cfg[k])
    allp = [as_nums(cfg[k]) for k in ("cool_plate_temp", "eng_plate_temp", "hot_plate_temp", "textured_plate_temp")
            if pick(cfg, k) is not None]
    if not allp:
        return []
    n = max(len(a) for a in allp)
    return [max([a[i] for a in allp if i < len(a) and a[i] is not None] or [None]) for i in range(n)]


def extract_profile(cfg):
    """Normalise an Orca/Bambu JSON dict or a Prusa-style key/value dict into one profile."""
    types_ = [norm_material(t) for t in as_list(pick(cfg, "filament_type"), r";")]
    ids = as_list(pick(cfg, "filament_settings_id", "filament_id_name"), r";")
    n_main = as_nums(pick(cfg, "nozzle_temperature", "temperature"))
    n_init = as_nums(pick(cfg, "nozzle_temperature_initial_layer", "first_layer_temperature"))
    nozzle = [max([x for x in (a, b) if x is not None] or [None]) if any(x is not None for x in (a, b)) else None
              for a, b in zip(n_main + [None] * max(0, len(n_init) - len(n_main)), n_init + [None] * max(0, len(n_main) - len(n_init)))]
    dens = pick(cfg, "sparse_infill_density", "fill_density")
    try:
        dens = None if dens is None else fnum(dens)
    except ValueError:
        dens = None
    brim = pick(cfg, "brim_type")
    if brim is None and pick(cfg, "brim_width") is not None:
        brim = "brim" if (first_num(cfg["brim_width"]) or 0) > 0 else "none"
    support = pick(cfg, "enable_support", "support_material")
    sup_type = pick(cfg, "support_type", "support_material_style")
    model = pick(cfg, "printer_model", "printer_settings_id", "printer_model_id")
    if isinstance(model, list):
        model = model[0] if model else None
    model = BBL_MODEL_IDS.get(str(model), model)
    ntype = norm_nozzle_type(*as_list(pick(cfg, "nozzle_type"), r";")[:1], pick(cfg, "printer_settings_id"), pick(cfg, "printer_model"))
    return {
        "printer_model": visible(model, 60) if model else None,
        "nozzle_diameter_mm": first_num(pick(cfg, "nozzle_diameter")),
        "nozzle_type": ntype,
        "layer_height_mm": first_num(pick(cfg, "layer_height")),
        "initial_layer_height_mm": first_num(pick(cfg, "initial_layer_print_height", "first_layer_height")),
        "wall_loops": first_num(pick(cfg, "wall_loops", "perimeters")),
        "top_shell_layers": first_num(pick(cfg, "top_shell_layers", "top_solid_layers")),
        "bottom_shell_layers": first_num(pick(cfg, "bottom_shell_layers", "bottom_solid_layers")),
        "infill_density_pct": dens,
        "infill_pattern": norm_pattern(pick(cfg, "sparse_infill_pattern", "fill_pattern")) or None,
        "support_enabled": None if support is None else truthy(as_list(support)[0] if as_list(support) else support),
        "support_type": visible(sup_type, 40) if sup_type else None,
        "brim_type": visible(as_list(brim, r";")[0], 40) if brim else None,
        "chamber_temp_c": first_num(pick(cfg, "chamber_temperature", "chamber_temperatures")),
        "slots": {"types": types_, "ids": ids, "nozzle": nozzle, "bed": bed_temps(cfg),
                  "mvs": as_nums(pick(cfg, "filament_max_volumetric_speed")),
                  "fan": as_nums(pick(cfg, "fan_max_speed", "max_fan_speed"))},
    }


def _slot(lst, i):
    if not lst:
        return None
    return lst[i] if i < len(lst) else lst[0]


def build_filament(profile, i, ftype=None, grams=None, meters=None):
    s = profile["slots"]
    t = norm_material(ftype) if ftype else (_slot(s["types"], i) or "")
    return {"slot": i + 1, "type": t, "settings_id": _slot(s["ids"], i) or "", "nozzle_temp_c": _slot(s["nozzle"], i),
            "bed_temp_c": _slot(s["bed"], i), "max_volumetric_speed_mm3s": _slot(s["mvs"], i),
            "fan_max_pct": _slot(s["fan"], i), "grams": grams, "meters": meters}


TRACKED = ("printer_model", "nozzle_diameter_mm", "nozzle_type", "layer_height_mm", "wall_loops", "top_shell_layers",
           "bottom_shell_layers", "infill_density_pct", "infill_pattern", "support_enabled", "filament_type",
           "nozzle_temp_c", "bed_temp_c")


def finish_plate(profile, idx, fils, extra):
    """Flat per-plate record: project profile + per-plate usage."""
    fils = fils or [build_filament(profile, 0)]
    temps = [f["nozzle_temp_c"] for f in fils if f["nozzle_temp_c"] is not None]
    beds = [f["bed_temp_c"] for f in fils if f["bed_temp_c"] is not None]
    mvs = [f["max_volumetric_speed_mm3s"] for f in fils if f["max_volumetric_speed_mm3s"] is not None]
    fans = [f["fan_max_pct"] for f in fils if f["fan_max_pct"] is not None]
    p = {k: v for k, v in profile.items() if k != "slots"}
    p.update({"index": idx, "filaments": fils, "filament_type": fils[0]["type"] or None,
              "filament_types": [f["type"] for f in fils], "nozzle_temp_c": max(temps) if temps else None,
              "bed_temp_c": max(beds) if beds else None, "max_volumetric_speed_mm3s": max(mvs) if mvs else None,
              "fan_max_pct": max(fans) if fans else None})
    p.update(extra)
    p["settings_hash"] = sha16(dict({k: p.get(k) for k in TRACKED}, filament_types=p["filament_types"]))
    return p


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_record(path):
    return {"path": path, "key": os.path.abspath(path), "kind": None, "sha256": None, "size": None, "slicer": None,
            "plates": [], "problems": [], "archive_violation": None, "error": None, "credential_keys": [],
            "settings_source": None}


def _credential_keys(cfg):
    return sorted(k for k in ("printhost_apikey", "printhost_password", "octoprint_apikey", "moonraker_api_key")
                  if str(cfg.get(k) or "").strip().strip('"'))


def inspect_gcode(path, rec):
    rec["kind"] = "gcode"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
            fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            tail = fh.read(TAIL_BYTES) if size > HEAD_BYTES else b""
        rec["size"], rec["sha256"] = size, file_sha256(path)
    except OSError as e:
        rec["error"] = "unreadable (%s)" % visible(e, 100)
        return rec
    text = head.decode("utf-8", "replace") + "\n" + tail.decode("utf-8", "replace")
    hdr, cfg = parse_gcode_header(text), parse_kv(text)
    profile = extract_profile(cfg)
    rec["slicer"] = hdr.get("slicer")
    rec["settings_source"] = "gcode comments" if cfg else None
    rec["credential_keys"] = _credential_keys(cfg)
    per = hdr.get("per_filament_g")
    used = [i for i, g in enumerate(per or []) if g and g > 0] or [0]
    fils = [build_filament(profile, i, grams=(per[i] if per and i < len(per) else None)) for i in used]
    if len(used) == 1 and fils[0]["grams"] is None:
        fils[0]["grams"] = hdr.get("grams")
    rec["plates"].append(finish_plate(profile, 1, fils, {
        "slicer": rec["slicer"], "sliced": "hours" in hdr or "grams" in hdr, "estimated_time_hrs": hdr.get("hours"),
        "filament_grams": hdr.get("grams"), "filament_meters": hdr.get("meters"), "object_count": None}))
    if not cfg:
        rec["problems"].append("no slicer settings block found in G-code head/tail")
    return rec


def inspect_3mf(path, rec):
    rec["kind"] = "3mf"
    try:
        rec["size"] = os.path.getsize(path)
        rec["sha256"] = file_sha256(path)
        zf = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile, NotImplementedError, EOFError) as e:
        rec["error"] = "not a readable 3MF/ZIP archive (%s)" % visible(e, 100)
        return rec
    with zf:
        bad = check_archive(zf)
        if bad:
            rec["archive_violation"] = bad
            return rec
        members = {}
        for zi in zf.infolist():
            if not zi.filename.endswith("/"):
                members.setdefault(zi.filename.lower(), zi)

        def get(name):
            zi = members.get(name.lower())
            if zi is None:
                return None
            try:
                return read_member(zf, zi)
            except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, EOFError) as e:
                rec["problems"].append("%s: %s" % (name, visible(e, 100)))
                return None

        cfg, source = {}, None
        raw = get("Metadata/project_settings.config")
        if raw is not None:
            try:
                doc = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(doc, dict):
                    cfg, source = doc, "project_settings.config"
            except ValueError:
                cfg = parse_ini(raw.decode("utf-8", "replace"))
                source = "project_settings.config (key/value)" if cfg else None
                if not cfg:
                    rec["problems"].append("Metadata/project_settings.config is not valid JSON")
        raw = get("Metadata/Slic3r_PE.config") if not cfg else None
        if raw is not None:
            cfg = parse_ini(raw.decode("utf-8", "replace"))
            source = "Slic3r_PE.config" if cfg else None
        app = None
        model_objects = 0
        zi = members.get("3d/3dmodel.model")
        if zi is not None:
            try:
                with zf.open(zi, "r") as fh:
                    for ev, el in ET.iterparse(_BoundedReader(fh, MAX_ENTRY_BYTES), events=("start", "end")):
                        tag = el.tag.rsplit("}", 1)[-1]
                        if ev == "start" and tag == "item":
                            model_objects += 1
                        elif ev == "end":
                            if tag == "metadata" and el.get("name") == "Application":
                                app = el.text
                            if tag in ("vertex", "triangle"):
                                el.clear()
            except (ET.ParseError, ValueError, OSError, RuntimeError, zipfile.BadZipFile, EOFError) as e:
                rec["problems"].append("3D/3dmodel.model: %s" % visible(e, 100))
        plates = {}   # index -> dict(gcode=..., info=..., objects=n)
        header = {}
        raw = get("Metadata/slice_info.config")
        if raw is not None:
            try:
                root = safe_xml(raw)
                for el in root.iter("header_item"):
                    header[el.get("key")] = el.get("value")
                for pe in root.iter("plate"):
                    md = {m.get("key"): m.get("value") for m in pe.findall("metadata")}
                    try:
                        idx = int(md.get("index", len(plates) + 1))
                    except ValueError:
                        idx = len(plates) + 1
                    objs = [o for o in pe.findall("object") if str(o.get("skipped", "false")).lower() != "true"]
                    fl = [{k: f.get(k) for k in ("id", "type", "used_m", "used_g")} for f in pe.findall("filament")]
                    plates[idx] = {"md": md, "objects": len(objs), "filaments": fl,
                                   "warnings": [visible(w.get("msg"), 60) for w in pe.findall("warning")]}
            except (ET.ParseError, ValueError) as e:
                rec["problems"].append("Metadata/slice_info.config: %s" % visible(e, 100))
        raw = get("Metadata/model_settings.config")
        ms_plates = set()
        if raw is not None:
            try:
                for pe in safe_xml(raw).iter("plate"):
                    for m in pe.findall("metadata"):
                        if m.get("key") == "plater_id":
                            ms_plates.add(int(m.get("value")))
            except (ET.ParseError, ValueError, TypeError) as e:
                rec["problems"].append("Metadata/model_settings.config: %s" % visible(e, 100))
        gcodes = {}
        for lname, zi in members.items():
            m = re.fullmatch(r"metadata/plate_(\d+)\.gcode", lname)
            if m:
                try:
                    with zf.open(zi, "r") as fh:
                        text, _ = head_tail(fh, limit=MAX_ENTRY_BYTES)
                    gcodes[int(m.group(1))] = text
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError) as e:
                    rec["problems"].append("%s: %s" % (zi.filename, visible(e, 100)))
        if not cfg and gcodes:
            first = gcodes[sorted(gcodes)[0]]
            cfg = parse_kv(first)
            source = "plate gcode config block" if cfg else None
        rec["settings_source"] = source
        rec["credential_keys"] = _credential_keys(cfg)
        profile = extract_profile(cfg)
        if not profile["printer_model"]:
            mid = next((p["md"].get("printer_model_id") for p in plates.values() if p["md"].get("printer_model_id")), None)
            profile["printer_model"] = BBL_MODEL_IDS.get(mid, mid)
        if profile["nozzle_diameter_mm"] is None:
            for p in plates.values():
                v = first_num(p["md"].get("nozzle_diameters"))
                if v is not None:
                    profile["nozzle_diameter_mm"] = v
                    break
        rec["slicer"] = (slicer_from_app(app) or ("BambuStudio" if header else None)
                         or next((parse_gcode_header(t).get("slicer") for t in gcodes.values() if parse_gcode_header(t).get("slicer")), None)
                         or ("PrusaSlicer" if source == "Slic3r_PE.config" else None))
        if not cfg:
            rec["problems"].append("no readable slicer settings found")
        indices = sorted(set(plates) | set(gcodes) | ms_plates) or [1]
        for idx in indices:
            info, hdr = plates.get(idx), parse_gcode_header(gcodes[idx]) if idx in gcodes else {}
            md = info["md"] if info else {}
            fils = []
            for f in (info["filaments"] if info else []):
                try:
                    slot = int(f["id"]) - 1
                except (TypeError, ValueError):
                    continue
                fils.append(build_filament(profile, slot, f.get("type"), _f(f.get("used_g")), _f(f.get("used_m"))))
            if not fils and hdr.get("per_filament_g"):
                fils = [build_filament(profile, i, grams=g) for i, g in enumerate(hdr["per_filament_g"]) if g]
            hrs = (_f(md.get("prediction")) / 3600.0) if _f(md.get("prediction")) is not None else hdr.get("hours")
            grams = _f(md.get("weight"))
            if grams is None:
                grams = hdr.get("grams")
            meters = sum(f["meters"] for f in fils if f["meters"] is not None) if any(f["meters"] is not None for f in fils) else hdr.get("meters")
            rec["plates"].append(finish_plate(profile, idx, fils, {
                "slicer": rec["slicer"], "sliced": hrs is not None or grams is not None, "estimated_time_hrs": hrs,
                "filament_grams": grams, "filament_meters": meters,
                "object_count": info["objects"] if info else (model_objects or None),
                "slicer_warnings": info["warnings"] if info else []}))
    return rec


def _f(v):
    try:
        return fnum(v)
    except (ValueError, TypeError):
        return None


def inspect_file(path):
    rec = new_record(path)
    if path.lower().endswith(".3mf"):
        return inspect_3mf(path, rec)
    return inspect_gcode(path, rec)


def is_slicer_file(name):
    return name.lower().endswith((".3mf", ".gcode"))


def discover(root):
    out = []
    for dp, dns, fns in os.walk(root, followlinks=False):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            if is_slicer_file(fn) and not os.path.islink(p):
                out.append(p)
                if len(out) >= MAX_DISCOVERED:
                    return out, True
    return out, False



# ---------------------------------------------------------------- shared context helpers
def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def match_printers(model, nozzle, printers):
    key = norm_model(model)
    if not key:
        return []
    cands = []
    for p in printers:
        pk = norm_model(p["model"])
        if pk and (pk == key or (min(len(pk), len(key)) >= 4 and (pk in key or key in pk))):
            if nozzle is None or p["nozzle_mm"] is None or abs(p["nozzle_mm"] - nozzle) < 0.005:
                cands.append(p)
    exact = [p for p in cands if norm_model(p["model"]) == key]
    return exact or cands


class Ctx(object):
    """Lookup state shared by the audits (printers / spools tables, SKU links, CLI economics)."""

    def __init__(self, args, printers, spools, now):
        self.args, self.printers, self.spools, self.now = args, printers, spools, now
        self.structural = {}

    def spool_flag(self, mat, field):
        vals = [s[field] for s in self.spools if s["material"] == mat and s[field] is not None]
        return any(vals)

    def envelope(self, mat):
        name, env = lookup_env(mat)
        if env is None:
            return None, None
        nmin, nmax, bmin, bmax, ntyp, btyp = env
        sp = [s for s in self.spools if s["material"] == norm_material(mat)]
        nmin = min([s["min_nozzle_c"] for s in sp if s["min_nozzle_c"] is not None] or [nmin])
        nmax = max([s["max_nozzle_c"] for s in sp if s["max_nozzle_c"] is not None] or [nmax])
        bmin = min([s["bed_min_c"] for s in sp if s["bed_min_c"] is not None] or [bmin])
        bmax = max([s["bed_max_c"] for s in sp if s["bed_max_c"] is not None] or [bmax])
        return name, (nmin, nmax, bmin, bmax, ntyp, btyp)

    def printer_ctx(self, pl):
        cands = match_printers(pl.get("printer_model"), pl.get("nozzle_diameter_mm"), self.printers)
        ntype, src, encl = pl.get("nozzle_type") or "unknown", "slicer file", None
        if ntype == "unknown" and cands:
            ts = {c["nozzle_type"] for c in cands}
            if len(ts) == 1 and ts != {"unknown"}:
                ntype, src = next(iter(ts)), "printers table"
        if cands:
            es = {c["enclosure"] for c in cands}
            if len(es) == 1:
                encl = next(iter(es))
        return ntype, src, encl

    def cost_per_kg(self, mat):
        live = [s["cost_per_kg"] for s in self.spools if s["material"] == mat and s["cost_per_kg"] is not None and s["remaining_g"] > 0]
        allc = [s["cost_per_kg"] for s in self.spools if s["material"] == mat and s["cost_per_kg"] is not None]
        c = _mean(live or allc)
        if c is not None:
            return c, "spool ledger"
        return DEFAULT_COST_PER_KG.get(mat) or DEFAULT_COST_PER_KG.get(ENV_ALIAS.get(mat, ""), 25.0), "default estimate"


# ---------------------------------------------------------------- bucket 1: slicer profile & safety
def plate_findings(rec, pl, ctx):
    out = []

    def add(code, sub, sev, msg, **ev):
        out.append((code, sub, sev, msg, ev))

    nozzle, layer = pl.get("nozzle_diameter_mm"), pl.get("layer_height_mm")
    ntype, nsrc, encl = ctx.printer_ctx(pl)
    structural = ctx.structural.get((rec["key"], pl["index"]), False)
    cfgf_any = False
    for f in pl["filaments"]:
        t = norm_material(f["type"]) or "UNKNOWN"
        n, b = f["nozzle_temp_c"], f["bed_temp_c"]
        abr = is_abrasive(t, f["settings_id"]) or ctx.spool_flag(t, "abrasive")
        cfgf = is_cfgf(t, f["settings_id"])
        cfgf_any = cfgf_any or cfgf
        lim = DEGRADE_C.get(t)
        n_crit = n is not None and n > 0 and (n > 320 or n < 170 or (lim is not None and n > lim))
        b_crit = b is not None and b > 130
        if n is not None and n > 0 and (n > 320 or n < 170):
            add("THERMAL_RUNAWAY_OR_OUT_OF_RANGE", "nozzle-range:" + t, "CRITICAL",
                "%s nozzle temperature %g C is outside the 170-320 C hotend range" % (t, n), material=t, nozzle_temp_c=n)
        elif n_crit:
            add("THERMAL_RUNAWAY_OR_OUT_OF_RANGE", "nozzle-degrade:" + t, "CRITICAL",
                "%s sliced at %g C exceeds its %g C degradation limit (charring, heat-creep jams, fumes)" % (t, n, lim),
                material=t, nozzle_temp_c=n, limit_c=lim)
        if b_crit:
            add("THERMAL_RUNAWAY_OR_OUT_OF_RANGE", "bed:" + t, "CRITICAL",
                "%s bed temperature %g C exceeds the 130 C ceiling" % (t, b), material=t, bed_temp_c=b)
        name, env = ctx.envelope(t)
        if env:
            nmin, nmax, bmin, bmax = env[:4]
            bad = []
            if n is not None and n > 0 and not n_crit and not (nmin <= n <= nmax):
                bad.append("nozzle %g C (envelope %g-%g C)" % (n, nmin, nmax))
            if b is not None and not b_crit and not (bmin <= b <= bmax):
                bad.append("bed %g C (envelope %g-%g C)" % (b, bmin, bmax))
            if bad:
                add("TEMP_MATERIAL_MISMATCH", "temps:" + t, "HIGH",
                    "%s sliced with %s; the filament preset was probably not switched" % (t, " and ".join(bad)),
                    material=t, nozzle_temp_c=n, bed_temp_c=b, envelope=[nmin, nmax, bmin, bmax])
        if abr:
            if ntype in ("brass", "stainless_steel"):
                add("ABRASIVE_FILAMENT_BRASS_NOZZLE", "abr:" + t, "CRITICAL",
                    "abrasive filament %s sliced for a %s nozzle (%s); it will wear through it, use a hardened / tungsten nozzle"
                    % (t, ntype.replace("_", " "), nsrc), material=t, nozzle_type=ntype)
            elif ntype == "unknown":
                add("ABRASIVE_FILAMENT_BRASS_NOZZLE", "abr:" + t, "HIGH",
                    "abrasive filament %s sliced for a nozzle whose material is unknown; confirm it is hardened" % t,
                    material=t, nozzle_type=ntype)
            if nozzle is not None:
                if nozzle < 0.4 - 0.005:
                    add("ABRASIVE_CLOG_RISK_SMALL_NOZZLE", "small:" + t, "HIGH",
                        "abrasive %s through a %g mm nozzle clogs; use 0.4 mm minimum, 0.6 mm hardened preferred" % (t, nozzle),
                        material=t, nozzle_diameter_mm=nozzle)
                elif abs(nozzle - 0.4) <= 0.005 and t in HEAVY_ABRASIVE:
                    add("ABRASIVE_CLOG_RISK_SMALL_NOZZLE", "small:" + t, "MEDIUM",
                        "heavily filled %s on a 0.4 mm nozzle clogs often; a 0.6 mm hardened nozzle is recommended" % t,
                        material=t, nozzle_diameter_mm=nozzle)
        if needs_enclosure(t) or ctx.spool_flag(t, "requires_enclosure"):
            open_frame = (not encl) if encl is not None else bool(_OPEN_FRAME_RE.search(pl.get("printer_model") or ""))
            if open_frame:
                add("ENCLOSURE_REQUIRED_OPEN_FRAME", "encl:" + t, "HIGH",
                    "%s sliced for open-frame %s: warp / delamination risk and VOC emissions without an enclosure"
                    % (t, pl.get("printer_model") or "printer"), material=t, printer_model=pl.get("printer_model"))
        mvs = f["max_volumetric_speed_mm3s"]
        if t.startswith("TPU") and mvs is not None and mvs > 6.0:
            add("TPU_HIGH_VOLUMETRIC_SPEED", "tpu", "MEDIUM",
                "TPU max volumetric speed %g mm3/s exceeds 6.0 (extruder gear buckling / jams)" % mvs,
                material=t, max_volumetric_speed_mm3s=mvs)
    if nozzle and layer:
        if layer > 0.8 * nozzle + 1e-9:
            add("LAYER_HEIGHT_EXCEEDS_NOZZLE_LIMIT", "tall", "HIGH",
                "layer height %g mm is above 80%% of the %g mm nozzle (%.3f mm): weak layer bonding / delamination"
                % (layer, nozzle, 0.8 * nozzle), layer_height_mm=layer, nozzle_diameter_mm=nozzle)
        elif cfgf_any and layer < 0.15 * nozzle - 1e-9:
            add("LAYER_HEIGHT_EXCEEDS_NOZZLE_LIMIT", "thin", "MEDIUM",
                "layer height %g mm is below 15%% of the %g mm nozzle on CF/GF filament: clog risk"
                % (layer, nozzle), layer_height_mm=layer, nozzle_diameter_mm=nozzle)
    load_bearing = cfgf_any or structural
    pat, dens, walls = pl.get("infill_pattern"), pl.get("infill_density_pct"), pl.get("wall_loops")
    if load_bearing and pat in ("grid", "rectilinear", "lightning"):
        sev = "HIGH" if pat == "lightning" or (structural and cfgf_any) else "MEDIUM"
        add("WEAK_INFILL_PATTERN_STRUCTURAL", "pattern", sev,
            "%s infill on a %s part: the nozzle re-crosses infill lines and shear strength is poor; use gyroid, cross_hatch or cubic"
            % (pat, "structural" if structural else "CF/GF"), infill_pattern=pat, structural=structural)
    if load_bearing:
        low_d = dens is not None and dens < 15
        low_w = walls is not None and walls < 3
        if low_d or low_w:
            sev = "HIGH" if (walls is not None and walls < 2) or (low_d and low_w) else "MEDIUM"
            bits = (["infill %g%% (<15%%)" % dens] if low_d else []) + (["%g wall loops (<3)" % walls] if low_w else [])
            add("STRUCTURAL_UNDER_WALLED", "walls", sev,
                "%s part is under-built: %s" % ("structural" if structural else "CF/GF", ", ".join(bits)),
                infill_density_pct=dens, wall_loops=walls, structural=structural)
    return out


def audit_file(rec, ctx):
    name = os.path.basename(rec["path"])
    if rec["archive_violation"]:
        return [mk("CRITICAL", "ARCHIVE_SAFETY_VIOLATION", "%s rejected before parsing: %s" % (name, "; ".join(rec["archive_violation"])),
                   key="ARCHIVE_SAFETY_VIOLATION:" + rec["key"], file=rec["path"], reasons=rec["archive_violation"])]
    F = []
    if rec["credential_keys"]:
        F.append(mk("HIGH", "EMBEDDED_PRINTER_CREDENTIAL",
                    "%s embeds printer credentials (%s); values not shown. Strip them before sharing or archiving this file"
                    % (name, ", ".join(rec["credential_keys"])), key="EMBEDDED_PRINTER_CREDENTIAL:" + rec["key"],
                    file=rec["path"], fields=rec["credential_keys"]))
    agg = {}
    for pl in rec["plates"]:
        for code, sub, sev, msg, ev in plate_findings(rec, pl, ctx):
            e = agg.setdefault((code, sub), {"sev": sev, "msg": msg, "ev": ev, "plates": []})
            e["plates"].append(pl["index"])
            if bc.SEV[sev] > bc.SEV[e["sev"]]:
                e["sev"] = sev
    multi = len(rec["plates"]) > 1
    for (code, sub), e in sorted(agg.items()):
        where = "%s plate%s %s" % (name, "s" if len(e["plates"]) > 1 else "", ",".join(str(p) for p in e["plates"])) if multi else name
        F.append(mk(e["sev"], code, "%s: %s" % (where, e["msg"]), key="%s:%s:%s" % (code, rec["key"], sub),
                    file=rec["path"], plates=e["plates"], **e["ev"]))
    return F


# ---------------------------------------------------------------- jobs, requirements, printers
def job_req(material, plate, ctx):
    fils = plate["filaments"] if plate else []
    mats = [norm_material(f["type"]) for f in fils if f["type"]] or ([material] if material else [])
    if material and material not in mats:
        mats.insert(0, material)
    hardened = enclosure = False
    ncs, bcs = [], []
    for m in mats:
        hardened = hardened or is_abrasive(m) or ctx.spool_flag(m, "abrasive")
        enclosure = enclosure or needs_enclosure(m) or ctx.spool_flag(m, "requires_enclosure")
        _, env = ctx.envelope(m)
        if env:
            ncs.append(env[4])
            bcs.append(env[5])
    if plate:
        hardened = hardened or any(is_abrasive(f["settings_id"]) for f in fils)
        ncs = [plate["nozzle_temp_c"]] if plate.get("nozzle_temp_c") else ncs
        bcs = [plate["bed_temp_c"]] if plate.get("bed_temp_c") is not None else bcs
    return {"materials": mats, "hardened": hardened, "enclosure": enclosure, "nozzle_c": max(ncs) if ncs else None,
            "bed_c": max(bcs) if bcs else None, "nozzle_mm": plate.get("nozzle_diameter_mm") if plate else None}


def printer_fails(p, req):
    f = []
    if req["enclosure"] and not p["enclosure"]:
        f.append("enclosure")
    if req["hardened"] and p["nozzle_type"] not in HARDENED:
        f.append("hardened nozzle")
    if req["nozzle_c"] and p["max_nozzle_c"] is not None and p["max_nozzle_c"] < req["nozzle_c"]:
        f.append("hotend >= %g C" % req["nozzle_c"])
    if req["bed_c"] and p["max_bed_c"] is not None and p["max_bed_c"] < req["bed_c"]:
        f.append("bed >= %g C" % req["bed_c"])
    if req["nozzle_mm"] and p["nozzle_mm"] is not None and abs(p["nozzle_mm"] - req["nozzle_mm"]) > 0.005:
        f.append("%g mm nozzle" % req["nozzle_mm"])
    return f


def req_text(req):
    bits = [("enclosure" if req["enclosure"] else ""), ("hardened nozzle" if req["hardened"] else ""),
            ("%g mm nozzle" % req["nozzle_mm"] if req["nozzle_mm"] else ""),
            ("%g C hotend" % req["nozzle_c"] if req["nozzle_c"] else ""), ("%g C bed" % req["bed_c"] if req["bed_c"] else "")]
    return ", ".join(b for b in bits if b) or "its profile"


def missing_caps(req, printers):
    """Requirements that no online printer meets individually (or the combination, when each is met somewhere)."""
    online = [p for p in printers if p["online"]]
    if not online:
        return ["no online printer"]
    per = {}
    for p in online:
        for c in printer_fails(p, req):
            per[c] = per.get(c, 0) + 1
    all_reqs = set(per)
    missing = sorted(c for c in all_reqs if per[c] == len(online))
    return missing or ["a single printer with the full combination (%s)" % ", ".join(sorted(all_reqs))]


def resolve_plate(plate_file, files, base_dir):
    if not plate_file:
        return None, None
    m = re.match(r"^(.*?)(?:#(\d+))?$", plate_file)
    path, want = m.group(1), int(m.group(2)) if m.group(2) else None
    cands = [os.path.abspath(os.path.join(base_dir, path)), os.path.abspath(path)]
    by_key = {f["key"]: f for f in files}
    rec = next((by_key[c] for c in cands if c in by_key), None)
    if rec is None:
        same = [f for f in files if os.path.basename(f["path"]) == os.path.basename(path)]
        rec = same[0] if len(same) == 1 else None
    if rec is None or not rec["plates"]:
        return None, None
    plates = rec["plates"]
    if want is not None:
        pl = next((p for p in plates if p["index"] == want), None)
    else:
        pl = next((p for p in plates if p["index"] == 1), None) if len(plates) > 1 else plates[0]
        pl = pl or plates[0]
    return (rec, pl) if pl else (None, None)


# ---------------------------------------------------------------- bucket 2: spools
def audit_spools(spools, now):
    F, ledger = [], []
    for s in spools:
        sid, rem, thr, mat = s["spool_id"], s["remaining_g"], s["reorder_threshold_g"], s["material"]
        status = "ok"
        if rem <= LOW_STOCK_HIGH_G:
            status = "LOW(<=150g)"
            F.append(mk("HIGH", "SPOOL_LOW_STOCK", "spool %s (%s %s) has %g g left (<= %g g floor); swap before a long print"
                        % (sid, s["brand"] or "-", mat, rem, LOW_STOCK_HIGH_G), key="SPOOL_LOW_STOCK:" + sid,
                        spool_id=sid, material=mat, remaining_g=rem, reorder_threshold_g=thr))
        elif thr > 0 and rem <= thr:
            status = "REORDER"
            F.append(mk("MEDIUM", "SPOOL_LOW_STOCK", "spool %s (%s %s) is at %g g, at or below its %g g reorder threshold"
                        % (sid, s["brand"] or "-", mat, rem, thr), key="SPOOL_LOW_STOCK:" + sid,
                        spool_id=sid, material=mat, remaining_g=rem, reorder_threshold_g=thr))
        pol = dry_policy(mat, s["hygroscopic"])
        age = None
        if s["dried_at"] is not None:
            age = max(0.0, (now - s["dried_at"]).total_seconds() / 86400.0)
        if pol and rem > 0:
            limit, sev = pol
            if age is None or age > limit:
                why = "no dried_at recorded" if age is None else "last dried %.0f days ago" % age
                F.append(mk(sev, "HYGROSCOPIC_DRYING_OVERDUE", "spool %s (%s) drying overdue: %s (limit %d days); wet filament strings, "
                            "bubbles and loses strength" % (sid, mat, why, limit), key="HYGROSCOPIC_DRYING_OVERDUE:" + sid,
                            spool_id=sid, material=mat, days_since_dried=age, limit_days=limit))
                status = "DRY-OVERDUE" if status == "ok" else status + "+DRY"
            if s["last_desiccant_change"] is not None:
                dage = max(0.0, (now - s["last_desiccant_change"]).total_seconds() / 86400.0)
                if dage > 60:
                    F.append(mk("LOW", "DESICCANT_CHANGE_OVERDUE", "spool %s desiccant last changed %.0f days ago (limit 60)" % (sid, dage),
                                key="DESICCANT_CHANGE_OVERDUE:" + sid, spool_id=sid, days_since_change=dage))
        ledger.append({"spool_id": sid, "material": mat, "brand": s["brand"], "color": s["color"], "remaining_g": rem,
                       "reorder_threshold_g": thr, "cost_per_kg": s["cost_per_kg"], "days_since_dried": age,
                       "hygroscopic": pol is not None, "status": status})
    return F, ledger


def audit_supply(demand, spools):
    """demand: material -> {"grams": g, "sources": [...]}. Returns (findings, rows)."""
    F, rows = [], []
    for mat in sorted(demand):
        need = demand[mat]["grams"]
        comp = [s for s in spools if s["material"] == mat]
        supply = sum(max(0.0, s["remaining_g"]) for s in comp)
        thr = sum(s["reorder_threshold_g"] for s in comp)
        after = supply - need
        status = "ok"
        if need > 0 and (not comp or after < 0):
            status = "SHORT"
            F.append(mk("CRITICAL", "SPOOL_INSUFFICIENT_FOR_QUEUE",
                        "%s: queue needs %.0f g (incl. scrap buffer) but %s%.0f g on hand across %d spool(s); short by %.0f g"
                        % (mat, need, "only " if comp else "", supply, len(comp), -after), key="SPOOL_INSUFFICIENT_FOR_QUEUE:" + mat,
                        material=mat, required_g=need, available_g=supply, shortfall_g=-after, sources=demand[mat]["sources"]))
        elif need > 0 and after < thr:
            status = "BELOW-REORDER"
            F.append(mk("HIGH", "SPOOL_INSUFFICIENT_FOR_QUEUE",
                        "%s: after the queue only %.0f g would remain, below the %.0f g combined reorder threshold; order now"
                        % (mat, after, thr), key="SPOOL_INSUFFICIENT_FOR_QUEUE:" + mat,
                        material=mat, required_g=need, available_g=supply, remaining_after_g=after, reorder_threshold_g=thr,
                        sources=demand[mat]["sources"]))
        rows.append({"material": mat, "required_g": need, "available_g": supply, "after_g": after,
                     "reorder_threshold_g": thr, "spools": len(comp), "status": status})
    return F, rows


# ---------------------------------------------------------------- buckets 3 & 4: SKUs
def analyze_sku(o, rec, pl, ctx, args):
    onh, opn, vel, lead, safe, upp = (o["on_hand"], o["open_orders"], o["velocity"], o["lead_time_days"],
                                      o["safety_stock_days"], o["units_per_plate"])
    net = onh - opn
    dos = (max(0.0, net) / vel) if vel > 0 else math.inf
    target = math.ceil((lead + safe) * vel - 1e-9) + opn
    deficit = max(0.0, target - onh)
    plates_needed = int(math.ceil(deficit / upp - 1e-9)) if deficit > 0 else 0
    mat = o["material"] or (pl["filament_type"] if pl and pl.get("filament_type") else "")
    sl_g = (pl["filament_grams"] / upp) if pl and pl.get("filament_grams") else None
    sl_h = (pl["estimated_time_hrs"] / upp) if pl and pl.get("estimated_time_hrs") else None
    grams = o["grams_per_unit"] if o["grams_per_unit"] is not None else sl_g
    hours = o["print_hours_per_unit"] if o["print_hours_per_unit"] is not None else sl_h
    req = job_req(mat, pl, ctx)
    comp = [p for p in ctx.printers if p["online"] and not printer_fails(p, req)]
    watts = _mean([p["watts_avg"] for p in comp if p["watts_avg"] is not None]) or args.printer_watts
    rate = _mean([p["rate_hr"] for p in comp if p["rate_hr"] is not None])
    rate = args.machine_rate_hr if rate is None else rate
    ckg, csrc = ctx.cost_per_kg(mat) if mat else (None, None)
    fail = 1 + args.failure_rate_pct / 100.0
    s = {"sku": o["sku"], "name": o["name"], "material": mat, "structural": o["structural"], "on_hand": onh,
         "open_orders": opn, "velocity": vel, "lead_time_days": lead, "safety_stock_days": safe, "net_available": net,
         "days_of_supply": dos, "target_stock_units": target, "deficit_units": deficit, "units_per_plate": upp,
         "plates_needed": plates_needed, "plate_hours": (hours or 0.0) * upp, "plate_grams": (grams or 0.0) * upp,
         "grams_per_unit": grams, "hours_per_unit": hours, "total_print_hours": plates_needed * (hours or 0.0) * upp,
         "plate_ref": ("%s#%d" % (os.path.basename(rec["path"]), pl["index"])) if rec else None,
         "price": o["sale_price_usd"], "cost_per_kg": ckg, "cost_source": csrc, "watts": watts, "machine_rate_hr": rate,
         "req": req, "compatible_printers": [p["printer_id"] for p in comp], "tier": None, "costed": False}
    if onh < opn:
        s["tier"] = "1_BACKORDER"
    elif dos < lead:
        s["tier"] = "2_STOCKOUT_RISK"
    elif plates_needed > 0:
        s["tier"] = "3_SAFETY_REPLENISH"
    if grams is not None and hours is not None and ckg is not None:
        fil = grams * fail * (ckg / 1000.0)
        ele = hours * (watts / 1000.0) * args.kwh_rate
        mac = hours * rate * fail
        lab = (o["post_proc_mins"] / 60.0) * args.labor_rate_hr
        cogs = fil + ele + mac + lab
        s.update({"costed": True, "filament_cost_usd": fil, "electricity_cost_usd": ele, "machine_cost_usd": mac,
                  "labor_cost_usd": lab, "total_cogs_usd": cogs})
        if o["sale_price_usd"] is not None:
            p = o["sale_price_usd"]
            rev = p * (1 - o["marketplace_fee_pct"] / 100.0) - o["shipping_cost_usd"]
            s.update({"net_revenue_usd": rev, "net_profit_usd": rev - cogs,
                      "gross_margin_pct": ((rev - cogs) / p * 100.0) if p > 0 else None})
    return s


def sku_findings(s, o, pl, args):
    F, sku = [], s["sku"]
    ev = dict(sku=sku, on_hand=s["on_hand"], open_orders=s["open_orders"], days_of_supply=s["days_of_supply"],
              lead_time_days=s["lead_time_days"])
    if s["tier"] == "1_BACKORDER":
        F.append(mk("CRITICAL", "SKU_BACKORDER_DEFICIT", "%s: %g on hand vs %g open orders; %g customer unit(s) are waiting on the print queue"
                    % (sku, s["on_hand"], s["open_orders"], s["open_orders"] - s["on_hand"]), key="SKU_BACKORDER_DEFICIT:" + sku, **ev))
    elif s["tier"] == "2_STOCKOUT_RISK":
        F.append(mk("HIGH", "SKU_STOCKOUT_IMMINENT", "%s: %.1f days of supply is below the %g day lead time; it will stock out before a reprint lands"
                    % (sku, s["days_of_supply"], s["lead_time_days"]), key="SKU_STOCKOUT_IMMINENT:" + sku, **ev))
    elif s["days_of_supply"] < s["lead_time_days"] + s["safety_stock_days"]:
        F.append(mk("MEDIUM", "SKU_REORDER_TRIGGER", "%s: %.1f days of supply is below lead time + safety stock (%g days); queue %d plate(s)"
                    % (sku, s["days_of_supply"], s["lead_time_days"] + s["safety_stock_days"], s["plates_needed"]),
                    key="SKU_REORDER_TRIGGER:" + sku, plates_needed=s["plates_needed"], **ev))
    if not s["costed"]:
        miss = [n for n, v in (("grams_per_unit", s["grams_per_unit"]), ("print_hours_per_unit", s["hours_per_unit"]),
                               ("material", s["material"])) if not v and v != 0.0]
        F.append(mk("INFO", "SKU_COST_INPUTS_MISSING", "%s: cannot compute unit cost, missing %s" % (sku, ", ".join(miss) or "inputs"),
                    key="SKU_COST_INPUTS_MISSING:" + sku, sku=sku, missing=miss))
        return F
    if "net_profit_usd" in s:
        gm, np_ = s["gross_margin_pct"], s["net_profit_usd"]
        ev3 = dict(sku=sku, total_cogs_usd=s["total_cogs_usd"], net_profit_usd=np_, gross_margin_pct=gm, sale_price_usd=s["price"])
        if np_ <= 0:
            F.append(mk("CRITICAL", "SKU_NEGATIVE_MARGIN", "%s loses %s per unit (COGS %s vs net revenue %s)"
                        % (sku, usd(-np_), usd(s["total_cogs_usd"]), usd(s["net_revenue_usd"])), key="SKU_NEGATIVE_MARGIN:" + sku, **ev3))
        elif gm < 25.0:
            F.append(mk("HIGH", "SKU_CRITICAL_MARGIN_COMPRESSION", "%s gross margin %.1f%% is under 25%% (profit %s on %s)"
                        % (sku, gm, usd(np_), usd(s["price"])), key="SKU_CRITICAL_MARGIN_COMPRESSION:" + sku, **ev3))
        elif gm < args.margin_target_pct:
            F.append(mk("MEDIUM", "SKU_BELOW_TARGET_MARGIN", "%s gross margin %.1f%% is below the %.1f%% target"
                        % (sku, gm, args.margin_target_pct), key="SKU_BELOW_TARGET_MARGIN:" + sku, target_pct=args.margin_target_pct, **ev3))
    if pl and (pl.get("filament_grams") is not None or pl.get("estimated_time_hrs") is not None):
        diffs = []
        upp = s["units_per_plate"]
        for label, tbl, sliced in (("grams", o["grams_per_unit"], pl["filament_grams"]), ("hours", o["print_hours_per_unit"], pl["estimated_time_hrs"])):
            if tbl and sliced is not None and abs(sliced / upp - tbl) / tbl > 0.15:
                diffs.append("%s/unit table %.2f vs sliced %.2f (%+.0f%%)" % (label, tbl, sliced / upp, (sliced / upp - tbl) / tbl * 100))
        if diffs:
            F.append(mk("MEDIUM", "PLATE_BOM_MISMATCH", "%s vs %s: %s; costs and queue hours use the table values"
                        % (sku, s["plate_ref"], "; ".join(diffs)), key="PLATE_BOM_MISMATCH:" + sku, sku=sku, plate=s["plate_ref"], diffs=diffs))
    return F


# ---------------------------------------------------------------- scheduler
def build_schedule(skus, printers, args):
    """Greedy earliest-finish assignment of plates in priority order. -> (rows, per-sku info)."""
    jobs = sorted((s for s in skus if s["tier"] and s["plates_needed"] > 0), key=lambda s: (s["tier"], s["days_of_supply"], s["sku"]))
    online = sorted((p for p in printers if p["online"]), key=lambda p: p["printer_id"])
    load = {p["printer_id"]: 0.0 for p in online}
    rows, info = [], {}
    fail = 1 + args.failure_rate_pct / 100.0
    for rank, s in enumerate(jobs, 1):
        compat = [p for p in online if not printer_fails(p, s["req"])]
        s["rank"] = rank
        if not compat:
            info[s["sku"]] = {"assigned": {}, "finish_day": None}
            rows.append({"priority_rank": rank, "urgency_tier": s["tier"], "sku": s["sku"], "name": s["name"], "material": s["material"],
                         "plates_to_print": s["plates_needed"], "units_to_print": s["deficit_units"], "assigned_printer": "UNASSIGNED",
                         "est_print_hours": s["total_print_hours"], "filament_grams_needed": s["plates_needed"] * s["plate_grams"] * fail,
                         "unit_cogs_usd": s.get("total_cogs_usd"), "gross_margin_pct": s.get("gross_margin_pct"),
                         "days_of_supply": s["days_of_supply"]})
            continue
        left, per, step = s["plates_needed"], {}, max(1, s["plates_needed"] // 1000)
        while left > 0:
            n = min(step, left)
            best = min(compat, key=lambda p: ((load[p["printer_id"]] + n * s["plate_hours"]) / p["capacity_hrs"] if p["capacity_hrs"] else math.inf,
                                              0 if s["material"] in p["loaded_materials"] else 1, p["printer_id"]))
            load[best["printer_id"]] += n * s["plate_hours"]
            per[best["printer_id"]] = per.get(best["printer_id"], 0) + n
            left -= n
        caps = {p["printer_id"]: p["capacity_hrs"] for p in online}
        info[s["sku"]] = {"assigned": per, "finish_day": max((load[pid] / caps[pid] if caps[pid] else math.inf) for pid in per)}
        units_left = s["deficit_units"]
        for pid in sorted(per):
            u = min(units_left, per[pid] * s["units_per_plate"])
            units_left -= u
            rows.append({"priority_rank": rank, "urgency_tier": s["tier"], "sku": s["sku"], "name": s["name"], "material": s["material"],
                         "plates_to_print": per[pid], "units_to_print": u, "assigned_printer": pid,
                         "est_print_hours": per[pid] * s["plate_hours"], "filament_grams_needed": per[pid] * s["plate_grams"] * fail,
                         "unit_cogs_usd": s.get("total_cogs_usd"), "gross_margin_pct": s.get("gross_margin_pct"),
                         "days_of_supply": s["days_of_supply"]})
    return rows, info, load


def capacity_findings(skus, printers, info):
    urgent = [s for s in skus if s["tier"] in ("1_BACKORDER", "2_STOCKOUT_RISK") and s["plates_needed"] > 0 and info.get(s["sku"], {}).get("assigned")]
    if not urgent:
        return [], None
    horizon = max(max(s["lead_time_days"], 1.0) for s in urgent)
    needed = sum(s["total_print_hours"] for s in urgent)
    pids = {pid for s in urgent for pid in info[s["sku"]]["assigned"]}
    avail = sum(p["capacity_hrs"] for p in printers if p["online"] and any(not printer_fails(p, s["req"]) for s in urgent)) * horizon
    late = [{"sku": s["sku"], "finish_day": info[s["sku"]]["finish_day"], "lead_time_days": s["lead_time_days"]}
            for s in urgent if info[s["sku"]]["finish_day"] > max(s["lead_time_days"], 1.0) + 1e-9]
    summary = {"urgent_hours": needed, "available_hours": avail, "horizon_days": horizon, "late": late, "printers": sorted(pids)}
    if needed > avail + 1e-9 or late:
        why = ("%.1f urgent print hours vs %.1f available farm hours in %.0f days" % (needed, avail, horizon)) if needed > avail + 1e-9 else \
            "%d urgent job(s) would finish after their lead time (%s)" % (len(late), ", ".join(l["sku"] for l in late[:5]))
        return [mk("HIGH", "FARM_CAPACITY_OVERLOAD", "farm capacity overload: %s" % why, key="FARM_CAPACITY_OVERLOAD",
                   urgent_hours=needed, available_hours=avail, horizon_days=horizon, late=late)], summary
    return [], summary


# ---------------------------------------------------------------- state + delta
BUCKETS = (
    ("1", "Slicer profile & thermal / mechanical safety",
     {"ARCHIVE_SAFETY_VIOLATION", "EMBEDDED_PRINTER_CREDENTIAL", "THERMAL_RUNAWAY_OR_OUT_OF_RANGE", "ABRASIVE_FILAMENT_BRASS_NOZZLE",
      "ABRASIVE_CLOG_RISK_SMALL_NOZZLE", "LAYER_HEIGHT_EXCEEDS_NOZZLE_LIMIT", "WEAK_INFILL_PATTERN_STRUCTURAL",
      "STRUCTURAL_UNDER_WALLED", "ENCLOSURE_REQUIRED_OPEN_FRAME", "TPU_HIGH_VOLUMETRIC_SPEED", "TEMP_MATERIAL_MISMATCH"}),
    ("2", "Spool inventory, moisture hygiene & BOM depletion",
     {"SPOOL_INSUFFICIENT_FOR_QUEUE", "SPOOL_LOW_STOCK", "HYGROSCOPIC_DRYING_OVERDUE", "DESICCANT_CHANGE_OVERDUE"}),
    ("3", "Unit economics & per-part BOM",
     {"SKU_NEGATIVE_MARGIN", "SKU_CRITICAL_MARGIN_COMPRESSION", "SKU_BELOW_TARGET_MARGIN", "PLATE_BOM_MISMATCH",
      "SKU_COST_INPUTS_MISSING", "FILAMENT_COST_ASSUMED", "PLATE_FILE_UNRESOLVED"}),
    ("4", "SKU run-rate & multi-printer queue",
     {"SKU_BACKORDER_DEFICIT", "SKU_STOCKOUT_IMMINENT", "SKU_REORDER_TRIGGER", "NO_COMPATIBLE_PRINTER_FOR_JOB",
      "FARM_CAPACITY_OVERLOAD"}),
)
DRIFT_HIGH = ("filament_types", "nozzle_temp_c", "nozzle_diameter_mm", "nozzle_type")


def make_state(spools, skus, files, findings):
    plates = {}
    for rec in files:
        for pl in rec["plates"]:
            settings = dict({k: pl.get(k) for k in TRACKED}, filament_types=pl["filament_types"])
            plates["%s#%d" % (rec["key"], pl["index"])] = {"hash": pl["settings_hash"], "settings": settings,
                                                          "file_sha256": rec["sha256"], "file": os.path.basename(rec["path"])}
    return clean({
        "spools": {s["spool_id"]: {"remaining_g": s["remaining_g"], "reorder_threshold_g": s["reorder_threshold_g"],
                                   "material": s["material"]} for s in spools},
        "skus": {s["sku"]: {"days_of_supply": s["days_of_supply"], "tier": s["tier"], "on_hand": s["on_hand"],
                            "open_orders": s["open_orders"]} for s in skus},
        "plates": plates,
        "findings_by_key": {f.key: f.to_dict() for f in bc.sort_findings(findings)}})


def diff_findings(old, new):
    o = {k: bc.Finding.from_dict(v) for k, v in (old or {}).items()}
    n = {f.key: f for f in new}
    res = {"NEW": [], "CHANGED": [], "RESOLVED": []}
    for k in sorted(n):
        if k not in o:
            res["NEW"].append(n[k])
        elif o[k].sev != n[k].sev or o[k].msg != n[k].msg:
            res["CHANGED"].append(n[k])
    res["RESOLVED"] = [o[k] for k in sorted(o) if k not in n]
    return res


def compute_events(prev, cur):
    ev = []

    def add(sev, code, msg, key, **e):
        ev.append(mk(sev, code, msg, key="EVENT:%s:%s" % (code, key), **e))

    ps, cs = prev.get("spools") or {}, cur["spools"]
    for sid in sorted(set(ps) & set(cs)):
        a, b = ps[sid], cs[sid]
        drop = (a.get("remaining_g") or 0) - b["remaining_g"]
        thr = b.get("reorder_threshold_g") or 0
        crossed = thr > 0 and (a.get("remaining_g") or 0) > thr >= b["remaining_g"]
        if drop >= 250 or crossed:
            why = ("dropped %.0f g" % drop) + (" and crossed below its %.0f g reorder threshold" % thr if crossed else "")
            add("HIGH" if b["remaining_g"] <= LOW_STOCK_HIGH_G else "MEDIUM", "SPOOL_DEPLETED",
                "spool %s (%s) %s since baseline (%.0f g -> %.0f g)" % (sid, b.get("material"), why, a.get("remaining_g") or 0, b["remaining_g"]),
                sid, spool_id=sid, previous_g=a.get("remaining_g"), remaining_g=b["remaining_g"])
    pk, ck = prev.get("skus") or {}, cur["skus"]
    for sku in sorted(set(pk) & set(ck)):
        a, b = pk[sku], ck[sku]
        pd, cd = a.get("days_of_supply"), b.get("days_of_supply")
        bad = ("1_BACKORDER", "2_STOCKOUT_RISK")
        if b.get("tier") in bad and a.get("tier") not in bad:
            add("CRITICAL" if b["tier"] == "1_BACKORDER" else "HIGH", "SKU_DAYS_OF_SUPPLY_DROP",
                "%s entered %s (days of supply %s -> %s)" % (sku, b["tier"][2:].lower(), fd(pd if pd is not None else math.inf), fd(cd if cd is not None else math.inf)),
                sku, sku=sku, previous_days=pd, days_of_supply=cd, tier=b["tier"])
        elif pd is not None and cd is not None and pd - cd >= 2.0:
            add("MEDIUM", "SKU_DAYS_OF_SUPPLY_DROP", "%s days of supply fell %.1f -> %.1f (-%.1f days) since baseline" % (sku, pd, cd, pd - cd),
                sku, sku=sku, previous_days=pd, days_of_supply=cd)
    pp, cp = prev.get("plates") or {}, cur["plates"]
    for k in sorted(set(pp) & set(cp)):
        a, b = pp[k], cp[k]
        if a.get("hash") != b["hash"]:
            sa, sb = a.get("settings") or {}, b["settings"]
            changed = sorted(f for f in set(sa) | set(sb) if sa.get(f) != sb.get(f))
            detail = ", ".join("%s %s -> %s" % (f, sa.get(f), sb.get(f)) for f in changed[:6])
            add("HIGH" if any(f in DRIFT_HIGH for f in changed) else "MEDIUM", "PROFILE_DRIFT_DETECTED",
                "%s slicer settings changed since baseline: %s" % (b.get("file") or k, detail), k, file=b.get("file"), changed=changed)
        elif a.get("file_sha256") != b.get("file_sha256"):
            add("LOW", "PROFILE_DRIFT_DETECTED", "%s file content changed since baseline but tracked slicer settings are identical"
                % (b.get("file") or k), k, file=b.get("file"), changed=[])
    return ev


def compute_exit(findings, degraded, fail_on):
    if fail_on != "none" and findings:
        worst = max(bc.SEV[f.sev] for f in findings)
        if worst >= bc.SEV[fail_on.upper()]:
            return bc.EXIT_CRITICAL if worst == 4 else bc.EXIT_HIGH if worst == 3 else bc.EXIT_MEDIUM
    return bc.EXIT_DEGRADED if degraded else bc.EXIT_OK


# ---------------------------------------------------------------- rendering
def _counts(findings):
    c = {k: 0 for k in SEV_ORDER}
    for f in findings:
        c[f.sev] += 1
    return c


def _worst(findings):
    w = max((bc.SEV[f.sev] for f in findings), default=-1)
    return next((k for k, v in bc.SEV.items() if v == w), "NONE")


def _cell(s, md):
    s = "" if s is None else str(s)
    return s.replace("|", "\\|").replace("\n", " ") if md else s


def table(headers, rows, md):
    rows = [[_cell(c, md) for c in r] for r in rows]
    if md:
        return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + ["| " + " | ".join(r) + " |" for r in rows]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = lambda r: ("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r))).rstrip()  # noqa: E731
    return [line(headers), line(["-" * x for x in w])] + [line(r) for r in rows]


def finding_lines(fs, md):
    if not fs:
        return ["- (no findings)" if md else "  (no findings)"]
    if md:
        return ["- **%s** `%s` %s" % (f.sev, f.code, _cell(f.msg, True)) for f in bc.sort_findings(fs)]
    return ["  [%-8s] %s: %s" % (f.sev, f.code, f.msg) for f in bc.sort_findings(fs)]


def delta_lines(delta, md):
    if delta is None:
        return []
    if delta.get("skipped"):
        return ["skipped: %s" % delta["skipped"]]
    if delta.get("baseline"):
        return ["baseline run: %d finding(s) recorded as NEW; no prior state" % len(delta["new"])]
    L = []
    for label in ("events", "new", "changed", "resolved"):
        for f in delta[label]:
            L.append(("- **%s** [%s] `%s` %s" if md else "  %-8s [%s] %s: %s") % ("EVENT" if label == "events" else label.upper(), f.sev, f.code, f.msg))
    return L or ["(no changes since baseline)"]


def render(D, md):
    L = []
    H = (lambda t: ["", "## " + t, ""]) if md else (lambda t: ["", "== " + t + " =="])
    if md:
        L += ["# 3D Print Farm & Slicer Queue Ops Report", "", "```", D["header"], "```", ""]
    else:
        L += [D["header"]]
    inp = D["inputs"]
    L += [("**Inputs:** " if md else "inputs     ") + "%d slicer file(s) / %d plate(s), %d spool(s), %d SKU(s), %d printer(s)"
          % (inp["files"], inp["plates"], inp["spools"], inp["skus"], inp["printers"])]
    c = _counts(D["findings"])
    L += H("Summary")
    L += table(["critical", "high", "medium", "low", "info", "worst"],
               [[c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], _worst(D["findings"])]], md)
    # bucket 1
    L += H("Bucket 1 - Slicer profile & thermal / mechanical safety")
    if D["plates"]:
        L += table(["file", "plate", "slicer", "printer", "nozzle", "material", "layer", "walls", "infill", "N/B C", "hrs", "g"],
                   [[p["file"], p["index"], p["slicer"] or "-", p["printer_model"] or "-",
                     "%s %s" % (fd(p["nozzle_diameter_mm"], 2), p["nozzle_type"]), "/".join(dict.fromkeys(t or "?" for t in p["filament_types"])),
                     fd(p["layer_height_mm"], 2), fd(p["wall_loops"], 0),
                     "%s %s" % (fd(p["infill_density_pct"], 0) + "%" if p["infill_density_pct"] is not None else "-", p["infill_pattern"] or ""),
                     "%s/%s" % (fd(p["nozzle_temp_c"], 0), fd(p["bed_temp_c"], 0)), fd(p["estimated_time_hrs"], 2), fd(p["filament_grams"], 1)]
                    for p in D["plates"]], md)
    else:
        L += ["(no slicer files inspected)"]
    L += [""] + finding_lines(D["by_bucket"]["1"], md)
    # bucket 2
    L += H("Bucket 2 - Spool inventory, moisture hygiene & BOM depletion")
    if D["ledger"]:
        L += table(["spool", "material", "brand", "remaining g", "reorder g", "$/kg", "dried (days ago)", "status"],
                   [[s["spool_id"], s["material"], s["brand"] or "-", fd(s["remaining_g"], 0), fd(s["reorder_threshold_g"], 0),
                     fd(s["cost_per_kg"], 2), fd(s["days_since_dried"], 0) if s["hygroscopic"] else "n/a", s["status"]] for s in D["ledger"]], md)
        L += [""]
    if D["supply"]:
        L += table(["material", "queue needs g", "available g", "after g", "status"],
                   [[s["material"], fd(s["required_g"], 0), fd(s["available_g"], 0), fd(s["after_g"], 0), s["status"]] for s in D["supply"]], md)
        L += [""]
    L += finding_lines(D["by_bucket"]["2"], md)
    # bucket 3
    L += H("Bucket 3 - Unit economics & per-part BOM")
    costed = [s for s in D["skus"] if s["costed"]]
    if costed:
        L += table(["sku", "material", "filament", "power", "machine", "labor", "COGS", "price", "net rev", "profit", "margin"],
                   [[s["sku"], s["material"], usd(s["filament_cost_usd"]), usd(s["electricity_cost_usd"]), usd(s["machine_cost_usd"]),
                     usd(s["labor_cost_usd"]), usd(s["total_cogs_usd"]), usd(s.get("price")), usd(s.get("net_revenue_usd")),
                     usd(s.get("net_profit_usd")), fd(s.get("gross_margin_pct"), 1) + "%" if s.get("gross_margin_pct") is not None else "-"]
                    for s in costed], md)
    else:
        L += ["(no costed SKUs)"]
    if D["plate_costs"]:
        L += ["", "Unlinked plates (cost only, no sale price):"] + ([""] if md else []) + table(
            ["plate", "material", "grams", "hours", "filament", "power", "machine", "COGS"],
            [[p["plate"], p["material"], fd(p["grams"], 1), fd(p["hours"], 2), usd(p["filament"]), usd(p["power"]), usd(p["machine"]), usd(p["cogs"])]
             for p in D["plate_costs"]], md)
    L += [""] + finding_lines(D["by_bucket"]["3"], md)
    # bucket 4
    L += H("Bucket 4 - SKU run-rate & multi-printer queue")
    if D["skus"]:
        L += table(["sku", "on hand", "open", "net avail", "vel/day", "days supply", "lead+safety", "target", "deficit", "plates", "print hrs", "tier"],
                   [[s["sku"], fd(s["on_hand"], 0), fd(s["open_orders"], 0), fd(s["net_available"], 0), fd(s["velocity"], 2),
                     fd(s["days_of_supply"], 1), "%g+%g" % (s["lead_time_days"], s["safety_stock_days"]), fd(s["target_stock_units"], 0),
                     fd(s["deficit_units"], 0), s["plates_needed"], fd(s["total_print_hours"], 1), s["tier"] or "-"] for s in D["skus"]], md)
        L += [""]
    if D["printers"]:
        L += table(["printer", "model", "nozzle", "type", "encl", "max N/B C", "status", "cap h/day", "queued h"],
                   [[p["printer_id"], p["model"] or "-", fd(p["nozzle_mm"], 2), p["nozzle_type"], "yes" if p["enclosure"] else "no",
                     "%s/%s" % (fd(p["max_nozzle_c"], 0), fd(p["max_bed_c"], 0)), p["status"], fd(p["capacity_hrs"], 1),
                     fd(D["load"].get(p["printer_id"], 0.0), 1)] for p in D["printers"]], md)
        L += [""]
    if D["schedule"]:
        L += ["Scheduled queue (greedy earliest-finish):"] + ([""] if md else []) + table(
            ["#", "tier", "sku", "material", "plates", "units", "printer", "hrs", "grams", "COGS", "margin", "days supply"],
            [[r["priority_rank"], r["urgency_tier"], r["sku"], r["material"], r["plates_to_print"], fd(r["units_to_print"], 0), r["assigned_printer"],
              fd(r["est_print_hours"], 1), fd(r["filament_grams_needed"], 0), usd(r["unit_cogs_usd"]),
              fd(r["gross_margin_pct"], 1) + "%" if r["gross_margin_pct"] is not None else "-", fd(r["days_of_supply"], 1)] for r in D["schedule"]], md)
        L += [""]
    L += finding_lines(D["by_bucket"]["4"], md)
    other = D["by_bucket"]["other"]
    if other:
        L += H("Other") + finding_lines(other, md)
    if D["delta"] is not None:
        L += H("Delta since baseline") + delta_lines(D["delta"], md)
    if D["degraded"]:
        L += H("Degraded inputs") + [("- " if md else "  ") + d for d in D["degraded"]]
        L += ["", "DEGRADED: result may be incomplete; this is not a clean pass."]
    L += ["", "exit_code %d (fail-on=%s)" % (D["code"], D["fail_on"])]
    L += ["", "---", "", SCOPE_NOTE, ""] if md else ["", SCOPE_NOTE]
    return "\n".join(L)


# ---------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = _Parser(prog="print_farm_ops.py", allow_abbrev=False,
                description="Read-only, offline 3D print farm & slicer queue ops auditor")
    p.add_argument("paths", nargs="*", metavar="PATH", help=".3mf / .gcode.3mf / .gcode files or directories")
    p.add_argument("--3mf", dest="threemf_files", action="append", default=[], metavar="PATH")
    p.add_argument("--dir", action="append", default=[], metavar="DIR")
    p.add_argument("--spools", metavar="PATH")
    p.add_argument("--orders", "--inventory", dest="orders", metavar="PATH")
    p.add_argument("--printers", metavar="PATH")
    p.add_argument("--kwh-rate", type=float, default=0.14)
    p.add_argument("--printer-watts", type=float, default=180.0)
    p.add_argument("--machine-rate-hr", type=float, default=0.85)
    p.add_argument("--labor-rate-hr", type=float, default=22.0)
    p.add_argument("--failure-rate-pct", type=float, default=4.0)
    p.add_argument("--margin-target-pct", type=float, default=45.0)
    p.add_argument("--queue-csv", metavar="PATH")
    p.add_argument("--state", metavar="PATH")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--now", metavar="ISO", help="override the report clock (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ) for reproducible runs")
    p.add_argument("--format", type=str.lower, choices=["text", "markdown", "md", "json"], default="text")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", metavar="PATH")
    p.add_argument("--fail-on", type=str.lower, choices=["none", "info", "low", "medium", "high", "critical"], default="medium")
    return p


def _parse_now(s):
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def check_target(path, flag, protected):
    if path.lower().endswith((".3mf", ".gcode")):
        raise UsageError("%s refuses to write a .3mf / .gcode path (%s): slicer files are never written" % (flag, visible(path, 80)))
    if os.path.isdir(path):
        raise UsageError("%s %s is a directory" % (flag, visible(path, 80)))
    rp = os.path.realpath(path)
    for q in protected:
        same = rp == q
        if not same and os.path.exists(path) and os.path.exists(q):
            try:
                same = os.path.samefile(path, q)
            except OSError:
                same = False
        if same:
            raise UsageError("%s %s matches an input file; refusing to overwrite it" % (flag, visible(path, 80)))


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pfo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


QUEUE_COLUMNS = ("priority_rank", "urgency_tier", "sku", "name", "material", "plates_to_print", "units_to_print", "assigned_printer",
                 "est_print_hours", "filament_grams_needed", "unit_cogs_usd", "gross_margin_pct", "days_of_supply")


def queue_csv_text(rows):
    def safe(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    def num_(v, nd=2):
        if v is None:
            return ""
        if isinstance(v, float):
            return "inf" if math.isinf(v) else ("%.*f" % (nd, v)).rstrip("0").rstrip(".") if nd else str(int(v))
        return str(v)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(QUEUE_COLUMNS)
    for r in rows:
        w.writerow([r["priority_rank"], r["urgency_tier"], safe(r["sku"]), safe(r["name"]), safe(r["material"]), r["plates_to_print"],
                    num_(r["units_to_print"]), safe(r["assigned_printer"]), num_(r["est_print_hours"]), num_(r["filament_grams_needed"], 1),
                    num_(r["unit_cogs_usd"]), num_(r["gross_margin_pct"], 1), num_(r["days_of_supply"], 1)])
    return buf.getvalue()


def collect_files(args, degraded):
    paths, seen = [], set()

    def add(path, explicit):
        if os.path.isdir(path):
            found, trunc = discover(path)
            if trunc:
                degraded.append("%s: directory scan truncated at %d files" % (path, MAX_DISCOVERED))
            for f in found:
                add(f, False)
        elif os.path.isfile(path):
            if not is_slicer_file(path):
                if explicit:
                    raise UsageError("unsupported file type %s (expected .3mf, .gcode.3mf or .gcode)" % visible(path, 80))
                return
            rp = os.path.realpath(path)
            if rp not in seen:
                seen.add(rp)
                paths.append(path)
        else:
            degraded.append("%s: not found" % visible(path, 100))
    for p in args.paths + args.threemf_files + args.dir:
        add(p, True)
    return paths, add


def run(argv, out=None, err=None, now=None):
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(argv)
    hit = scan_argv(argv)
    if hit:
        print("error: refusing to run: argument %d looks like a %s; credentials, keys, seed phrases and printer access codes "
              "are never accepted (value not echoed)" % hit, file=err)
        return bc.EXIT_USAGE
    try:
        args = build_parser().parse_args(argv)
        if args.delta and not args.state:
            raise UsageError("--delta requires --state PATH")
        for flag in ("kwh_rate", "printer_watts", "machine_rate_hr", "labor_rate_hr", "failure_rate_pct", "margin_target_pct"):
            v = getattr(args, flag)
            if not math.isfinite(v) or v < 0 or v > 100000:
                raise UsageError("--%s must be a finite non-negative number" % flag.replace("_", "-"))
        if args.margin_target_pct > 100:
            raise UsageError("--margin-target-pct must be 0-100")
        clock = now or (_parse_now(args.now) if args.now else None)
        if args.now and clock is None:
            raise UsageError("--now must be an ISO date or timestamp")
        clock = clock or bc.utc_now()
        degraded = []
        scan_list, add_file = collect_files(args, degraded)
        tables = {}
        for label, path, parser, idkey in (("spools", args.spools, parse_spool, "spool_id"), ("orders", args.orders, parse_order, "sku"),
                                           ("printers", args.printers, parse_printer, "printer_id")):
            tables[label] = None
            if path:
                rows, e = load_table(path, label)
                if e:
                    degraded.append("%s %s: %s" % (label, visible(path, 100), e))
                    continue
                scan_rows(rows, label)
                tables[label] = parse_rows(rows, parser, idkey, label, degraded)
        spools, orders, printers = (tables[k] or [] for k in ("spools", "orders", "printers"))
        # plate files referenced by SKUs are inspected too (read-only)
        base_dir = os.path.dirname(os.path.abspath(args.orders)) if args.orders else os.getcwd()
        for o in orders:
            pf = re.sub(r"#\d+$", "", o["plate_file"])
            for cand in ((os.path.join(base_dir, pf), pf) if pf else ()):
                if os.path.isfile(cand) and is_slicer_file(cand):
                    add_file(cand, False)
                    break
        protected = {os.path.realpath(p) for p in scan_list + [p for p in (args.spools, args.orders, args.printers) if p]}
        if args.state:
            check_target(args.state, "--state", protected)
        extra = {os.path.realpath(p) for p in (args.state,) if p}
        outs = [("--queue-csv", args.queue_csv), ("--out", args.out)]
        for flag, path in outs:
            if path:
                others = {os.path.realpath(p) for f2, p in outs if p and f2 != flag}
                check_target(path, flag, protected | extra | others)
    except UsageError as e:
        print("error: %s" % e, file=err)
        return bc.EXIT_USAGE
    except SystemExit as e:  # --help
        return int(e.code or 0)

    fmt = "json" if args.json else ("markdown" if args.format == "md" else args.format)
    files = []
    for path in scan_list:
        rec = inspect_file(path)
        files.append(rec)
        if rec["error"]:
            degraded.append("%s: %s" % (visible(path, 100), rec["error"]))
        if rec["archive_violation"]:
            degraded.append("%s: unsafe archive rejected (%s)" % (visible(path, 100), rec["archive_violation"][0]))
        for pr in rec["problems"]:
            degraded.append("%s: %s" % (visible(path, 100), pr))
    files = [r for r in files if not r["error"]]
    valid_inputs = bool(files) or any(tables[k] is not None for k in tables)
    if not valid_inputs and not degraded:
        degraded.append("no inputs provided; nothing to audit")
    elif not valid_inputs:
        degraded.append("no valid inputs could be read")

    ctx = Ctx(args, printers, spools, clock)
    links = {}
    for o in orders:
        rec, pl = resolve_plate(o["plate_file"], files, base_dir)
        links[o["sku"]] = (rec, pl)
        if o["structural"] and pl:
            ctx.structural[(rec["key"], pl["index"])] = True
    findings = []
    for rec in files:
        findings += audit_file(rec, ctx)
    sp_f, ledger = audit_spools(spools, clock)
    findings += sp_f
    skus = []
    for o in orders:
        rec, pl = links[o["sku"]]
        s = analyze_sku(o, rec, pl, ctx, args)
        skus.append(s)
        findings += sku_findings(s, o, pl, args)
        if o["plate_file"] and rec is None:
            findings.append(mk("LOW", "PLATE_FILE_UNRESOLVED", "%s: plate_file %s was not found among the inspected files" % (o["sku"], visible(o["plate_file"], 80)),
                               key="PLATE_FILE_UNRESOLVED:" + o["sku"], sku=o["sku"]))
    for mat in sorted({s["material"] for s in skus if s["costed"] and s["cost_source"] != "spool ledger"}):
        users = [s["sku"] for s in skus if s["material"] == mat and s["costed"] and s["cost_source"] != "spool ledger"]
        findings.append(mk("INFO", "FILAMENT_COST_ASSUMED", "%s: no spool cost in the ledger, used a default estimate for %s" % (mat, ", ".join(users[:6])),
                           key="FILAMENT_COST_ASSUMED:" + mat, material=mat, skus=users))
    # unlinked plates: one print each, cost-only
    linked_files = {rec["key"] for rec, _ in links.values() if rec}
    fail = 1 + args.failure_rate_pct / 100.0
    plate_jobs, plate_costs = [], []
    for rec in files:
        if rec["key"] in linked_files:
            continue
        for pl in rec["plates"]:
            if not pl.get("sliced") and pl.get("filament_grams") is None:
                continue
            req = job_req(pl["filament_type"] or "", pl, ctx)
            plate_jobs.append((rec, pl, req))
            mat = pl["filament_type"] or ""
            ckg, _ = ctx.cost_per_kg(mat) if mat else (None, None)
            if ckg is not None and pl.get("filament_grams") is not None and pl.get("estimated_time_hrs") is not None:
                comp = [p for p in printers if p["online"] and not printer_fails(p, req)]
                w = _mean([p["watts_avg"] for p in comp if p["watts_avg"] is not None]) or args.printer_watts
                rt = _mean([p["rate_hr"] for p in comp if p["rate_hr"] is not None])
                rt = args.machine_rate_hr if rt is None else rt
                fil, pw = pl["filament_grams"] * fail * ckg / 1000.0, pl["estimated_time_hrs"] * w / 1000.0 * args.kwh_rate
                mac = pl["estimated_time_hrs"] * rt * fail
                plate_costs.append({"plate": "%s#%d" % (os.path.basename(rec["path"]), pl["index"]), "material": mat, "grams": pl["filament_grams"],
                                    "hours": pl["estimated_time_hrs"], "filament": fil, "power": pw, "machine": mac, "cogs": fil + pw + mac})
    # bucket 4: scheduling + compatibility
    printers_ok = args.printers is not None and tables["printers"] is not None
    sched, info, load = build_schedule(skus, printers, args)
    if printers_ok:
        for s in skus:
            if s["tier"] and s["plates_needed"] > 0 and not s["compatible_printers"]:
                miss = missing_caps(s["req"], printers)
                findings.append(mk("CRITICAL", "NO_COMPATIBLE_PRINTER_FOR_JOB",
                                   "%s (%s) needs [%s] but no online printer provides: %s" % (s["sku"], s["material"] or "?", req_text(s["req"]), ", ".join(miss)),
                                   key="NO_COMPATIBLE_PRINTER_FOR_JOB:sku:" + s["sku"], sku=s["sku"], missing=miss))
        for rec, pl, req in plate_jobs:
            if not any(p["online"] and not printer_fails(p, req) for p in printers):
                miss = missing_caps(req, printers)
                pid = "%s#%d" % (os.path.basename(rec["path"]), pl["index"])
                findings.append(mk("CRITICAL", "NO_COMPATIBLE_PRINTER_FOR_JOB", "plate %s (%s) needs [%s] but no online printer provides: %s"
                                   % (pid, "/".join(req["materials"]) or "?", req_text(req), ", ".join(miss)),
                                   key="NO_COMPATIBLE_PRINTER_FOR_JOB:plate:%s#%d" % (rec["key"], pl["index"]), plate=pid, missing=miss))
        cap_f, _ = capacity_findings(skus, printers, info)
        findings += cap_f
    # bucket 2: demand vs supply
    ledger_supply = []
    if args.spools and tables["spools"] is not None:
        demand = {}
        for s in skus:
            if s["tier"] and s["plates_needed"] > 0 and s["material"]:
                d = demand.setdefault(s["material"], {"grams": 0.0, "sources": []})
                d["grams"] += s["plates_needed"] * s["plate_grams"] * fail
                d["sources"].append("sku:" + s["sku"])
        for rec, pl, req in plate_jobs:
            per = [(f["type"], f["grams"]) for f in pl["filaments"] if f["grams"]] or [(pl["filament_type"], pl.get("filament_grams"))]
            for mat, g in per:
                if mat and g:
                    d = demand.setdefault(mat, {"grams": 0.0, "sources": []})
                    d["grams"] += g * fail
                    d["sources"].append("plate:%s#%d" % (os.path.basename(rec["path"]), pl["index"]))
        sup_f, ledger_supply = audit_supply(demand, spools)
        findings += sup_f

    # state + delta
    delta, events = None, []
    if args.state:
        store = bc.StateStore(args.state)
        prev = store.get(STATE_KEY)
        cur = make_state(spools, skus, files, findings)
        if args.delta:
            if prev is None:
                delta = {"baseline": True, "events": [], "new": bc.sort_findings(findings), "changed": [], "resolved": []}
            elif degraded:
                delta = {"baseline": False, "skipped": "degraded run (%d input problem(s)); state not updated" % len(degraded),
                         "events": [], "new": [], "changed": [], "resolved": []}
            else:
                dd = diff_findings(prev.get("findings_by_key"), findings)
                events = compute_events(prev, cur)
                delta = {"baseline": False, "events": events, "new": dd["NEW"], "changed": dd["CHANGED"], "resolved": dd["RESOLVED"]}
        if not degraded:
            try:
                store.put(STATE_KEY, cur)
            except OSError as e:
                print("error: cannot write --state: %s" % e, file=err)
                return bc.EXIT_USAGE

    code = compute_exit(findings + events, bool(degraded), args.fail_on)
    plates_flat = [dict(pl, file=os.path.basename(rec["path"])) for rec in files for pl in rec["plates"]]
    by_bucket = {k: [f for f in findings if f.code in codes] for k, _, codes in BUCKETS}
    known = set().union(*(c for _, _, c in BUCKETS))
    by_bucket["other"] = [f for f in findings if f.code not in known]
    header = bc.snapshot_header(TOOL, ["local-files"], None, now=clock)
    D = {"header": header, "inputs": {"files": len(files), "plates": len(plates_flat), "spools": len(spools), "skus": len(skus),
                                      "printers": len(printers)},
         "findings": findings, "by_bucket": by_bucket, "plates": plates_flat, "ledger": ledger, "supply": ledger_supply, "skus": skus,
         "printers": printers, "load": load, "schedule": sched, "plate_costs": plate_costs, "delta": delta, "degraded": degraded,
         "code": code, "fail_on": args.fail_on}
    if fmt == "json":
        c = _counts(findings)
        payload = {"tool": TOOL, "version": VERSION, "retrieved_at": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "files": [{"path": r["path"], "kind": r["kind"], "sha256": r["sha256"], "slicer": r["slicer"],
                              "settings_source": r["settings_source"], "archive_violation": r["archive_violation"], "problems": r["problems"],
                              "plates": [{k: v for k, v in pl.items()} for pl in r["plates"]]} for r in files],
                   "spools": ledger, "supply": ledger_supply,
                   "skus": [{k: v for k, v in s.items() if k != "req"} for s in skus], "plate_costs": plate_costs,
                   "printers": [dict(p, queued_hours=load.get(p["printer_id"], 0.0)) for p in printers], "schedule": sched,
                   "summary": {"counts": c, "worst": _worst(findings), "events": [f.to_dict() for f in events]},
                   "findings": [f.to_dict() for f in bc.sort_findings(findings)],
                   "delta": None if delta is None else {k: ([f.to_dict() for f in v] if isinstance(v, list) else v) for k, v in delta.items()},
                   "exit_code": code, "fail_on": args.fail_on, "degraded_sources": degraded, "scope_note": SCOPE_NOTE}
        text = json.dumps(clean(payload), indent=2, sort_keys=True, ensure_ascii=False, default=str)
    else:
        text = render(D, fmt == "markdown")
    try:
        if args.queue_csv:
            write_atomic(args.queue_csv, queue_csv_text(sched))
            print("wrote %s (%d queue row(s))" % (args.queue_csv, len(sched)), file=err)
        if args.out:
            write_atomic(args.out, text + "\n")
            print("wrote %s (exit_code %d)" % (args.out, code), file=err)
        else:
            print(text, file=out)
    except OSError as e:
        print("error: cannot write output: %s" % visible(e, 120), file=err)
        return bc.EXIT_USAGE
    return code


def main():
    try:
        code = run(sys.argv[1:])
    except Exception as e:  # never leak a traceback (or an input value) as if it were a finding exit code
        print("error: unexpected %s while auditing; run is degraded" % type(e).__name__, file=sys.stderr)
        code = bc.EXIT_DEGRADED
    sys.exit(code)


if __name__ == "__main__":
    main()
