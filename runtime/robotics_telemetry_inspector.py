#!/usr/bin/env python3
"""Humanoid & Fleet Telemetry Inspector (strictly read-only, offline, stdlib + bot_common).

Audits exported robot / fleet telemetry across four buckets:
  1. actuator, joint and harmonic-drive duty cycles (--actuators, cross-checked against --urdf joint limits and --can-log)
  2. 16S LiFePO4 / LiPo battery-pack BMS telemetry (--bms)
  3. ROS 2 bag (.db3 SQLite, .mcap, metadata.yaml / JSON summary) topic rates, QoS, clock and TF-tree integrity (--rosbag)
  4. PX4 ULog (.ulg) / ArduPilot (.bin / CSV / JSON) EKF, IMU vibration, GNSS, ESC and failsafe readiness (--flight-log),
     plus a preventive-maintenance and flight-readiness checklist CSV (--maintenance-out)
It makes no network request, never issues motor / actuator commands, CAN frames, MAVLink parameter writes or BMS register
writes, never modifies an input file, and refuses credentials, tokens, MAVLink signing keys, DDS security private keys,
Wi-Fi passwords and SSH keys (exit 10, value never echoed).
Exit codes: 0 clean, 1 MEDIUM (or LOW/INFO with --fail-on low/info), 2 HIGH, 3 CRITICAL, 10 bad input / refusal,
11 degraded (unreadable / unsafe / malformed input, or nothing to audit).

Tables (CSV or JSON array of objects; header names are case-insensitive):
  --actuators joint_name,actuator_id,stator_temp_c,max_temp_c,rms_torque_nm,peak_torque_nm,rated_torque_nm,limit_torque_nm,
              velocity_rad_s,limit_velocity_rad_s,duty_cycle_pct,operating_hours,service_interval_hrs,backlash_arcmin,
              max_backlash_arcmin,harmonic_cycles,error_flags   (optional: position_rad, rated_harmonic_cycles)
  --bms       pack_id,chemistry,series_cells,pack_voltage_v,current_a,capacity_ah,max_c_rate,cell_min_mv,cell_max_mv,
              cell_voltages_mv,load_state,mosfet_temp_c,pack_temp_c,soh_pct,cycle_count,balance_active
  --flight-log ekf_vel_innov,ekf_pos_innov,ekf_hgt_innov,ekf_mag_innov,imu_vib_x,imu_vib_y,imu_vib_z,imu_clip_count,
              gnss_fix_type,gnss_satellites,gnss_hdop,esc_id,esc_rpm,esc_temp_c,esc_current_a,esc_desync_count,
              failsafe_active,prearm_error
"""
import argparse
import csv
import datetime
import html
import io
import json
import math
import os
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import types
import zipfile
import xml.etree.ElementTree as ET
from collections import deque

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

TOOL = "robotics-telemetry-inspector"
VERSION = "1.0.0"
STATE_KEY = "robotics-telemetry-inspector"
SCOPE_NOTE = ("SCOPE_NOTE: 100% offline, local, read-only inspection of the actuator, BMS, CAN / RS485, ROS 2 bag "
              "(.db3 / .mcap / metadata), PX4 ULog / ArduPilot and URDF / SDF files you supply. Findings are offline diagnostic "
              "heuristics on exported logs, not airworthiness, FAA / EASA flight certification, or functional-safety "
              "(ISO 13849 / IEC 61508 / ISO 10218) approval. No network request is made, and the tool never issues "
              "motor or actuator commands, CAN frames, MAVLink parameter writes or BMS register writes; it never modifies an "
              "input file (--state, --out and --maintenance-out only write new files and refuse any input path). Credentials, "
              "tokens, MAVLink signing keys, ROS 2 DDS security private keys, Wi-Fi passwords and SSH keys are refused "
              "(exit 10) or redacted, and never read back or echoed.")

SEV_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
MAX_TABLE_BYTES = 25_000_000        # actuator / BMS / flight tables
MAX_CAN_BYTES = 50_000_000          # CAN / RS485 logs
MAX_META_BYTES = 5_000_000          # bag metadata, URDF / SDF, state file
MAX_LOG_BYTES = 64 * 1024 * 1024    # .ulg / ArduPilot .bin (read fully into memory)
MAX_BAG_BYTES = 2 * 1024 ** 3       # .db3 / .mcap (never read fully into memory)
MAX_ROWS = 100_000
MAX_CAN_FRAMES = 500_000
MAX_BAG_MESSAGES = 2_000_000
MAX_LOG_RECORDS = 500_000
MAX_DISCOVERED = 500
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build"}
# ZIP hardening limits (read at call time so tests can lower them)
MAX_ENTRIES = 500
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RATIO = 100.0
MIN_RATIO_BYTES = 64 * 1024         # tiny entries can legitimately compress >100:1
HEARTBEAT_TIMEOUT_S = 1.0
CAN_BITRATE = 1_000_000
TF_SAMPLE_MESSAGES = 2000           # /tf messages decoded per bag (all of /tf_static)
EXTS = {"actuators": (".csv", ".json"), "bms": (".csv", ".json"), "flight": (".csv", ".json", ".ulg", ".bin"),
        "rosbag": (".db3", ".mcap", ".json", ".yaml", ".yml"), "urdf": (".urdf", ".sdf", ".xml"),
        "can": (".csv", ".json", ".log")}
ROLES = ("actuators", "bms", "rosbag", "flight", "urdf", "can")
ROLE_FLAG = {"actuators": "--actuators", "bms": "--bms", "rosbag": "--rosbag", "flight": "--flight-log", "urdf": "--urdf",
             "can": "--can-log"}


class UsageError(Exception):
    pass


class RowError(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def visible(s, limit=100):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s))
    return s if len(s) <= limit else s[:limit - 3] + "..."


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


def fd(x, nd=1):
    """Format a number for reports; None -> '-', inf -> 'inf'."""
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    if isinstance(x, float):
        return ("%." + str(nd) + "f") % x
    return str(x)



def norm_key(k):
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")



# ---------------------------------------------------------------- credential / key refusal
SECRET_PATTERNS = (
    ("OpenAI/Anthropic-style API key (sk-)", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}")),
    ("WattCoin key (wck_)", re.compile(r"(?<![A-Za-z0-9])wck_[A-Za-z0-9]{6,}")),
    ("WattCoin license key (wcl_)", re.compile(r"(?<![A-Za-z0-9])wcl_[A-Za-z0-9]{6,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{12,})")),
    ("JWT / bearer token (eyJ)", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}")),
    ("64-hex key (MAVLink signing key / private key)", re.compile(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")),
    ("cloud access key", re.compile(r"AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9\-]{10,}")),
    ("PEM / OpenSSH / DDS private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("SSH key material", re.compile(r"(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+)\s+AAAA[0-9A-Za-z+/=]{20,}")),
)
_CRED_NAMES = (r"(?:wifi|wi[_\- ]?fi|wlan|wpa\d?|ssid)[_\- ]?(?:password|passwd|pass|psk|key|passphrase)|wpa[_\- ]?psk|psk|"
               r"ssh[_\- ]?(?:private[_\- ]?)?(?:key|pass(?:word|phrase)?)|id[_\- ]?rsa|"
               r"(?:mavlink|mav)[_\- ]?(?:signing|sign)?[_\- ]?(?:key|secret|passphrase)|signing[_\- ]?(?:key|secret)|"
               r"secure[_\- ]?connection[_\- ]?key|"
               r"(?:dds|sros2?|ros[_\- ]?security|enclave)[_\- ]?(?:security[_\- ]?)?(?:private[_\- ]?)?(?:key|keystore|password)|"
               r"private[_\- ]?key|api[_\- ]?key|apikey|api[_\- ]?token|(?:auth|access|bearer|refresh|session)[_\- ]?token|token|"
               r"client[_\- ]?secret|secret(?:[_\- ]?key)?|seed[_\- ]?phrase|mnemonic|passphrase|passwd|password")
CRED_FLAG_RE = re.compile(r"^-{0,2}(?:%s)$" % _CRED_NAMES, re.I)
CRED_ASSIGN_RE = re.compile(r"(?:^|[\s,;&?\"'{])-{0,2}(?:%s)\s*[=:]\s*[\"']?[^\s\"',;}]" % _CRED_NAMES, re.I)
SEED_WORD_RE = re.compile(r"^[a-z]{3,8}$")


def _is_seed_phrase(s):
    words = s.split()
    return len(words) in (12, 15, 18, 21, 24) and all(SEED_WORD_RE.match(w) for w in words)


def secret_kind(value, allow_seed=True):
    """Name of the credential class `value` looks like, else None. The value itself is never returned."""
    s = str(value)
    if len(s) < 5:
        return None
    for kind, rx in SECRET_PATTERNS:
        if rx.search(s):
            return kind
    if CRED_FLAG_RE.match(s.strip()) or CRED_ASSIGN_RE.search(s):
        return "credential / key assignment"
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


def scan_text_lines(text, label):
    """Refuse (exit 10) a free-text telemetry log (candump) whose lines look like credentials."""
    for n, line in enumerate(text.splitlines(), 1):
        if secret_kind(line, allow_seed=False):
            raise UsageError("refusing to run: %s line %d looks like a credential or key; value not echoed" % (label, n))


def credential_findings(source, items):
    """EMBEDDED_TELEMETRY_CREDENTIAL (HIGH) for metadata fields that look like secrets. items: iterable of (field, value).

    The value is never stored or shown, only the (sanitised) field name and the credential class.
    """
    out, seen = [], set()
    for field, value in items:
        if value is None or isinstance(value, (bool, int, float)):
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if CRED_FLAG_RE.match(norm_key(re.split(r"[:./\[]", str(field))[-1] or str(field))) and text.strip() and text.strip().lower() not in ("none", "null", "false", "0"):
            kind = "credential field"
        else:
            kind = secret_kind(text, allow_seed=False)
        if not kind:
            continue
        shown = visible(field, 40) if not secret_kind(field, allow_seed=False) else "field#%d" % (len(seen) + 1)
        if (source, shown) in seen:
            continue
        seen.add((source, shown))
        out.append(mk("HIGH", "EMBEDDED_TELEMETRY_CREDENTIAL",
                      "%s: field %s holds what looks like a %s (value redacted); rotate it and re-export the log without it"
                      % (visible(source, 60), shown, kind),
                      key="EMBEDDED_TELEMETRY_CREDENTIAL:%s:%s" % (visible(source, 60), shown), source=visible(source, 60),
                      field=shown, kind=kind))
    return out


def walk_doc(doc, prefix="", limit=5000, _out=None):
    """Flatten a parsed JSON/YAML document into [(dotted.path, scalar)] so metadata can be scanned for credentials."""
    out = _out if _out is not None else []
    if len(out) >= limit:
        return out
    if isinstance(doc, dict):
        for k, v in doc.items():
            walk_doc(v, "%s.%s" % (prefix, k) if prefix else str(k), limit, out)
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            walk_doc(v, "%s[%d]" % (prefix, i), limit, out)
    elif doc is not None:
        out.append((prefix, doc))
    return out


# ---------------------------------------------------------------- bounded file reading
def read_regular(path, cap):
    """Bytes of a regular, non-symlink file of at most `cap` bytes (O_NOFOLLOW; no TOCTOU between check and read)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd_ = os.open(path, flags)
    try:
        st = os.fstat(fd_)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > cap:
            raise ValueError("file larger than %d bytes" % cap)
        with os.fdopen(fd_, "rb") as fh:
            fd_ = -1
            data = fh.read(cap + 1)
    finally:
        if fd_ != -1:
            os.close(fd_)
    if len(data) > cap:
        raise ValueError("file larger than %d bytes" % cap)
    return data


def open_regular(path, cap):
    """Open a regular, non-symlink file for streaming -> binary file object (size checked against `cap`)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd_ = os.open(path, flags)
    try:
        st = os.fstat(fd_)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > cap:
            raise ValueError("file larger than %d bytes" % cap)
        fh = os.fdopen(fd_, "rb")
        fd_ = -1
        return fh
    finally:
        if fd_ != -1:
            os.close(fd_)


def decode_text(data):
    return data.decode("utf-8-sig")


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
        if norm.startswith("/") or ".." in norm.split("/") or re.match(r"^[A-Za-z]:", norm) or "\x00" in name:
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


def read_member(zf, zi, cap):
    with zf.open(zi, "r") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise ValueError("member %r larger than %d bytes" % (visible(zi.filename, 60), cap))
    return data


# ---------------------------------------------------------------- XML (URDF / SDF) hardening
_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.I)
_ENC_RE = re.compile(rb"^\s*<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)[\"']", re.I)


def safe_xml(data):
    """Parse XML with xml.etree after refusing DTD / entity declarations and exotic encodings (XML-bomb / XXE guard)."""
    if b"\x00" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        raise ValueError("XML DTD / entity declarations are not accepted (UTF-16/32 or NUL bytes cannot be screened)")
    if _DTD_RE.search(data):
        raise ValueError("XML DTD / entity declarations are not accepted")
    m = _ENC_RE.match(data)
    if m and m.group(1).decode("ascii", "ignore").lower().replace("_", "-") not in ("utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1"):
        raise ValueError("XML encoding %r is not accepted" % visible(m.group(1).decode("ascii", "ignore"), 20))
    return ET.fromstring(data)


# ---------------------------------------------------------------- tables
def load_table_bytes(data, plural):
    """-> (list of normalised-key dict rows | None, error | None)."""
    try:
        text = decode_text(data)
    except UnicodeDecodeError as e:
        return None, "not UTF-8 text (%s)" % visible(e, 60)
    if text.lstrip()[:1] in ("[", "{"):
        try:
            doc = json.loads(text)
        except ValueError as e:
            return None, "malformed JSON (%s)" % visible(e, 80)
        if isinstance(doc, dict):
            doc = next((doc[k] for k in (plural, "rows", "items", "data", "records", "frames", "samples") if isinstance(doc.get(k), list)), None)
        if not isinstance(doc, list) or not all(isinstance(x, dict) for x in doc):
            return None, "expected a JSON array of objects (or an object holding one under %r)" % plural
        rows = [{norm_key(k): v for k, v in x.items()} for x in doc]
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        try:
            if not reader.fieldnames:
                return None, "empty CSV"
            rows = [{norm_key(k): v for k, v in r.items() if k is not None} for r in reader]
        except csv.Error as e:
            return None, "malformed CSV (%s)" % visible(e, 80)
    if len(rows) > MAX_ROWS and plural != "frames":
        return None, "more than %d rows" % MAX_ROWS
    if len(rows) > MAX_CAN_FRAMES:
        return None, "more than %d rows" % MAX_CAN_FRAMES
    return rows, None


def _txt(v):
    return "" if v is None else str(v).strip()


def num(r, key, default=None, lo=-1e9, hi=1e9):
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


def _f(v):
    try:
        return fnum(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- restricted YAML (rosbag2 metadata.yaml subset)
def _yaml_strip_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q and s[i - 1] != "\\":
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def _yaml_scalar(v):
    v = v.strip()
    if v == "":
        return None
    if v[0] in "&*!|>%@`":
        raise ValueError("unsupported YAML construct")
    if v[0] == '"':
        if v.endswith('"') and len(v) >= 2:
            try:
                return json.loads(v)
            except ValueError:
                return v[1:-1]
        raise ValueError("unterminated YAML string")
    if v[0] == "'":
        if v.endswith("'") and len(v) >= 2:
            return v[1:-1].replace("''", "'")
        raise ValueError("unterminated YAML string")
    if v[0] == "{" and v.endswith("}"):
        d = {}
        for part in [p for p in v[1:-1].split(",") if p.strip()]:
            k, sep, val = part.partition(":")
            if not sep:
                raise ValueError("bad flow mapping")
            d[k.strip()] = _yaml_scalar(val)
        return d
    if v[0] == "[" and v.endswith("]"):
        return [_yaml_scalar(p) for p in v[1:-1].split(",") if p.strip()]
    low = v.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "false"):
        return low == "true"
    try:
        if re.fullmatch(r"[-+]?\d+", v):
            return int(v)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", v):
            return float(v)
    except ValueError:
        pass
    return v


_KV_SPLIT = re.compile(r"^((?:\"[^\"]*\"|'[^']*'|[^\s:\"'#\[\]{}][^:]*?)):(?:\s+(.*))?$")


def mini_yaml(text, max_lines=20000, max_depth=24):
    """Parse the block-style YAML subset used by rosbag2 metadata.yaml (mappings, sequences, quoted/flow scalars)."""
    lines = []
    for raw in text.splitlines()[:max_lines]:
        lead = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in lead:
            raise ValueError("tab indentation")
        s = _yaml_strip_comment(raw).rstrip()
        if not s.strip() or s.strip() in ("---", "..."):
            continue
        lines.append([len(s) - len(s.lstrip(" ")), s.strip()])
    n = len(lines)

    def kv(s):
        m = _KV_SPLIT.match(s)
        if not m:
            return None
        k = m.group(1).strip()
        if k[:1] in "\"'":
            k = k[1:-1]
        return k, (m.group(2) or "").strip()

    def block(i, depth):
        if depth > max_depth:
            raise ValueError("YAML nesting too deep")
        s = lines[i][1]
        return seq(i, lines[i][0], depth) if (s.startswith("- ") or s == "-") else mapping(i, lines[i][0], depth)

    def seq(i, ind, depth):
        out = []
        while i < n and lines[i][0] == ind and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest = lines[i][1][1:].strip()
            if not rest:
                if i + 1 < n and lines[i + 1][0] > ind:
                    v, i = block(i + 1, depth + 1)
                else:
                    v, i = None, i + 1
            elif kv(rest) is not None and not rest.startswith(("{", "[", '"', "'")):
                lines[i] = [ind + 2, rest]
                v, i = mapping(i, ind + 2, depth + 1)
            else:
                v, i = _yaml_scalar(rest), i + 1
            out.append(v)
        return out, i

    def mapping(i, ind, depth):
        d = {}
        while i < n and lines[i][0] == ind and not lines[i][1].startswith("- "):
            pair = kv(lines[i][1])
            if pair is None:
                raise ValueError("unsupported YAML line")
            k, v = pair
            if v == "":
                if i + 1 < n and (lines[i + 1][0] > ind or (lines[i + 1][0] == ind and lines[i + 1][1].startswith("- "))):
                    val, i = block(i + 1, depth + 1)
                else:
                    val, i = None, i + 1
            else:
                val, i = _yaml_scalar(v), i + 1
            d[k] = val
        return d, i

    if not lines:
        return None
    val, end = block(0, 0)
    if end < n:
        raise ValueError("unsupported YAML structure")
    return val


def load_doc(data):
    """JSON or restricted-YAML document -> object (raises ValueError)."""
    text = decode_text(data)
    if text.lstrip()[:1] in ("{", "["):
        return json.loads(text)
    return mini_yaml(text)


# ---------------------------------------------------------------- ROS 2 bag model
REL_NAMES = {0: "SYSTEM_DEFAULT", 1: "RELIABLE", 2: "BEST_EFFORT", 3: "UNKNOWN"}
DUR_NAMES = {0: "SYSTEM_DEFAULT", 1: "TRANSIENT_LOCAL", 2: "VOLATILE", 3: "UNKNOWN"}
HIST_NAMES = {0: "SYSTEM_DEFAULT", 1: "KEEP_LAST", 2: "KEEP_ALL", 3: "UNKNOWN"}
SENSOR_RE = re.compile(r"scan|lidar|imu|image|camera|depth|points|pointcloud|odom|fix|gps|gnss|battery|joint_state|"
                       r"range|sonar|radar|wrench|force|encoder|sensor|diagnostic", re.I)
CONTROL_RE = re.compile(r"cmd_vel|command|control|trajectory|effort|twist|setpoint|goal|actuator|motor|torque|velocity_controller|"
                        r"position_controller|estop|e_stop|emergency", re.I)
SAFETY_RE = re.compile(r"estop|e_stop|emergency|safety|heartbeat|watchdog", re.I)
HIGH_DEPTH = 100


def qos_enum(v, names, prefix):
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return names.get(int(v))
    s = str(v).strip().upper().replace("-", "_").replace(" ", "_")
    s = re.sub(r"^(?:RMW_)?QOS_POLICY_%s_|^%s_" % (prefix, prefix), "", s)
    if s.isdigit():
        return names.get(int(s))
    return s if s in names.values() else None


def qos_norm(d):
    """Normalise one QoS profile mapping (rosbag2 ints or names) -> {reliability, durability, history, depth}."""
    if not isinstance(d, dict):
        return None
    g = {norm_key(k): v for k, v in d.items()}
    depth = _f(g.get("depth"))
    out = {"reliability": qos_enum(g.get("reliability"), REL_NAMES, "RELIABILITY"),
           "durability": qos_enum(g.get("durability"), DUR_NAMES, "DURABILITY"),
           "history": qos_enum(g.get("history"), HIST_NAMES, "HISTORY"),
           "depth": int(depth) if depth is not None else None}
    return out if any(v is not None for v in out.values()) else None


def parse_qos_profiles(v):
    """offered_qos_profiles (YAML text, list or dict) -> list of normalised profiles."""
    if isinstance(v, str):
        if not v.strip():
            return []
        try:
            v = mini_yaml(v.replace("\\n", "\n")) if not v.lstrip().startswith(("[", "{")) else json.loads(v)
        except ValueError:
            return []
    if isinstance(v, dict):
        v = [v]
    out = []
    for p in (v if isinstance(v, list) else []):
        q = qos_norm(p)
        if q:
            out.append(q)
    return out


def new_topic(name, ttype=""):
    return {"name": name, "type": ttype, "count": 0, "first_ns": None, "last_ns": None, "max_gap_ns": None, "ts_back": 0,
            "stamp_back": 0, "prev_ns": None, "prev_stamp": None, "off_first": [], "off_last": deque(maxlen=40), "off_n": 0,
            "expected_hz": None, "required": False, "safety": False, "pub_qos": [], "sub_qos": [], "rate_hz": None,
            "offset_ms": None, "drift_ms": None, "known_gap_s": None, "has_counts": True, "encoding": ""}


def topic_update(t, ts, head):
    """Fold one message (bag timestamp ns + first <=12 bytes of the CDR payload) into the running topic statistics."""
    t["count"] += 1
    if t["first_ns"] is None or ts < t["first_ns"]:
        t["first_ns"] = ts
    if t["last_ns"] is None or ts > t["last_ns"]:
        t["last_ns"] = ts
    if t["prev_ns"] is not None:
        d = ts - t["prev_ns"]
        if d < 0:
            t["ts_back"] += 1
        elif t["max_gap_ns"] is None or d > t["max_gap_ns"]:
            t["max_gap_ns"] = d
    t["prev_ns"] = ts
    stamp = header_stamp_ns(head, t["type"])
    if stamp:
        if t["prev_stamp"] is not None and stamp < t["prev_stamp"] - 1_000_000:
            t["stamp_back"] += 1
        t["prev_stamp"] = stamp
        off = (ts - stamp) / 1e6
        t["off_n"] += 1
        if len(t["off_first"]) < 40:
            t["off_first"].append(off)
        else:
            t["off_last"].append(off)


def has_header(ttype):
    s = str(ttype or "")
    return s.startswith(("sensor_msgs/", "nav_msgs/")) or s.endswith("Stamped")


def header_stamp_ns(head, ttype):
    if not head or len(head) < 12 or not has_header(ttype) or head[0] != 0 or head[1] not in (0, 1):
        return 0
    sec, nsec = struct.unpack_from("<iI" if head[1] == 1 else ">iI", head, 4)
    if sec <= 0 or nsec >= 1_000_000_000:
        return 0
    return sec * 1_000_000_000 + nsec


def _median(xs):
    xs = sorted(xs)
    return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2.0)


def finish_topic(t, bag_duration_s):
    if t["off_n"]:
        allv = t["off_first"] + list(t["off_last"])
        t["offset_ms"] = _median(allv)
        if t["off_last"] and len(t["off_first"]) >= 10:
            t["drift_ms"] = _median(list(t["off_last"])) - _median(t["off_first"])
    if t["rate_hz"] is None and t["has_counts"]:
        if bag_duration_s and bag_duration_s > 0:
            t["rate_hz"] = t["count"] / bag_duration_s
    if t["max_gap_ns"] is not None and t["known_gap_s"] is None:
        t["known_gap_s"] = t["max_gap_ns"] / 1e9


class CDR(object):
    """Just enough of OMG CDR (as used by rosbag2) to read tf2_msgs/TFMessage frame ids."""

    def __init__(self, blob):
        if len(blob) < 8 or blob[0] != 0 or blob[1] not in (0, 1):
            raise ValueError("not a CDR payload")
        self.b, self.e, self.pos, self.base = blob, ("<" if blob[1] == 1 else ">"), 4, 4

    def align(self, n):
        self.pos = self.base + ((self.pos - self.base + n - 1) // n) * n

    def u32(self):
        self.align(4)
        if self.pos + 4 > len(self.b):
            raise ValueError("truncated CDR")
        v = struct.unpack_from(self.e + "I", self.b, self.pos)[0]
        self.pos += 4
        return v

    def string(self):
        n = self.u32()
        if n == 0 or n > 512 or self.pos + n > len(self.b):
            raise ValueError("bad CDR string")
        s = self.b[self.pos:self.pos + n - 1].decode("utf-8", "replace")
        self.pos += n
        return s

    def skip(self, align, n):
        self.align(align)
        self.pos += n


def tf_edges_from_cdr(blob):
    c = CDR(blob)
    count = c.u32()
    if count > 2000:
        raise ValueError("implausible TF message")
    out = []
    for _ in range(count):
        c.skip(4, 8)
        parent, child = c.string().lstrip("/"), c.string().lstrip("/")
        c.skip(8, 56)
        out.append((parent, child))
    return out


def new_bag(name, kind):
    return {"name": name, "kind": kind, "topics": {}, "tf": set(), "tf_static": set(), "duration_s": None, "start_ns": None,
            "end_ns": None, "messages": 0, "meta": [], "problems": [], "has_counts": True, "skew_ms": None}


def is_tf_topic(name):
    return name.rstrip("/").endswith("/tf") or name == "tf"


def is_tf_static(name):
    return name.rstrip("/").endswith("/tf_static") or name == "tf_static"


def finalize_bag(bag):
    firsts = [t["first_ns"] for t in bag["topics"].values() if t["first_ns"] is not None]
    lasts = [t["last_ns"] for t in bag["topics"].values() if t["last_ns"] is not None]
    if firsts and lasts and bag["duration_s"] is None:
        bag["start_ns"], bag["end_ns"] = min(firsts), max(lasts)
        bag["duration_s"] = (bag["end_ns"] - bag["start_ns"]) / 1e9
    for t in bag["topics"].values():
        finish_topic(t, bag["duration_s"])
    return bag


# ---- SQLite .db3
def _sqlite_open(path=None, data=None):
    if data is not None:
        con = sqlite3.connect(":memory:")
        if not hasattr(con, "deserialize"):
            con.close()
            raise ValueError("this Python cannot open a .db3 inside an archive (sqlite3 deserialize unavailable)")
        con.deserialize(data)
    else:
        uri = "file:%s?mode=ro&immutable=1" % os.path.abspath(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    budget = [0]

    def progress():
        budget[0] += 1
        return 1 if budget[0] > 20000 else 0   # ~2e9 VM steps: abort pathological queries

    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

    def authorize(action, arg1, *rest):
        if action in allowed or (action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    con.execute("PRAGMA query_only=ON")
    con.set_authorizer(authorize)
    con.set_progress_handler(progress, 100000)
    return con


def parse_db3(name, path=None, data=None):
    bag = new_bag(name, "db3")
    head = data[:16] if data is not None else None
    if head is None:
        fh = open_regular(path, MAX_BAG_BYTES)
        try:
            head = fh.read(16)
        finally:
            fh.close()
    if not head.startswith(b"SQLite format 3\x00"):
        raise ValueError("not a SQLite 3 database")
    con = _sqlite_open(path, data)
    try:
        tcols = {r[1] for r in con.execute("PRAGMA table_info(topics)")}
        mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        if not {"id", "name", "type"} <= tcols or not {"topic_id", "timestamp", "data"} <= mcols:
            raise ValueError("no rosbag2 topics/messages tables")
        qcol = "offered_qos_profiles" if "offered_qos_profiles" in tcols else "''"
        ecol = "serialization_format" if "serialization_format" in tcols else "''"
        ids = {}
        for tid, tname, ttype, qos, enc in con.execute("SELECT id, name, type, %s, %s FROM topics ORDER BY id LIMIT 10000" % (qcol, ecol)):
            t = new_topic(str(tname), str(ttype or ""))
            t["pub_qos"] = parse_qos_profiles(qos)
            t["encoding"] = str(enc or "")
            bag["topics"][t["name"]] = t
            ids[tid] = t
            bag["meta"].append(("topic:" + str(tname), str(ttype or "")))
            bag["meta"].append(("qos:" + str(tname), str(qos or "")[:2000]))
        total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total > MAX_BAG_MESSAGES:
            bag["problems"].append("bag has %d messages; only the first %d were analysed" % (total, MAX_BAG_MESSAGES))
        tf_ids = {i for i, t in ids.items() if is_tf_topic(t["name"])}
        static_ids = {i for i, t in ids.items() if is_tf_static(t["name"])}
        cur = con.execute("SELECT topic_id, timestamp, substr(data, 1, 12) FROM messages ORDER BY id LIMIT ?", (MAX_BAG_MESSAGES,))
        for tid, ts, head12 in cur:
            t = ids.get(tid)
            if t is not None and isinstance(ts, int):
                topic_update(t, ts, bytes(head12) if head12 is not None else b"")
        bag["messages"] = sum(t["count"] for t in ids.values())
        for group, edge_set, limit in ((static_ids, bag["tf_static"], 500), (tf_ids, bag["tf"], TF_SAMPLE_MESSAGES)):
            for tid in sorted(group):
                bad = 0
                for (blob,) in con.execute("SELECT data FROM messages WHERE topic_id=? ORDER BY id LIMIT ?", (tid, limit)):
                    try:
                        edge_set.update(tf_edges_from_cdr(bytes(blob)))
                    except (ValueError, struct.error):
                        bad += 1
                if bad:
                    bag["problems"].append("%d undecodable %s message(s)" % (bad, ids[tid]["name"]))
    except sqlite3.Error as e:
        raise ValueError("SQLite error (%s)" % visible(e, 80))
    finally:
        con.close()
    return finalize_bag(bag)


# ---- MCAP
MCAP_MAGIC = b"\x89MCAP0\r\n"


def _mcap_str(b, pos):
    n = struct.unpack_from("<I", b, pos)[0]
    if pos + 4 + n > len(b):
        raise ValueError("bad MCAP string")
    return b[pos + 4:pos + 4 + n].decode("utf-8", "replace"), pos + 4 + n


def _mcap_map(b, pos):
    n = struct.unpack_from("<I", b, pos)[0]
    end, pos, out = pos + 4 + n, pos + 4, {}
    if end > len(b):
        raise ValueError("bad MCAP map")
    while pos < end:
        k, pos = _mcap_str(b, pos)
        v, pos = _mcap_str(b, pos)
        out[k] = v
    return out, end


def parse_mcap(name, fh):
    bag = new_bag(name, "mcap")
    fh.seek(0, 2)
    size = fh.tell()
    fh.seek(0)
    if fh.read(8) != MCAP_MAGIC:
        raise ValueError("not an MCAP file (bad magic)")
    schemas, channels, compressed = {}, {}, set()
    state = {"records": 0, "msgs": 0, "tf_msgs": 0, "tail": False}

    def chan_topic(cid):
        ch = channels.get(cid)
        if ch is None:
            return None
        t = bag["topics"].get(ch["topic"])
        if t is None:
            t = new_topic(ch["topic"], schemas.get(ch["schema_id"], ("", ""))[0])
            t["encoding"] = ch["encoding"]
            t["pub_qos"] = parse_qos_profiles(ch["meta"].get("offered_qos_profiles", ""))
            bag["topics"][ch["topic"]] = t
            for k, v in ch["meta"].items():
                bag["meta"].append(("channel:%s:%s" % (ch["topic"], k), v[:2000]))
        return t

    def handle(op, body, in_chunk):
        if op == 0x03:
            sid = struct.unpack_from("<H", body, 0)[0]
            sname, p = _mcap_str(body, 2)
            enc, _ = _mcap_str(body, p)
            schemas[sid] = (sname, enc)
        elif op == 0x04:
            cid, sid = struct.unpack_from("<HH", body, 0)
            topic, p = _mcap_str(body, 4)
            enc, p = _mcap_str(body, p)
            meta, _ = _mcap_map(body, p)
            channels[cid] = {"topic": topic, "schema_id": sid, "encoding": enc, "meta": meta}
            chan_topic(cid)
        elif op == 0x05 and state["msgs"] < MAX_BAG_MESSAGES:
            cid, _seq, log_time = struct.unpack_from("<HIQ", body, 0)
            t = chan_topic(cid)
            if t is None:
                return
            state["msgs"] += 1
            payload = body[22:]
            topic_update(t, log_time, payload[:12])
            if is_tf_static(t["name"]) or (is_tf_topic(t["name"]) and state["tf_msgs"] < TF_SAMPLE_MESSAGES):
                state["tf_msgs"] += 0 if is_tf_static(t["name"]) else 1
                try:
                    (bag["tf_static"] if is_tf_static(t["name"]) else bag["tf"]).update(tf_edges_from_cdr(payload))
                except (ValueError, struct.error):
                    state["bad_tf"] = state.get("bad_tf", 0) + 1
        elif op == 0x0B:
            mc, sc, cc, ac, mdc, ckc, ms, me = struct.unpack_from("<QHIIIIQQ", body, 0)
            counts, _ = _mcap_map_u16(body, 8 + 2 + 4 * 4 + 16)
            stats_box.update({"messages": mc, "start": ms, "end": me, "counts": counts})
        elif op == 0x0C:
            mname, p = _mcap_str(body, 0)
            meta, _ = _mcap_map(body, p)
            for k, v in meta.items():
                bag["meta"].append(("metadata:%s:%s" % (mname, k), v[:2000]))
        elif op == 0x06 and not in_chunk:
            ms, me, usize, crc = struct.unpack_from("<QQQI", body, 0)
            comp, p = _mcap_str(body, 28)
            rlen = struct.unpack_from("<Q", body, p)[0]
            recs = body[p + 8:p + 8 + rlen]
            if comp:
                compressed.add(comp)
                return
            walk(recs, True)

    stats_box = {}

    def walk(buf, in_chunk):
        pos = 0
        while pos + 9 <= len(buf) and state["records"] < 5_000_000:
            op = buf[pos]
            ln = struct.unpack_from("<Q", buf, pos + 1)[0]
            if pos + 9 + ln > len(buf):
                raise ValueError("truncated MCAP chunk")
            state["records"] += 1
            handle(op, buf[pos + 9:pos + 9 + ln], in_chunk)
            pos += 9 + ln

    while True:
        hdr = fh.read(9)
        if len(hdr) < 9:
            break
        op, ln = hdr[0], struct.unpack("<Q", hdr[1:])[0]
        here = fh.tell()
        if here + ln > size:
            bag["problems"].append("truncated MCAP record (file cut short)")
            break
        state["records"] += 1
        if state["records"] > 5_000_000:
            bag["problems"].append("MCAP record limit reached")
            break
        if op == 0x02:
            fh.seek(ln, 1)
            state["tail"] = fh.read(8) == MCAP_MAGIC
            break
        if op in (0x03, 0x04, 0x0B, 0x0C) or (op == 0x06 and ln <= 256 * 1024 * 1024) or op == 0x05:
            if ln > 64 * 1024 * 1024:
                fh.seek(ln, 1)
                bag["problems"].append("oversized MCAP record skipped")
                continue
            body = fh.read(ln)
            try:
                handle(op, body, False)
            except (struct.error, ValueError, IndexError):
                bag["problems"].append("undecodable MCAP record (opcode 0x%02x)" % op)
        else:
            fh.seek(ln, 1)
    if not state["tail"]:
        bag["problems"].append("MCAP footer missing (file truncated?); results use the records that could be read")
    if compressed:
        bag["problems"].append("MCAP chunk compression %s is not supported (stdlib only); message-level checks use the summary statistics"
                               % "/".join(sorted(compressed)))
    if state.get("bad_tf"):
        bag["problems"].append("%d undecodable tf message(s)" % state["bad_tf"])
    if stats_box:
        dur = (stats_box["end"] - stats_box["start"]) / 1e9 if stats_box["end"] > stats_box["start"] else None
        if dur:
            bag["duration_s"], bag["start_ns"], bag["end_ns"] = dur, stats_box["start"], stats_box["end"]
        for cid, cnt in stats_box["counts"].items():
            t = chan_topic(cid)
            if t is not None and t["count"] < cnt:
                t["count"] = cnt     # compressed chunks: message bodies unreadable, summary count is authoritative
        bag["messages"] = stats_box["messages"]
    else:
        bag["messages"] = state["msgs"]
    return finalize_bag(bag)


def _mcap_map_u16(b, pos):
    n = struct.unpack_from("<I", b, pos)[0]
    end, pos, out = pos + 4 + n, pos + 4, {}
    if end > len(b):
        raise ValueError("bad MCAP map")
    while pos + 10 <= end:
        k, v = struct.unpack_from("<HQ", b, pos)
        out[k] = v
        pos += 10
    return out, end


# ---- JSON / YAML metadata summary
def _truthy_flag(v):
    return bool(tristate(v))


def parse_bag_summary(name, doc):
    """JSON/YAML bag summary (custom schema or a native rosbag2 metadata.yaml) -> bag model."""
    if not isinstance(doc, dict):
        raise ValueError("expected a mapping")
    bag = new_bag(name, "summary")
    bag["meta"] = walk_doc(doc)
    info = doc.get("rosbag2_bagfile_information")
    topics_in, edges_in = [], []
    if isinstance(info, dict):
        dur = (info.get("duration") or {}).get("nanoseconds") if isinstance(info.get("duration"), dict) else None
        st = (info.get("starting_time") or {}).get("nanoseconds_since_epoch") if isinstance(info.get("starting_time"), dict) else None
        if _f(dur):
            bag["duration_s"] = _f(dur) / 1e9
        if _f(st) is not None:
            bag["start_ns"] = int(_f(st))
        for ent in info.get("topics_with_message_count") or []:
            tm = ent.get("topic_metadata") if isinstance(ent, dict) and isinstance(ent.get("topic_metadata"), dict) else {}
            topics_in.append({"name": tm.get("name"), "type": tm.get("type"), "message_count": ent.get("message_count"),
                              "offered_qos_profiles": tm.get("offered_qos_profiles"), "encoding": tm.get("serialization_format")})
    else:
        topics_in = doc.get("topics") if isinstance(doc.get("topics"), list) else []
        edges_in = doc.get("tf_edges") if isinstance(doc.get("tf_edges"), list) else []
        d = _f(doc.get("duration_s"))
        if d is None and _f(doc.get("duration_ns")) is not None:
            d = _f(doc.get("duration_ns")) / 1e9
        bag["duration_s"] = d
        bag["skew_ms"] = _f(doc.get("clock_skew_ms"))
    for ent in topics_in:
        if not isinstance(ent, dict):
            continue
        g = {norm_key(k): v for k, v in ent.items()}
        tname = _txt(g.get("name") or g.get("topic"))
        if not tname:
            continue
        t = new_topic(tname, _txt(g.get("type")))
        cnt = _f(g.get("message_count", g.get("count", g.get("messages"))))
        t["has_counts"] = cnt is not None or _f(g.get("rate_hz", g.get("avg_rate_hz"))) is not None
        t["count"] = int(cnt) if cnt is not None else 0
        t["expected_hz"] = _f(g.get("expected_hz"))
        t["rate_hz"] = _f(g.get("rate_hz", g.get("avg_rate_hz")))
        t["known_gap_s"] = _f(g.get("max_gap_s"))
        t["required"] = _truthy_flag(g.get("required"))
        t["safety"] = _truthy_flag(g.get("safety")) or _truthy_flag(g.get("critical"))
        t["offset_ms"] = _f(g.get("header_offset_ms"))
        t["drift_ms"] = _f(g.get("header_drift_ms"))
        t["ts_back"] = int(_f(g.get("backward_jumps")) or 0)
        t["encoding"] = _txt(g.get("encoding"))
        pq, sq = g.get("publisher_qos", g.get("publishers")), g.get("subscriber_qos", g.get("subscribers"))
        qos = g.get("qos") if isinstance(g.get("qos"), dict) else {}
        pq = pq if pq is not None else qos.get("publisher", qos.get("publishers", g.get("offered_qos_profiles")))
        sq = sq if sq is not None else qos.get("subscriber", qos.get("subscribers"))
        t["pub_qos"], t["sub_qos"] = parse_qos_profiles(pq), parse_qos_profiles(sq)
        bag["topics"][tname] = t
    for e in edges_in:
        if isinstance(e, dict) and e.get("parent") and e.get("child"):
            (bag["tf_static"] if _truthy_flag(e.get("static")) else bag["tf"]).add((str(e["parent"]).lstrip("/"), str(e["child"]).lstrip("/")))
    bag["has_counts"] = any(t["has_counts"] for t in bag["topics"].values())
    for t in bag["topics"].values():
        if t["rate_hz"] is None and t["has_counts"] and bag["duration_s"]:
            t["rate_hz"] = t["count"] / bag["duration_s"]
        if t["first_ns"] is None and bag["start_ns"] is not None and t["count"]:
            t["first_ns"] = bag["start_ns"]
    if not bag["topics"] and not bag["tf"] and not bag["tf_static"]:
        raise ValueError("no topics or tf_edges found")
    return bag


def merge_expectations(measured, specs, problems):
    """Apply expectation-only summaries (expected_hz / required / subscriber QoS) onto the measured bags they describe.

    A spec applies to every bag sharing at least one topic with it; when none does and there is exactly one bag it applies
    to that bag (so a missing required topic shows up as a dropout).
    """
    for spec in specs:
        targets = [b for b in measured if set(spec["topics"]) & set(b["topics"])]
        if not targets and len(measured) == 1:
            targets = measured
        if not targets:
            problems.append("%s: expectations metadata matches no bag; not applied" % visible(spec["name"], 60))
            continue
        for bag in targets:
            for name, st in spec["topics"].items():
                t = bag["topics"].get(name)
                if t is None:
                    if st["expected_hz"] is None and not (st["required"] or st["safety"]):
                        continue
                    t = new_topic(name, st["type"])
                    bag["topics"][name] = t
                if st["expected_hz"] is not None:
                    t["expected_hz"] = st["expected_hz"]
                t["required"] = t["required"] or st["required"]
                t["safety"] = t["safety"] or st["safety"]
                if st["sub_qos"]:
                    t["sub_qos"] = st["sub_qos"]
                if st["pub_qos"] and not t["pub_qos"]:
                    t["pub_qos"] = st["pub_qos"]
            bag["tf"] |= spec["tf"]
            bag["tf_static"] |= spec["tf_static"]
            if bag["skew_ms"] is None:
                bag["skew_ms"] = spec["skew_ms"]
            finalize_bag(bag)


# ---------------------------------------------------------------- findings helpers
def put(fset, f):
    """Keep one finding per key, the most severe wins."""
    cur = fset.get(f.key)
    if cur is None or bc.SEV[f.sev] > bc.SEV[cur.sev]:
        fset[f.key] = f


# ---------------------------------------------------------------- URDF / SDF
def parse_urdf(data):
    """-> {'format', 'joints': {name: {...}}, 'problems': [str]} (raises ValueError on unsafe / unparseable XML)."""
    root = safe_xml(data)
    local = lambda e: e.tag.split("}")[-1] if isinstance(e.tag, str) else ""  # noqa: E731
    rt = local(root)
    if rt not in ("robot", "sdf", "model", "world"):
        raise ValueError("not a URDF / SDF document (root <%s>)" % visible(rt, 20))
    fmt = "urdf" if rt == "robot" else "sdf"
    joints, problems = {}, []
    for j in root.iter():
        if local(j) != "joint" or not j.get("name"):
            continue
        rec = {"name": j.get("name"), "type": (j.get("type") or "").lower(), "parent": None, "child": None, "lower": None, "upper": None,
               "effort": None, "velocity": None, "has_limit": False}
        for c in j:
            ct = local(c)
            if ct in ("parent", "child"):
                rec[ct] = c.get("link") or (c.text or "").strip() or None
            elif ct == "limit" and fmt == "urdf":
                rec["has_limit"] = True
                for k in ("lower", "upper", "effort", "velocity"):
                    rec[k] = _f(c.get(k))
            elif ct == "axis" and fmt == "sdf":
                for lim in c:
                    if local(lim) == "limit":
                        rec["has_limit"] = True
                        for sub in lim:
                            if local(sub) in ("lower", "upper", "effort", "velocity"):
                                rec[local(sub)] = _f(sub.text)
        if fmt == "sdf":
            for k in ("lower", "upper"):
                if rec[k] is not None and abs(rec[k]) >= 1e15:
                    rec[k] = None
            for k in ("effort", "velocity"):
                if rec[k] is not None and rec[k] < 0:
                    rec[k] = None
        joints[rec["name"]] = rec
    if not joints:
        problems.append("no <joint> elements found")
    texts = [(k + "@" + e.get("name", "?"), v) for e in root.iter() for k, v in e.attrib.items()]
    return {"format": fmt, "joints": joints, "problems": problems, "strings": texts}


MOVABLE = ("revolute", "prismatic", "revolute2", "universal")


def urdf_findings(name, model):
    fs = {}
    for j in model["joints"].values():
        t = j["type"]
        if t in MOVABLE or (t == "continuous" and model["format"] == "urdf"):
            bad = []
            if model["format"] == "urdf" and not j["has_limit"]:
                bad.append("missing <limit>")
            if t != "continuous" and j["lower"] is not None and j["upper"] is not None and j["lower"] >= j["upper"]:
                bad.append("lower %g >= upper %g" % (j["lower"], j["upper"]))
            if model["format"] == "urdf" and j["has_limit"] and (j["effort"] is None or j["velocity"] is None):
                bad.append("effort / velocity limit missing")
            for k in ("effort", "velocity"):
                if j[k] is not None and j[k] < 0:
                    bad.append("negative %s limit" % k)
            if bad:
                put(fs, mk("MEDIUM", "URDF_JOINT_LIMIT_INVALID", "%s joint %s: %s (observed values cannot be checked against it)"
                           % (visible(name, 40), visible(j["name"], 40), "; ".join(bad)),
                           key="URDF_JOINT_LIMIT_INVALID:%s:%s" % (name, j["name"]), joint=j["name"], problems=bad))
    return list(fs.values())


def tf_graph_findings(source, edges, label):
    """Cycle / disconnected-root / conflicting-parent analysis of a parent->child frame graph."""
    out = []
    edges = {(p, c) for p, c in edges if p and c}
    if not edges:
        return out
    children, parents = {}, {}
    nodes = set()
    for p, c in edges:
        children.setdefault(p, set()).add(c)
        parents.setdefault(c, set()).add(p)
        nodes.update((p, c))
    color, cycle = {}, None
    for start in sorted(nodes):
        if color.get(start) or cycle:
            continue
        stack = [(start, iter(sorted(children.get(start, ()))))]
        path = [start]
        color[start] = 1
        while stack and not cycle:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
                path.pop()
            elif color.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt]
            elif not color.get(nxt):
                color[nxt] = 1
                path.append(nxt)
                stack.append((nxt, iter(sorted(children.get(nxt, ())))))
    if cycle:
        out.append(mk("CRITICAL", "TF_TREE_CYCLE_OR_DISCONNECT",
                      "%s: %s frame graph contains a cycle (%s); transform lookups will loop or fail"
                      % (visible(source, 60), label, " -> ".join(visible(x, 30) for x in cycle[:8])),
                      key="TF_TREE_CYCLE_OR_DISCONNECT:%s:%s:cycle" % (source, label), kind="cycle", frames=cycle[:12]))
    roots = sorted(n for n in nodes if n not in parents)
    if len(roots) > 1:
        out.append(mk("HIGH", "TF_TREE_CYCLE_OR_DISCONNECT",
                      "%s: %s frame graph has %d disconnected root frames (%s); expected a single tree"
                      % (visible(source, 60), label, len(roots), ", ".join(visible(r, 30) for r in roots[:6])),
                      key="TF_TREE_CYCLE_OR_DISCONNECT:%s:%s:roots" % (source, label), kind="roots", roots=roots[:12]))
    multi = sorted(c for c, ps in parents.items() if len(ps) > 1)
    if multi:
        out.append(mk("HIGH", "TF_TREE_CYCLE_OR_DISCONNECT",
                      "%s: %s frame(s) have more than one parent (%s); a frame can have only one parent in a tf tree"
                      % (visible(source, 60), label, ", ".join(visible(m, 30) for m in multi[:6])),
                      key="TF_TREE_CYCLE_OR_DISCONNECT:%s:%s:parents" % (source, label), kind="multi_parent", frames=multi[:12]))
    return out


# ---------------------------------------------------------------- bag findings
def topic_class(t):
    if CONTROL_RE.search(t["name"]):
        return "control"
    if SENSOR_RE.search(t["name"]) or str(t["type"]).startswith("sensor_msgs/"):
        return "sensor"
    return "other"


def bag_findings(bag):
    fs = {}
    src = bag["name"]
    for name in sorted(bag["topics"]):
        t = bag["topics"][name]
        if is_tf_static(name):
            continue        # latched once; no rate expectation
        exp = t["expected_hz"]
        rate = t["rate_hz"]
        crit_topic = t["required"] or t["safety"] or bool(SAFETY_RE.search(name))
        key = "ROSBAG_TOPIC_DROPOUT:%s:%s" % (src, name)
        if t["has_counts"]:
            if t["count"] == 0 and not t["rate_hz"] and (crit_topic or exp):
                put(fs, mk("CRITICAL", "ROSBAG_TOPIC_DROPOUT", "%s: %s %s recorded 0 messages%s" % (
                    visible(src, 40), "required/safety topic" if crit_topic else "topic", visible(name, 60),
                    " (expected %s Hz)" % fd(exp, 1) if exp else ""), key=key, topic=name, bag=src, count=0, expected_hz=exp))
            elif exp and rate is not None and exp > 0:
                ratio = rate / exp
                if ratio < 0.25:
                    put(fs, mk("CRITICAL", "ROSBAG_TOPIC_DROPOUT", "%s: topic %s ran at %.2f Hz, %.0f%% of the expected %s Hz"
                               % (visible(src, 40), visible(name, 60), rate, ratio * 100, fd(exp, 1)),
                               key=key, topic=name, bag=src, rate_hz=rate, expected_hz=exp))
                elif ratio < 0.75:
                    put(fs, mk("HIGH", "ROSBAG_TOPIC_DROPOUT", "%s: topic %s ran at %.2f Hz, %.0f%% of the expected %s Hz"
                               % (visible(src, 40), visible(name, 60), rate, ratio * 100, fd(exp, 1)),
                               key=key, topic=name, bag=src, rate_hz=rate, expected_hz=exp))
            if exp and exp > 0 and t["known_gap_s"] is not None and t["known_gap_s"] > 5.0 / exp:
                put(fs, mk("HIGH", "ROSBAG_TOPIC_DROPOUT", "%s: topic %s has a %.2f s message gap, over 5x the %.3f s period (%s Hz expected)"
                           % (visible(src, 40), visible(name, 60), t["known_gap_s"], 1.0 / exp, fd(exp, 1)),
                           key="ROSBAG_TOPIC_DROPOUT:%s:%s:gap" % (src, name), topic=name, bag=src, max_gap_s=t["known_gap_s"],
                           expected_hz=exp))
        # QoS
        cls = topic_class(t)
        probs = []
        pubs, subs = t["pub_qos"], t["sub_qos"]
        for p in pubs:
            for s in subs:
                if p["reliability"] == "BEST_EFFORT" and s["reliability"] == "RELIABLE":
                    probs.append("BEST_EFFORT publisher vs RELIABLE subscriber")
                if p["durability"] == "VOLATILE" and s["durability"] == "TRANSIENT_LOCAL":
                    probs.append("VOLATILE publisher vs TRANSIENT_LOCAL subscriber")
        rels = {p["reliability"] for p in pubs if p["reliability"] not in (None, "SYSTEM_DEFAULT", "UNKNOWN")}
        durs = {p["durability"] for p in pubs if p["durability"] not in (None, "SYSTEM_DEFAULT", "UNKNOWN")}
        if len(rels) > 1:
            probs.append("publishers disagree on reliability (%s)" % "/".join(sorted(rels)))
        if len(durs) > 1:
            probs.append("publishers disagree on durability (%s)" % "/".join(sorted(durs)))
        probs = list(dict.fromkeys(probs))
        if probs:
            put(fs, mk("HIGH" if cls in ("sensor", "control") else "MEDIUM", "QOS_INCOMPATIBLE_MISMATCH",
                       "%s: %s topic %s QoS mismatch: %s; endpoints will not connect or will silently drop data"
                       % (visible(src, 40), cls, visible(name, 60), "; ".join(probs)),
                       key="QOS_INCOMPATIBLE_MISMATCH:%s:%s:mismatch" % (src, name), topic=name, bag=src, class_=cls, problems=probs))
        mem = [q for q in pubs + subs if q["history"] == "KEEP_ALL" or (q["depth"] is not None and q["depth"] > HIGH_DEPTH)]
        if mem:
            q = mem[0]
            put(fs, mk("MEDIUM", "QOS_INCOMPATIBLE_MISMATCH", "%s: topic %s uses %s (depth %s): unbounded queue memory risk on a long run"
                       % (visible(src, 40), visible(name, 60), q["history"] or "KEEP_LAST", q["depth"] if q["depth"] is not None else "-"),
                       key="QOS_INCOMPATIBLE_MISMATCH:%s:%s:memory" % (src, name), topic=name, bag=src, history=q["history"], depth=q["depth"]))
        # clock
        if t["ts_back"] or t["stamp_back"]:
            put(fs, mk("HIGH", "ROSBAG_CLOCK_DRIFT_OR_JUMP", "%s: topic %s has %d backward timestamp jump(s) (bag time %d, header stamp %d)"
                       % (visible(src, 40), visible(name, 60), t["ts_back"] + t["stamp_back"], t["ts_back"], t["stamp_back"]),
                       key="ROSBAG_CLOCK_DRIFT_OR_JUMP:%s:%s:jump" % (src, name), topic=name, bag=src, bag_time_jumps=t["ts_back"],
                       header_stamp_jumps=t["stamp_back"]))
        if t["drift_ms"] is not None and abs(t["drift_ms"]) > 15.0:
            put(fs, mk("HIGH" if abs(t["drift_ms"]) > 50.0 else "MEDIUM", "ROSBAG_CLOCK_DRIFT_OR_JUMP",
                       "%s: topic %s header-stamp offset to recording time drifted %.1f ms over the run" % (visible(src, 40), visible(name, 60), t["drift_ms"]),
                       key="ROSBAG_CLOCK_DRIFT_OR_JUMP:%s:%s:drift" % (src, name), topic=name, bag=src, drift_ms=t["drift_ms"]))
    offs = {n: t["offset_ms"] for n, t in bag["topics"].items() if t["offset_ms"] is not None and not is_tf_topic(n)}
    skew = bag["skew_ms"]
    if len(offs) >= 2:
        lo, hi = min(offs, key=offs.get), max(offs, key=offs.get)
        s = offs[hi] - offs[lo]
        skew = max(skew, s) if skew is not None else s
        who = "%s vs %s" % (visible(lo, 40), visible(hi, 40))
    else:
        who = "summary"
    if skew is not None and skew > 15.0:
        put(fs, mk("HIGH" if skew > 50.0 else "MEDIUM", "ROSBAG_CLOCK_DRIFT_OR_JUMP",
                   "%s: inter-sensor clock skew %.1f ms (%s); sensor fusion will mis-associate samples" % (visible(src, 40), skew, who),
                   key="ROSBAG_CLOCK_DRIFT_OR_JUMP:%s:skew" % src, bag=src, skew_ms=skew))
    for f in tf_graph_findings(src, bag["tf"] | bag["tf_static"], "/tf + /tf_static"):
        put(fs, f)
    return list(fs.values())


# ---------------------------------------------------------------- bucket 1: actuators
NO_FLAG = ("", "0", "0x0", "0x00", "none", "ok", "nominal", "clear", "false", "no", "n/a", "-")
SEVERE_FLAG_RE = re.compile(r"over_?temp|over_?current|stall|encoder|short|estop|e_stop|fault|desync|thermal|brownout|under_?volt", re.I)


def parse_actuator(r):
    name, aid = _txt(r.get("joint_name")), _txt(r.get("actuator_id"))
    if not name and not aid:
        raise RowError("missing joint_name and actuator_id")
    d = {"joint_name": name or aid, "actuator_id": aid}
    for k in ("stator_temp_c", "max_temp_c", "rms_torque_nm", "peak_torque_nm", "rated_torque_nm", "limit_torque_nm", "velocity_rad_s",
              "limit_velocity_rad_s", "operating_hours", "service_interval_hrs", "backlash_arcmin", "max_backlash_arcmin",
              "harmonic_cycles", "rated_harmonic_cycles", "position_rad", "min_position_rad", "max_position_rad"):
        d[k] = num(r, k)
    d["duty_cycle_pct"] = num(r, "duty_cycle_pct", lo=0.0, hi=1000.0)
    d["error_flags"] = _txt(r.get("error_flags"))
    return d


def actuator_findings(rows, joints):
    fs = {}
    for a in rows:
        j, J = a["joint_name"], (joints or {}).get(a["joint_name"])
        key = lambda code: "%s:%s" % (code, j)  # noqa: E731
        t, tmax = a["stator_temp_c"], a["max_temp_c"]
        if t is not None:
            if (tmax is not None and t >= tmax) or t >= 100.0:
                put(fs, mk("CRITICAL", "ACTUATOR_THERMAL_OVERLOAD", "%s stator %.1f C is at/over its limit (%s C max, 100 C absolute)" % (visible(j, 40), t, fd(tmax)),
                           key=key("ACTUATOR_THERMAL_OVERLOAD"), joint=j, stator_temp_c=t, max_temp_c=tmax))
            elif (tmax is not None and t >= tmax - 10.0) or t >= 85.0:
                put(fs, mk("HIGH", "ACTUATOR_THERMAL_OVERLOAD", "%s stator %.1f C is within 10 C of its limit (%s C max) or at/over 85 C" % (visible(j, 40), t, fd(tmax)),
                           key=key("ACTUATOR_THERMAL_OVERLOAD"), joint=j, stator_temp_c=t, max_temp_c=tmax))
        pk, rms = a["peak_torque_nm"], a["rms_torque_nm"]
        lim_t = [x for x in (a["limit_torque_nm"], J["effort"] if J else None) if x is not None]
        if pk is not None and lim_t and abs(pk) > min(lim_t):
            src = "URDF/SDF effort" if J and J["effort"] is not None and J["effort"] == min(lim_t) else "limit_torque_nm"
            put(fs, mk("CRITICAL", "JOINT_TORQUE_LIMIT_EXCEEDED", "%s peak torque %.2f Nm exceeds the %.2f Nm limit (%s)" % (visible(j, 40), abs(pk), min(lim_t), src),
                       key=key("JOINT_TORQUE_LIMIT_EXCEEDED"), joint=j, peak_torque_nm=pk, limit_nm=min(lim_t), limit_source=src))
        elif rms is not None and a["rated_torque_nm"] is not None and abs(rms) > a["rated_torque_nm"]:
            put(fs, mk("HIGH", "JOINT_TORQUE_LIMIT_EXCEEDED", "%s RMS torque %.2f Nm exceeds the %.2f Nm rated torque (continuous overload)" % (visible(j, 40), abs(rms), a["rated_torque_nm"]),
                       key=key("JOINT_TORQUE_LIMIT_EXCEEDED"), joint=j, rms_torque_nm=rms, rated_torque_nm=a["rated_torque_nm"]))
        v = a["velocity_rad_s"]
        lim_v = [x for x in (a["limit_velocity_rad_s"], J["velocity"] if J else None) if x is not None]
        if v is not None and lim_v and abs(v) > min(lim_v):
            put(fs, mk("HIGH", "JOINT_VELOCITY_LIMIT_EXCEEDED", "%s velocity %.2f rad/s exceeds the %.2f rad/s limit" % (visible(j, 40), abs(v), min(lim_v)),
                       key=key("JOINT_VELOCITY_LIMIT_EXCEEDED"), joint=j, velocity_rad_s=v, limit_rad_s=min(lim_v)))
        if J and J["type"] in ("revolute", "prismatic") and J["lower"] is not None and J["upper"] is not None:
            seen = [x for x in (a["position_rad"], a["min_position_rad"], a["max_position_rad"]) if x is not None]
            if seen and (min(seen) < J["lower"] or max(seen) > J["upper"]):
                put(fs, mk("HIGH", "JOINT_POSITION_LIMIT_EXCEEDED", "%s observed position %.3f..%.3f is outside the model range [%.3f, %.3f]"
                           % (visible(j, 40), min(seen), max(seen), J["lower"], J["upper"]),
                           key=key("JOINT_POSITION_LIMIT_EXCEEDED"), joint=j, observed_min=min(seen), observed_max=max(seen),
                           lower=J["lower"], upper=J["upper"]))
        duty = a["duty_cycle_pct"]
        if duty is not None and duty > 70.0:
            put(fs, mk("HIGH" if duty > 85.0 else "MEDIUM", "ACTUATOR_HIGH_DUTY_CYCLE", "%s duty cycle %.1f%% (limits: >70%% MEDIUM, >85%% HIGH)" % (visible(j, 40), duty),
                       key=key("ACTUATOR_HIGH_DUTY_CYCLE"), joint=j, duty_cycle_pct=duty))
        bl, blmax, hrs, iv = a["backlash_arcmin"], a["max_backlash_arcmin"], a["operating_hours"], a["service_interval_hrs"]
        hc, hcr = a["harmonic_cycles"], a["rated_harmonic_cycles"]
        wear = []
        if bl is not None and blmax is not None and bl > blmax:
            wear.append(("HIGH", "backlash %.2f arcmin exceeds the %.2f arcmin limit" % (bl, blmax)))
        if hrs is not None and iv and hrs >= iv:
            wear.append(("MEDIUM", "%.0f operating hours reached the %.0f h service interval" % (hrs, iv)))
        elif hrs is not None and iv and hrs >= 0.9 * iv:
            wear.append(("LOW", "%.0f operating hours is %.0f%% of the %.0f h service interval" % (hrs, 100.0 * hrs / iv, iv)))
        if hc is not None and hcr and hc >= hcr:
            wear.append(("MEDIUM", "%.3g flexspline cycles reached the %.3g rated cycles" % (hc, hcr)))
        elif hc is not None and hcr and hc >= 0.9 * hcr:
            wear.append(("LOW", "%.3g flexspline cycles is %.0f%% of the %.3g rated cycles" % (hc, 100.0 * hc / hcr, hcr)))
        if wear:
            wear.sort(key=lambda w: -bc.SEV[w[0]])
            put(fs, mk(wear[0][0], "HARMONIC_DRIVE_WEAR_THRESHOLD", "%s harmonic drive: %s" % (visible(j, 40), "; ".join(w[1] for w in wear)),
                       key=key("HARMONIC_DRIVE_WEAR_THRESHOLD"), joint=j, backlash_arcmin=bl, operating_hours=hrs, service_interval_hrs=iv,
                       harmonic_cycles=hc))
        fl = a["error_flags"]
        if fl.lower() not in NO_FLAG:
            put(fs, mk("HIGH" if SEVERE_FLAG_RE.search(fl) else "MEDIUM", "ACTUATOR_ERROR_FLAGS_SET", "%s reports error flags %s" % (visible(j, 40), visible(fl, 60)),
                       key=key("ACTUATOR_ERROR_FLAGS_SET"), joint=j, error_flags=visible(fl, 60)))
    if joints and rows:
        missing = sorted({a["joint_name"] for a in rows if a["joint_name"] not in joints})
        if missing:
            put(fs, mk("LOW", "ACTUATOR_JOINT_NOT_IN_MODEL", "%d actuator joint(s) not found in the URDF/SDF, so model limits were not applied: %s"
                       % (len(missing), ", ".join(visible(m, 30) for m in missing[:6])), key="ACTUATOR_JOINT_NOT_IN_MODEL", joints=missing[:12]))
    return list(fs.values())


# ---------------------------------------------------------------- bucket 2: BMS
def norm_chem(s):
    t = re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    if "lifepo" in t or t in ("lfp", "lifepo4") or "ironphosphate" in t:
        return "LFP"
    if t in ("lipo", "lipoly", "lithiumpolymer", "liion", "nmc", "lithiumion", "lco", "nca") or t.startswith("lipo"):
        return "LIPO"
    return None


def parse_cells(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return []
    if isinstance(v, list):
        return [fnum(x) for x in v]
    s = str(v).strip().strip("[]")
    return [fnum(x) for x in re.split(r"[;|,\s]+", s) if x]


def parse_bms(r):
    pid = _txt(r.get("pack_id"))
    if not pid:
        raise RowError("missing pack_id")
    chem = norm_chem(r.get("chemistry"))
    if chem is None:
        raise RowError("pack %s: unknown chemistry %r (expected LiFePO4 / LFP or LiPo / Li-ion)" % (visible(pid, 30), visible(r.get("chemistry"), 20)))
    try:
        cells = parse_cells(r.get("cell_voltages_mv"))
    except (ValueError, TypeError):
        raise RowError("pack %s: cell_voltages_mv is not a list of numbers" % visible(pid, 30))
    cmin, cmax = num(r, "cell_min_mv", lo=0.0, hi=10000.0), num(r, "cell_max_mv", lo=0.0, hi=10000.0)
    if cells:
        cmin = min(cells) if cmin is None else cmin
        cmax = max(cells) if cmax is None else cmax
    if cmin is None or cmax is None:
        raise RowError("pack %s: needs cell_min_mv/cell_max_mv or cell_voltages_mv" % visible(pid, 30))
    cap, cur = num(r, "capacity_ah", lo=0.0, hi=1e5), num(r, "current_a", lo=-1e5, hi=1e5)
    ls = _txt(r.get("load_state")).lower()
    if not ls:
        ls = "load" if cur is not None and abs(cur) >= max(1.0, 0.02 * (cap or 0.0)) else "rest"
    loaded = ls not in ("rest", "idle", "resting", "standby", "open_circuit", "ocv", "off", "sleep")
    return {"pack_id": pid, "chemistry": chem, "series_cells": num(r, "series_cells", lo=1.0, hi=500.0), "pack_voltage_v": num(r, "pack_voltage_v", lo=0.0, hi=2000.0),
            "current_a": cur, "capacity_ah": cap, "max_c_rate": num(r, "max_c_rate", lo=0.0, hi=1000.0), "cell_min_mv": cmin, "cell_max_mv": cmax,
            "cells": cells, "load_state": ls, "loaded": loaded, "mosfet_temp_c": num(r, "mosfet_temp_c"), "pack_temp_c": num(r, "pack_temp_c"),
            "soh_pct": num(r, "soh_pct", lo=0.0, hi=120.0), "cycle_count": num(r, "cycle_count", lo=0.0, hi=1e7),
            "rated_cycles": num(r, "rated_cycles", lo=1.0, hi=1e7), "balance_active": bool(tristate(r.get("balance_active"))),
            "delta_mv": cmax - cmin}


DEFAULT_RATED_CYCLES = {"LFP": 3000.0, "LIPO": 500.0}


def bms_findings(rows):
    fs = {}
    for b in rows:
        p, chem = b["pack_id"], b["chemistry"]
        label = "%s (%s)" % (visible(p, 40), "LiFePO4" if chem == "LFP" else "LiPo")
        crit, warn = (2500.0, 2800.0) if chem == "LFP" else (3200.0, 3500.0)
        cm = b["cell_min_mv"]
        if cm < crit:
            put(fs, mk("CRITICAL", "BMS_CRITICAL_CELL_SAG", "%s weakest cell %.0f mV is below the %.0f mV critical floor" % (label, cm, crit),
                       key="BMS_CRITICAL_CELL_SAG:" + p, pack_id=p, cell_min_mv=cm, floor_mv=crit))
        elif cm < warn and b["loaded"]:
            put(fs, mk("HIGH", "BMS_CRITICAL_CELL_SAG", "%s weakest cell %.0f mV sags below %.0f mV under load" % (label, cm, warn),
                       key="BMS_CRITICAL_CELL_SAG:" + p, pack_id=p, cell_min_mv=cm, floor_mv=warn))
        d = b["delta_mv"]
        sev = None
        if d >= 150.0:
            sev = "CRITICAL"
        elif (b["loaded"] and d >= 80.0) or (not b["loaded"] and d >= 40.0):
            sev = "HIGH"
        elif not b["loaded"] and d >= 25.0:
            sev = "MEDIUM"
        if sev:
            put(fs, mk(sev, "BMS_CELL_IMBALANCE_DELTA", "%s cell spread %.0f mV (%.0f..%.0f mV) %s%s" % (
                label, d, b["cell_min_mv"], b["cell_max_mv"], "under load" if b["loaded"] else "at rest",
                "; balancer active" if b["balance_active"] else "; balancer idle"),
                       key="BMS_CELL_IMBALANCE_DELTA:" + p, pack_id=p, delta_mv=d, loaded=b["loaded"], balance_active=b["balance_active"]))
        mt, pt = b["mosfet_temp_c"], b["pack_temp_c"]
        if (mt is not None and mt >= 90.0) or (pt is not None and pt >= 60.0):
            put(fs, mk("CRITICAL", "BMS_MOSFET_THERMAL_RISE", "%s MOSFET %s C / pack %s C is at the critical limit (90 C / 60 C)" % (label, fd(mt), fd(pt)),
                       key="BMS_MOSFET_THERMAL_RISE:" + p, pack_id=p, mosfet_temp_c=mt, pack_temp_c=pt))
        elif (mt is not None and mt >= 75.0) or (pt is not None and pt >= 50.0):
            put(fs, mk("HIGH", "BMS_MOSFET_THERMAL_RISE", "%s MOSFET %s C / pack %s C is above the warning limit (75 C / 50 C)" % (label, fd(mt), fd(pt)),
                       key="BMS_MOSFET_THERMAL_RISE:" + p, pack_id=p, mosfet_temp_c=mt, pack_temp_c=pt))
        if b["capacity_ah"] and b["current_a"] is not None and b["max_c_rate"]:
            c = abs(b["current_a"]) / b["capacity_ah"]
            if c > b["max_c_rate"]:
                put(fs, mk("HIGH", "BMS_DISCHARGE_CRATE_EXCEEDED", "%s drawing %.2f C (%.1f A / %.1f Ah) over the %.2f C limit" % (label, c, abs(b["current_a"]), b["capacity_ah"], b["max_c_rate"]),
                           key="BMS_DISCHARGE_CRATE_EXCEEDED:" + p, pack_id=p, c_rate=c, max_c_rate=b["max_c_rate"]))
            elif c > 0.85 * b["max_c_rate"]:
                put(fs, mk("MEDIUM", "BMS_DISCHARGE_CRATE_EXCEEDED", "%s drawing %.2f C, over 85%% of the %.2f C limit" % (label, c, b["max_c_rate"]),
                           key="BMS_DISCHARGE_CRATE_EXCEEDED:" + p, pack_id=p, c_rate=c, max_c_rate=b["max_c_rate"]))
        soh, cyc = b["soh_pct"], b["cycle_count"]
        rated = b["rated_cycles"] or DEFAULT_RATED_CYCLES[chem]
        if soh is not None and soh < 70.0:
            put(fs, mk("HIGH", "BMS_SOH_DEGRADATION", "%s state of health %.1f%% is below 70%%" % (label, soh),
                       key="BMS_SOH_DEGRADATION:" + p, pack_id=p, soh_pct=soh, cycle_count=cyc))
        elif (soh is not None and soh < 80.0) or (cyc is not None and cyc > rated):
            put(fs, mk("MEDIUM", "BMS_SOH_DEGRADATION", "%s state of health %s%%, %s cycles (rated %.0f)" % (label, fd(soh), fd(cyc, 0), rated),
                       key="BMS_SOH_DEGRADATION:" + p, pack_id=p, soh_pct=soh, cycle_count=cyc, rated_cycles=rated))
        if b["cells"] and b["series_cells"] and len(b["cells"]) != int(b["series_cells"]):
            put(fs, mk("MEDIUM", "BMS_CELL_COUNT_MISMATCH", "%s reports %d cell voltages but series_cells is %d (sensing wire or BMS channel fault?)" % (label, len(b["cells"]), b["series_cells"]),
                       key="BMS_CELL_COUNT_MISMATCH:" + p, pack_id=p, cells=len(b["cells"]), series_cells=b["series_cells"]))
    return list(fs.values())


# ---------------------------------------------------------------- CAN / RS485
CAN_ERR_RE = re.compile(r"crc|stuff|form|bit|ack|parity|framing|overrun|error|err", re.I)
CAN_EMCY_KIND_RE = re.compile(r"emcy|emergency|estop|e_stop|fault", re.I)
CAN_HB_TIMEOUT_RE = re.compile(r"heartbeat[_ ]?timeout|hb[_ ]?timeout|node[_ ]?guard", re.I)


def parse_hex_id(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip().lower()
    try:
        return int(s, 16)       # "0x081", "081", "81" -> hex (candump convention); JSON numbers stay decimal
    except ValueError:
        raise RowError("cob_id %r is not hexadecimal" % visible(v, 20))


def parse_data(v):
    if v is None:
        return b""
    s = re.sub(r"[^0-9a-fA-F]", "", str(v))
    if len(s) % 2:
        s = s[:-1]
    return bytes.fromhex(s[:16])


def can_frame_from_row(r):
    fid = r.get("cob_id", r.get("can_id", r.get("arbitration_id", r.get("frame_id", r.get("id")))))
    kind = _txt(r.get("kind", r.get("type", r.get("event")))).lower()
    err = _txt(r.get("error", r.get("error_type", r.get("err", r.get("error_class")))))
    ts = num(r, "timestamp", None)
    if ts is None:
        for k in ("time_s", "time", "ts", "t"):
            ts = num(r, k, None)
            if ts is not None:
                break
    data = parse_data(r.get("data", r.get("payload")))
    dlc = num(r, "dlc", None, lo=0.0, hi=64.0)
    is_err = bool(err and err.lower() not in NO_FLAG) or bool(re.search(r"error|crc|stuff|form|bus_?off|ack", kind))
    ecls = (err or kind).lower()
    return {"ts": ts, "iface": _txt(r.get("bus", r.get("interface", r.get("channel")))) or "can0", "id": parse_hex_id(fid), "data": data,
            "dlc": int(dlc) if dlc is not None else len(data), "err": is_err, "err_class": ecls, "kind": kind,
            "load": num(r, "bus_load_pct", num(r, "bus_load", None, lo=0.0, hi=1000.0), lo=0.0, hi=1000.0), "bitrate": num(r, "bitrate", None, lo=1.0, hi=1e9),
            "node": _txt(r.get("node_id", r.get("node"))), "hb_ok": tristate(r.get("heartbeat_ok", r.get("heartbeat")))}


CANDUMP_RE = re.compile(r"^\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s+(\S+)\s+([0-9A-Fa-f]{3,8})#([0-9A-Fa-f]*)(?:\s+\S+)?\s*$")
CANDUMP_PLAIN_RE = re.compile(r"^\s*(\S+)\s+([0-9A-Fa-f]{3,8})\s+\[(\d+)\]\s*((?:[0-9A-Fa-f]{2}\s*)*)$")


def parse_candump(text):
    """candump -L (`(ts) can0 123#DEADBEEF`) and plain candump (`can0  123  [8]  DE AD ...`) -> (frames, skipped_lines)."""
    frames, skipped = [], 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = CANDUMP_RE.match(line)
        if m:
            ts, iface, cid, data = float(m.group(1)), m.group(2), int(m.group(3), 16), m.group(4)
        else:
            m = CANDUMP_PLAIN_RE.match(line)
            if not m:
                skipped += 1
                continue
            ts, iface, cid, data = None, m.group(1), int(m.group(2), 16), m.group(4)
        raw = bytes.fromhex(data[:16] if len(data) % 2 == 0 else data[:15])
        frames.append({"ts": ts, "iface": iface, "id": cid, "data": raw, "dlc": len(raw), "err": False, "err_class": "", "kind": "",
                       "load": None, "bitrate": None, "node": "", "hb_ok": None})
        if cid & 0x20000000:                       # SocketCAN error frame: the "id" carries the error class bits
            f = frames[-1]
            cls = cid & 0x1FFFFFFF
            f["err"] = True
            tags = []
            if cls & 0x40:
                tags.append("bus_off")
            if cls & 0x08 and len(raw) > 3:
                if raw[2] & 0x04:
                    tags.append("stuff")
                if raw[2] & 0x02:
                    tags.append("form")
                if raw[3] in (0x08, 0x18):
                    tags.append("crc")
            if cls & 0x20:
                tags.append("ack")
            f["err_class"] = ",".join(tags) or "error"
            f["id"] = None
    return frames, skipped


def analyze_can(frames):
    """-> (findings, per-bus summary rows)."""
    fs, summary = {}, []
    buses = {}
    for f in frames:
        buses.setdefault(f["iface"], []).append(f)
    for bus in sorted(buses):
        fr = buses[bus]
        total = len(fr)
        errs = [f for f in fr if f["err"]]
        classes = {"crc": 0, "stuff": 0, "form": 0}
        busoff = False
        for f in errs:
            for c in classes:
                if c in f["err_class"]:
                    classes[c] += 1
            busoff = busoff or "bus_off" in f["err_class"] or "busoff" in f["err_class"].replace("-", "")
        ts = [f["ts"] for f in fr if f["ts"] is not None]
        t0, t1 = (min(ts), max(ts)) if ts else (None, None)
        # bus load: explicit column wins, else estimated from frame sizes per 1 s window
        loads = [f["load"] for f in fr if f["load"] is not None]
        peak, est = (max(loads), False) if loads else (None, True)
        if peak is None and ts:
            bits = {}
            for f in fr:
                if f["ts"] is not None and not f["err"]:
                    bits[int(f["ts"])] = bits.get(int(f["ts"]), 0) + 47 + 8 * min(f["dlc"], 8)
            rate = next((f["bitrate"] for f in fr if f["bitrate"]), CAN_BITRATE)
            peak = max(bits.values()) * 100.0 / rate if bits else None
        emcy, hb_events = [], {}
        for f in fr:
            cid = f["id"]
            if cid is not None and 0x081 <= cid <= 0x0FF and (not f["data"] or any(f["data"][:2])):
                emcy.append(cid)
            elif CAN_EMCY_KIND_RE.search(f["kind"]) and not f["err"]:
                emcy.append(cid if cid is not None else -1)
            if f["hb_ok"] is False or CAN_HB_TIMEOUT_RE.search(f["kind"]):
                hb_events.setdefault(f["node"] or ("0x%X" % (cid & 0x7F) if cid is not None else "?"), "reported")
            if cid is not None and 0x701 <= cid <= 0x77F and f["ts"] is not None:
                hb_events.setdefault("_seen", {}).setdefault(cid - 0x700, []).append(f["ts"])
        seen = hb_events.pop("_seen", {})
        for node, tl in seen.items():
            tl = sorted(tl)
            gaps = [b - a for a, b in zip(tl, tl[1:])]
            if t1 is not None:
                gaps.append(t1 - tl[-1])
            worst = max(gaps) if gaps else 0.0
            if worst > HEARTBEAT_TIMEOUT_S:
                hb_events["node 0x%02X" % node] = "%.2f s silent" % worst
        ratio = len(errs) / total if total else 0.0
        summary.append({"bus": bus, "frames": total, "error_frames": len(errs), "error_pct": ratio * 100.0, "crc": classes["crc"],
                        "stuff": classes["stuff"], "form": classes["form"], "peak_load_pct": peak, "load_estimated": est and peak is not None,
                        "emergency_frames": len(emcy), "heartbeat_timeouts": len(hb_events)})
        k = lambda s: "CAN_BUS_FAULT_OR_TIMEOUT:%s:%s" % (bus, s)  # noqa: E731
        if emcy:
            ids = sorted({"0x%03X" % c for c in emcy if c >= 0}) or ["fault event"]
            put(fs, mk("CRITICAL", "CAN_BUS_FAULT_OR_TIMEOUT", "bus %s: %d emergency / fault frame(s) (cob_id %s)" % (visible(bus, 20), len(emcy), ", ".join(ids[:6])),
                       key=k("emergency"), bus=bus, cob_ids=ids[:12], count=len(emcy)))
        if busoff:
            put(fs, mk("CRITICAL", "CAN_BUS_FAULT_OR_TIMEOUT", "bus %s entered bus-off (controller disconnected itself after repeated errors)" % visible(bus, 20),
                       key=k("busoff"), bus=bus))
        if errs and ratio >= 0.01:
            put(fs, mk("CRITICAL" if ratio >= 0.05 else "HIGH", "CAN_BUS_FAULT_OR_TIMEOUT",
                       "bus %s: %d of %d frames are error frames (%.1f%%; CRC %d, stuff %d, form %d)" % (visible(bus, 20), len(errs), total, ratio * 100, classes["crc"], classes["stuff"], classes["form"]),
                       key=k("errors"), bus=bus, error_pct=ratio * 100, crc=classes["crc"], stuff=classes["stuff"], form=classes["form"]))
        for node, why in sorted(hb_events.items()):
            put(fs, mk("HIGH", "CAN_BUS_FAULT_OR_TIMEOUT", "bus %s: heartbeat timeout on %s (%s)" % (visible(bus, 20), visible(node, 20), why),
                       key=k("heartbeat:" + node), bus=bus, node=node))
        if peak is not None and peak > 80.0:
            put(fs, mk("MEDIUM", "CAN_BUS_FAULT_OR_TIMEOUT", "bus %s peak load %.0f%%%s exceeds 80%%; arbitration latency and frame loss risk" % (visible(bus, 20), peak, " (estimated)" if est else ""),
                       key=k("load"), bus=bus, peak_load_pct=peak))
    return list(fs.values()), summary


# ---------------------------------------------------------------- bucket 4: flight logs
FLIGHT_NUM = ("ekf_vel_innov", "ekf_pos_innov", "ekf_hgt_innov", "ekf_mag_innov", "imu_vib_x", "imu_vib_y", "imu_vib_z", "imu_clip_count",
              "gnss_fix_type", "gnss_satellites", "gnss_hdop", "esc_rpm", "esc_temp_c", "esc_current_a", "esc_desync_count")
INDOOR_ENV = ("indoor", "bench", "lab", "sim", "simulation", "hil", "sitl", "hangar")


def parse_flight_row(r):
    d = {k: num(r, k, lo=-1e9, hi=1e9) for k in FLIGHT_NUM}
    d["esc_id"] = _txt(r.get("esc_id"))
    d["failsafe_active"] = bool(tristate(r.get("failsafe_active")))
    pe = _txt(r.get("prearm_error"))
    d["prearm_error"] = "" if pe.lower() in NO_FLAG else pe
    env = _txt(r.get("environment", r.get("location"))).lower()
    ind = tristate(r.get("indoor"))
    d["indoor"] = bool(ind) if ind is not None else env in INDOOR_ENV
    air = tristate(r.get("in_air", r.get("armed")))
    d["on_ground"] = air is False
    return d


def blank_flight_row():
    d = {k: None for k in FLIGHT_NUM}
    d.update({"esc_id": "", "failsafe_active": False, "prearm_error": "", "indoor": False, "on_ground": False})
    return d


def flight_summary(rows):
    s = {"records": len(rows)}
    for ch, col in (("vel", "ekf_vel_innov"), ("pos", "ekf_pos_innov"), ("hgt", "ekf_hgt_innov"), ("mag", "ekf_mag_innov")):
        vals = [r[col] for r in rows if r[col] is not None]
        s["ekf_" + ch] = max(vals) if vals else None
    for ax in "xyz":
        vals = [abs(r["imu_vib_" + ax]) for r in rows if r["imu_vib_" + ax] is not None]
        s["vib_" + ax] = max(vals) if vals else None
    clips = [r["imu_clip_count"] for r in rows if r["imu_clip_count"] is not None]
    s["clip"] = max(clips) if clips else None
    g = [r for r in rows if not r["indoor"] and not r["on_ground"]]
    for k, col, fn in (("fix_min", "gnss_fix_type", min), ("sats_min", "gnss_satellites", min), ("hdop_max", "gnss_hdop", max)):
        vals = [r[col] for r in g if r[col] is not None]
        s[k] = fn(vals) if vals else None
    esc = {}
    for r in rows:
        if r["esc_id"] == "" and all(r[c] is None for c in ("esc_rpm", "esc_temp_c", "esc_desync_count", "esc_current_a")):
            continue
        e = esc.setdefault(r["esc_id"] or "0", {"rpm": [], "temp": None, "desync": None, "current": None})
        if r["esc_rpm"] is not None and r["esc_rpm"] > 0:
            e["rpm"].append(r["esc_rpm"])
        if r["esc_temp_c"] is not None:
            e["temp"] = r["esc_temp_c"] if e["temp"] is None else max(e["temp"], r["esc_temp_c"])
        if r["esc_desync_count"] is not None:
            e["desync"] = r["esc_desync_count"] if e["desync"] is None else max(e["desync"], r["esc_desync_count"])
        if r["esc_current_a"] is not None:
            e["current"] = r["esc_current_a"] if e["current"] is None else max(e["current"], r["esc_current_a"])
    s["esc"] = {k: {"mean_rpm": (sum(v["rpm"]) / len(v["rpm"])) if v["rpm"] else None, "temp": v["temp"], "desync": v["desync"], "current": v["current"]}
                for k, v in sorted(esc.items())}
    s["failsafe"] = any(r["failsafe_active"] for r in rows)
    s["prearm"] = next((r["prearm_error"] for r in rows if r["prearm_error"]), "")
    return s


def flight_findings(s, name="flight"):
    fs = {}
    if not s["records"]:
        return []
    clip = s["clip"]
    if clip is not None and clip > 0:
        put(fs, mk("CRITICAL", "IMU_VIBRATION_CLIPPING", "accelerometer clipping: %.0f clipped sample(s) logged (sensor saturation corrupts the EKF)" % clip,
                   key="IMU_VIBRATION_CLIPPING:clipping", imu_clip_count=clip))
    vm = max((s["vib_" + a] for a in "xyz" if s["vib_" + a] is not None), default=None)
    if vm is not None and vm > 30.0:
        put(fs, mk("CRITICAL" if vm > 60.0 else "HIGH", "IMU_VIBRATION_CLIPPING", "IMU vibration RMS peaks at %.1f m/s^2 (x %s, y %s, z %s; >30 HIGH, >60 CRITICAL)"
                   % (vm, fd(s["vib_x"]), fd(s["vib_y"]), fd(s["vib_z"])), key="IMU_VIBRATION_CLIPPING:vibration", vib_max=vm))
    for ch, label in (("vel", "velocity"), ("pos", "position"), ("hgt", "height"), ("mag", "magnetometer")):
        v = s["ekf_" + ch]
        if v is not None and v >= 0.7:
            put(fs, mk("CRITICAL" if v >= 1.0 else "HIGH", "EKF_INNOVATION_DIVERGENCE", "EKF %s innovation test ratio peaks at %.2f (>=0.7 HIGH, >=1.0 rejects the measurement)" % (label, v),
                       key="EKF_INNOVATION_DIVERGENCE:" + ch, channel=ch, test_ratio=v))
    for eid, e in s["esc"].items():
        desync, temp = e["desync"], e["temp"]
        if (desync is not None and desync > 0) or (temp is not None and temp >= 105.0):
            put(fs, mk("CRITICAL", "ESC_DESYNC_OR_OVERHEAT", "ESC %s: %s desync event(s), peak temperature %s C" % (visible(eid, 10), fd(desync, 0), fd(temp)),
                       key="ESC_DESYNC_OR_OVERHEAT:esc" + eid, esc_id=eid, desync_count=desync, esc_temp_c=temp))
        elif temp is not None and temp >= 85.0:
            put(fs, mk("HIGH", "ESC_DESYNC_OR_OVERHEAT", "ESC %s peak temperature %.1f C (>=85 C)" % (visible(eid, 10), temp),
                       key="ESC_DESYNC_OR_OVERHEAT:esc" + eid, esc_id=eid, esc_temp_c=temp))
    means = {k: e["mean_rpm"] for k, e in s["esc"].items() if e["mean_rpm"]}
    if len(means) >= 2:
        hi, lo = max(means.values()), min(means.values())
        spread = (hi - lo) / hi * 100.0
        if spread > 15.0:
            put(fs, mk("HIGH", "ESC_DESYNC_OR_OVERHEAT", "ESC mean RPM spread %.1f%% (%.0f..%.0f) exceeds 15%%: a motor/prop/ESC is working harder or slipping" % (spread, lo, hi),
                       key="ESC_DESYNC_OR_OVERHEAT:rpm_spread", spread_pct=spread, rpm_min=lo, rpm_max=hi))
    fix, sats, hdop = s["fix_min"], s["sats_min"], s["hdop_max"]
    if (fix is not None and fix < 3) or (sats is not None and sats < 6) or (hdop is not None and hdop > 2.5):
        put(fs, mk("HIGH", "GNSS_DEGRADED_FIX", "outdoor GNSS degraded: min fix type %s, min satellites %s, max HDOP %s (fix<3, sats<6 or HDOP>2.5)" % (fd(fix, 0), fd(sats, 0), fd(hdop, 2)),
                   key="GNSS_DEGRADED_FIX", fix_min=fix, sats_min=sats, hdop_max=hdop))
    elif (sats is not None and sats < 8) or (hdop is not None and hdop > 1.8):
        put(fs, mk("MEDIUM", "GNSS_DEGRADED_FIX", "outdoor GNSS marginal: min satellites %s, max HDOP %s (sats<8 or HDOP>1.8)" % (fd(sats, 0), fd(hdop, 2)),
                   key="GNSS_DEGRADED_FIX", fix_min=fix, sats_min=sats, hdop_max=hdop))
    if s["failsafe"]:
        put(fs, mk("CRITICAL", "FLIGHT_FAILSAFE_OR_PREARM_FAULT", "a failsafe was active during the log", key="FLIGHT_FAILSAFE_OR_PREARM_FAULT:failsafe"))
    if s["prearm"]:
        put(fs, mk("HIGH", "FLIGHT_FAILSAFE_OR_PREARM_FAULT", "pre-arm check failure logged: %s" % visible(s["prearm"], 80), key="FLIGHT_FAILSAFE_OR_PREARM_FAULT:prearm",
                   prearm_error=visible(s["prearm"], 80)))
    return list(fs.values())


# ---- PX4 ULog
ULOG_MAGIC = b"ULog\x01\x12\x35"
_ULOG_TYPES = {"int8_t": "b", "uint8_t": "B", "int16_t": "h", "uint16_t": "H", "int32_t": "i", "uint32_t": "I", "int64_t": "q", "uint64_t": "Q",
               "float": "f", "double": "d", "bool": "?", "char": "c"}


class ULogLayout(object):
    def __init__(self, formats):
        self.formats, self.cache = formats, {}

    def flat(self, name, depth=0):
        if name in self.cache:
            return self.cache[name]
        if depth > 6 or name not in self.formats:
            raise ValueError("unknown ULog type")
        out, off = {}, 0
        for ftype, count, fname in self.formats[name]:
            n = count or 1
            if ftype in _ULOG_TYPES:
                size = struct.calcsize("<" + _ULOG_TYPES[ftype])
                if not fname.startswith("_padding"):
                    for i in range(n):
                        out[fname + ("[%d]" % i if count else "")] = (_ULOG_TYPES[ftype], off + i * size)
                off += size * n
            else:
                sub, subsize = self.flat(ftype, depth + 1)
                for i in range(n):
                    for k, v in sub.items():
                        out["%s%s.%s" % (fname, "[%d]" % i if count else "", k)] = (v[0], off + i * subsize + v[1])
                off += subsize * n
        self.cache[name] = (out, off)
        return out, off


def parse_ulog(name, data):
    if data[:7] != ULOG_MAGIC:
        raise ValueError("not a ULog file (bad magic)")
    problems, meta, rows = [], [], []
    formats, subs, pos = {}, {}, 16
    last_dropout = 0
    texts = {"prearm": "", "failsafe": False}
    layout = None
    want = {"estimator_status", "vehicle_imu_status", "vehicle_gps_position", "sensor_gps", "esc_status", "vehicle_status"}
    records = 0
    while pos + 3 <= len(data):
        size, mtype = struct.unpack_from("<HB", data, pos)
        body = data[pos + 3:pos + 3 + size]
        if len(body) < size:
            problems.append("ULog truncated mid-message")
            break
        pos += 3 + size
        t = chr(mtype)
        try:
            if t == "F":
                nm, _, fields = body.decode("utf-8", "replace").partition(":")
                lst = []
                for f in [x for x in fields.split(";") if x.strip()]:
                    ftype, _, fname = f.strip().partition(" ")
                    m = re.fullmatch(r"([A-Za-z0-9_]+)\[(\d+)\]", ftype)
                    lst.append((m.group(1), int(m.group(2)), fname) if m else (ftype, None, fname))
                formats[nm] = lst
            elif t in ("I", "M", "P", "Q"):
                off = 1 if t == "M" else 0
                klen = body[off]
                key = body[off + 1:off + 1 + klen].decode("utf-8", "replace")
                val = body[off + 1 + klen:]
                kname = key.split(" ")[-1]
                if t in ("I", "M") and key.startswith("char["):
                    meta.append(("info:" + kname, val.decode("utf-8", "replace").rstrip("\x00")[:500]))
            elif t == "A":
                mid = struct.unpack_from("<H", body, 1)[0]
                subs[mid] = body[3:].decode("utf-8", "replace")
            elif t in ("L", "C"):
                off = 1 if t == "L" else 3
                msg = body[off + 8:].decode("utf-8", "replace")
                meta.append(("log:%d" % len(meta), msg[:300]))
                if re.search(r"preflight fail|prearm|arming denied", msg, re.I) and not texts["prearm"]:
                    texts["prearm"] = msg
                if re.search(r"failsafe (?:activated|enabled|triggered)|failsafe:", msg, re.I):
                    texts["failsafe"] = True
            elif t == "O":
                last_dropout += 1
            elif t == "D" and records < MAX_LOG_RECORDS:
                mid = struct.unpack_from("<H", body, 0)[0]
                nm = subs.get(mid)
                if nm not in want:
                    continue
                if layout is None:
                    layout = ULogLayout(formats)
                flat, total = layout.flat(nm)
                payload = body[2:]
                if len(payload) < total:
                    continue
                val = lambda k: struct.unpack_from("<" + flat[k][0], payload, flat[k][1])[0] if k in flat else None  # noqa: E731
                r = blank_flight_row()
                records += 1
                if nm == "estimator_status":
                    for col, k in (("ekf_vel_innov", "vel_test_ratio"), ("ekf_pos_innov", "pos_test_ratio"), ("ekf_hgt_innov", "hgt_test_ratio"),
                                   ("ekf_mag_innov", "mag_test_ratio")):
                        r[col] = _f(val(k))
                    for i, ax in enumerate("xyz"):
                        r["imu_vib_" + ax] = _f(val("vibe[%d]" % i))
                elif nm == "vehicle_imu_status":
                    r["imu_vib_x"] = _f(val("accel_vibration_metric"))
                    clips = [val("accel_clipping[%d]" % i) for i in range(3)]
                    if any(c is not None for c in clips):
                        r["imu_clip_count"] = float(sum(c or 0 for c in clips))
                elif nm in ("vehicle_gps_position", "sensor_gps"):
                    r["gnss_fix_type"], r["gnss_satellites"], r["gnss_hdop"] = _f(val("fix_type")), _f(val("satellites_used")), _f(val("hdop"))
                elif nm == "vehicle_status":
                    r["failsafe_active"] = bool(val("failsafe"))
                elif nm == "esc_status":
                    cnt = int(val("esc_count") or 0)
                    for i in range(min(cnt, 16)):
                        e = blank_flight_row()
                        e["esc_id"] = str(i)
                        e["esc_rpm"], e["esc_current_a"] = _f(val("esc[%d].esc_rpm" % i)), _f(val("esc[%d].esc_current" % i))
                        e["esc_temp_c"], e["esc_desync_count"] = _f(val("esc[%d].esc_temperature" % i)), _f(val("esc[%d].esc_errorcount" % i))
                        rows.append(e)
                    continue
                rows.append(r)
        except (struct.error, ValueError, IndexError, KeyError):
            problems.append("undecodable ULog message type %r" % t)
    if texts["failsafe"]:
        r = blank_flight_row()
        r["failsafe_active"] = True
        rows.append(r)
    if texts["prearm"]:
        r = blank_flight_row()
        r["prearm_error"] = texts["prearm"]
        rows.append(r)
    if records >= MAX_LOG_RECORDS:
        problems.append("ULog record limit %d reached; later samples were not analysed" % MAX_LOG_RECORDS)
    if not formats:
        problems.append("ULog has no format definitions")
    return {"name": name, "kind": "ulog", "rows": rows, "meta": meta, "problems": list(dict.fromkeys(problems)),
            "topics": sorted(set(subs.values())), "dropouts": last_dropout}


# ---- ArduPilot DataFlash .bin
_DF_SIZES = {"a": 64, "b": 1, "B": 1, "h": 2, "H": 2, "i": 4, "I": 4, "f": 4, "d": 8, "n": 4, "N": 16, "Z": 64, "c": 2, "C": 2, "e": 4, "E": 4,
             "L": 4, "M": 1, "q": 8, "Q": 8}
_DF_FMT = {"b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I", "f": "f", "d": "d", "c": "h", "C": "H", "e": "i", "E": "I", "L": "i", "M": "B",
           "q": "q", "Q": "Q"}
FAILSAFE_SUBSYS = {5, 6, 7, 8, 9, 17, 20, 23, 26, 27, 29, 31}


def parse_ardupilot_bin(name, data):
    if data[:2] != b"\xa3\x95":
        raise ValueError("not an ArduPilot DataFlash log (bad header)")
    fmts = {128: ("FMT", 89, "BBnNZ", ["Type", "Length", "Name", "Format", "Columns"])}
    pos, n, rows, problems, meta = 0, len(data), [], [], []
    records, failsafe, prearm = 0, False, ""
    while pos + 3 <= n and records < MAX_LOG_RECORDS * 4:
        if data[pos] != 0xA3 or data[pos + 1] != 0x95:
            pos += 1
            continue
        mt = data[pos + 2]
        f = fmts.get(mt)
        if f is None:
            pos += 1
            continue
        mname, ln, fmt, labels = f
        if pos + ln > n:
            problems.append("ArduPilot log truncated mid-message")
            break
        body = data[pos + 3:pos + ln]
        pos += ln
        records += 1
        try:
            vals, off = {}, 0
            for lab, ch in zip(labels, fmt):
                sz = _DF_SIZES[ch]
                raw = body[off:off + sz]
                if ch in "nNZ":
                    v = raw.split(b"\x00")[0].decode("utf-8", "replace")
                elif ch == "a":
                    v = None
                else:
                    v = struct.unpack("<" + _DF_FMT[ch], raw)[0]
                    if ch in "cCeE":
                        v = v / 100.0
                    elif ch == "L":
                        v = v / 1e7
                vals[lab] = v
                off += sz
        except (struct.error, KeyError):
            problems.append("undecodable ArduPilot message %s" % visible(mname, 8))
            continue
        if mt == 128:
            t, ln2, nm, fm, cols = vals["Type"], vals["Length"], vals["Name"], vals["Format"], vals["Columns"]
            fmts[t] = (nm, ln2, fm, cols.split(","))
            continue
        r = None
        if mname == "VIBE":
            r = blank_flight_row()
            r["imu_vib_x"], r["imu_vib_y"], r["imu_vib_z"] = _f(vals.get("VibeX")), _f(vals.get("VibeY")), _f(vals.get("VibeZ"))
            clips = [vals[k] for k in ("Clip", "Clip0", "Clip1", "Clip2") if vals.get(k) is not None]
            if clips:
                r["imu_clip_count"] = float(sum(clips))
        elif mname in ("XKF4", "NKF4"):
            r = blank_flight_row()
            r["ekf_vel_innov"], r["ekf_pos_innov"], r["ekf_hgt_innov"], r["ekf_mag_innov"] = (_f(vals.get(k)) for k in ("SV", "SP", "SH", "SM"))
        elif mname == "GPS":
            r = blank_flight_row()
            r["gnss_fix_type"], r["gnss_satellites"], r["gnss_hdop"] = _f(vals.get("Status")), _f(vals.get("NSats")), _f(vals.get("HDop"))
        elif mname == "ESC":
            r = blank_flight_row()
            r["esc_id"] = str(int(vals.get("Instance") or 0))
            r["esc_rpm"], r["esc_current_a"], r["esc_temp_c"] = _f(vals.get("RPM")), _f(vals.get("Curr")), _f(vals.get("Temp"))
        elif mname == "ERR":
            if vals.get("Subsys") in FAILSAFE_SUBSYS and vals.get("ECode"):
                failsafe = True
        elif mname == "MSG":
            msg = str(vals.get("Message") or "")
            meta.append(("log:%d" % len(meta), msg[:300]))
            if msg.lower().startswith("prearm") and not prearm:
                prearm = msg
        if r is not None:
            rows.append(r)
    if failsafe:
        r = blank_flight_row()
        r["failsafe_active"] = True
        rows.append(r)
    if prearm:
        r = blank_flight_row()
        r["prearm_error"] = prearm
        rows.append(r)
    if len(fmts) <= 1:
        problems.append("ArduPilot log has no FMT definitions")
    return {"name": name, "kind": "ardupilot", "rows": rows, "meta": meta, "problems": list(dict.fromkeys(problems)), "topics": sorted(v[0] for v in fmts.values())}


# ---------------------------------------------------------------- input collection
TELEMETRY_EXTS = (".db3", ".mcap", ".ulg", ".bin", ".urdf", ".sdf", ".xml", ".zip", ".log", ".yaml", ".yml", ".sqlite", ".bag")
MAX_ZIP_BYTES = 128 * 1024 * 1024
ROLE_SIGNATURES = {
    "bms": ({"pack_id"}, {"cell_min_mv", "cell_max_mv", "cell_voltages_mv", "series_cells", "pack_voltage_v", "soh_pct"}),
    "actuators": ({"joint_name", "actuator_id"}, {"stator_temp_c", "rms_torque_nm", "peak_torque_nm", "backlash_arcmin", "duty_cycle_pct",
                                                   "operating_hours", "limit_torque_nm", "velocity_rad_s"}),
}
FLIGHT_SIG = set(FLIGHT_NUM) | {"esc_id", "failsafe_active", "prearm_error"}
CAN_ID_COLS = {"cob_id", "can_id", "arbitration_id", "frame_id"}


def sniff_table_role(rows):
    keys = set()
    for r in rows[:50]:
        keys |= set(r)
    for role in ("bms", "actuators"):
        need, any_of = ROLE_SIGNATURES[role]
        if keys & need and keys & any_of:
            return role
    if keys & FLIGHT_SIG:
        return "flight"
    if keys & CAN_ID_COLS or ("id" in keys and keys & {"dlc", "data", "payload"}):
        return "can"
    return None


def role_cap(role, ext):
    if ext in (".db3", ".mcap"):
        return MAX_BAG_BYTES
    if ext in (".ulg", ".bin"):
        return MAX_LOG_BYTES
    if role == "can" or ext == ".log":
        return MAX_CAN_BYTES
    if role in ("rosbag", "urdf"):
        return MAX_META_BYTES
    return MAX_TABLE_BYTES


def make_src(role, name, **kw):
    s = {"role": role, "name": name, "path": None, "data": None, "rows": None, "doc": None, "origin": None}
    s.update(kw)
    return s


def build_src(name, data, forced, path=None, origin=None, degraded=None, explicit=True):
    """Classify one telemetry blob (file or archive member) -> source dict, or None when it is not telemetry we audit."""
    ext = os.path.splitext(name)[1].lower()
    role = None
    rows = doc = None
    if ext in (".db3", ".mcap"):
        role = "rosbag"
    elif ext == ".ulg":
        role = "flight"
    elif ext == ".bin":
        role = "flight" if (data is None or data[:2] == b"\xa3\x95" or forced == "flight") else None
    elif ext in (".urdf", ".sdf"):
        role = "urdf"
    elif ext == ".xml":
        role = "urdf" if (forced == "urdf" or re.search(rb"<\s*(?:robot|sdf)\b", data[:65536] or b"")) else None
    elif ext == ".log":
        role = "can"
    elif ext in (".yaml", ".yml"):
        role = "rosbag"
    elif ext in (".csv", ".json"):
        text = data[:65536].decode("utf-8-sig", "replace") if data else ""
        if forced == "rosbag" and ext == ".json" or (not forced and ext == ".json" and text.lstrip()[:1] == "{"
                                                      and re.search(r'"(?:topics|rosbag2_bagfile_information|tf_edges)"', text)):
            role = "rosbag"
        elif forced == "urdf":
            role = None
        else:
            plural = {"actuators": "actuators", "bms": "packs", "flight": "samples", "can": "frames"}.get(forced, "rows")
            try:
                rows, err = load_table_bytes(data, plural)
            except Exception:
                rows, err = None, "unreadable"
            sniffed = sniff_table_role(rows) if rows else None
            role = sniffed or (forced if forced in ("actuators", "bms", "flight", "can") else None)
            if role is None and not err and degraded is not None and explicit:
                degraded.append("%s: columns match no known telemetry schema; skipped" % visible(name, 80))
    if role is None:
        return None
    if forced and role != forced:
        if explicit and degraded is not None:
            degraded.append("%s: is a %s file, not valid for %s; skipped" % (visible(name, 80), role, ROLE_FLAG[forced]))
        return None
    return make_src(role, name, path=path, data=data, rows=rows, doc=doc, origin=origin)


def collect_sources(args, degraded, violations):
    """Walk every PATH / flag, read bounded bytes (never following symlinks) -> list of source dicts."""
    srcs, seen = [], set()

    def add_bytes(label, name, data, forced, origin=None, path=None, explicit=True):
        s = build_src(name, data, forced, path=path, origin=origin, degraded=degraded, explicit=explicit)
        if s is not None:
            s["name"] = label
            srcs.append(s)

    def add_zip(path, forced, explicit):
        try:
            data = read_regular(path, MAX_ZIP_BYTES)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
            degraded.append("%s: unreadable archive (%s)" % (visible(path, 100), visible(e, 80)))
            return
        with zf:
            problems = check_archive(zf)
            if problems:
                violations.append(mk("CRITICAL", "ARCHIVE_SAFETY_VIOLATION", "%s rejected before parsing: %s" % (visible(os.path.basename(path), 60), "; ".join(problems[:4])),
                                     key="ARCHIVE_SAFETY_VIOLATION:" + os.path.basename(path), archive=os.path.basename(path), problems=problems))
                degraded.append("%s: unsafe archive rejected (%s)" % (visible(path, 100), problems[0]))
                return
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                ext = os.path.splitext(zi.filename)[1].lower()
                if ext == ".zip":
                    degraded.append("%s!%s: nested archive not opened" % (visible(path, 60), visible(zi.filename, 60)))
                    continue
                if ext not in (".csv", ".json", ".log", ".urdf", ".sdf", ".xml", ".yaml", ".yml", ".ulg", ".bin", ".db3", ".mcap"):
                    continue
                try:
                    blob = read_member(zf, zi, min(role_cap(forced or "", ext), MAX_LOG_BYTES))
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as e:
                    degraded.append("%s!%s: %s" % (visible(path, 60), visible(zi.filename, 60), visible(e, 80)))
                    continue
                label = "%s!%s" % (os.path.basename(path), zi.filename)
                add_bytes(label, zi.filename, blob, forced, origin=os.path.basename(path), explicit=False)

    def add_file(path, forced, explicit):
        ext = os.path.splitext(path)[1].lower()
        rp = os.path.realpath(path)
        if (rp, forced) in seen:
            return
        seen.add((rp, forced))
        if ext == ".zip":
            add_zip(path, forced, explicit)
            return
        if explicit and forced and ext not in EXTS[forced] and ext != ".zip":
            raise UsageError("unsupported file type %s for %s (expected %s)" % (visible(path, 80), ROLE_FLAG[forced], ", ".join(EXTS[forced])))
        if not forced and ext not in (".csv", ".json", ".log", ".urdf", ".sdf", ".xml", ".yaml", ".yml", ".ulg", ".bin", ".db3", ".mcap"):
            if explicit:
                raise UsageError("unsupported file type %s" % visible(path, 80))
            return
        try:
            if ext in (".db3", ".mcap"):
                fh = open_regular(path, MAX_BAG_BYTES)
                fh.close()
                srcs.append(make_src("rosbag", os.path.basename(path), path=path))
                return
            data = read_regular(path, role_cap(forced or "", ext))
        except (OSError, ValueError) as e:
            degraded.append("%s: unreadable (%s)" % (visible(path, 100), visible(getattr(e, "strerror", None) or e, 80)))
            return
        add_bytes(os.path.basename(path), os.path.basename(path), data, forced, path=path, explicit=explicit)

    def walk(path, forced, explicit):
        if os.path.islink(path):
            if explicit and not os.path.isdir(path):
                raise UsageError("refusing symlink input %s: symlinks are never followed" % visible(path, 80))
            degraded.append("%s: symlink skipped (symlinks are never followed)" % visible(path, 100))
            return
        if os.path.isdir(path):
            count = 0
            for dp, dns, fns in os.walk(path, followlinks=False):
                dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
                for fn in sorted(fns):
                    p = os.path.join(dp, fn)
                    if os.path.islink(p):
                        degraded.append("%s: symlink skipped (symlinks are never followed)" % visible(p, 100))
                        continue
                    if forced and os.path.splitext(fn)[1].lower() not in EXTS[forced] + (".zip",):
                        continue
                    count += 1
                    if count > MAX_DISCOVERED:
                        degraded.append("%s: directory scan truncated at %d files" % (visible(path, 100), MAX_DISCOVERED))
                        return
                    try:
                        add_file(p, forced, False)
                    except UsageError:
                        raise
        elif os.path.isfile(path):
            add_file(path, forced, explicit)
        else:
            degraded.append("%s: not found" % visible(path, 100))

    for p in args.paths + args.dir:
        walk(p, None, True)
    for role in ROLES:
        for p in getattr(args, role if role != "flight" else "flight_log") or []:
            walk(p, role, True)
    return srcs


# ---------------------------------------------------------------- audit pipeline
BUCKETS = (
    ("1", "Actuator, joint & harmonic-drive duty cycles",
     {"ACTUATOR_THERMAL_OVERLOAD", "JOINT_TORQUE_LIMIT_EXCEEDED", "JOINT_VELOCITY_LIMIT_EXCEEDED", "JOINT_POSITION_LIMIT_EXCEEDED",
      "ACTUATOR_HIGH_DUTY_CYCLE", "HARMONIC_DRIVE_WEAR_THRESHOLD", "CAN_BUS_FAULT_OR_TIMEOUT", "XML_ENTITY_SAFETY_VIOLATION",
      "ARCHIVE_SAFETY_VIOLATION", "ACTUATOR_ERROR_FLAGS_SET", "URDF_JOINT_LIMIT_INVALID", "ACTUATOR_JOINT_NOT_IN_MODEL"}),
    ("2", "16S LiFePO4 / LiPo battery management system",
     {"BMS_CRITICAL_CELL_SAG", "BMS_CELL_IMBALANCE_DELTA", "BMS_MOSFET_THERMAL_RISE", "BMS_DISCHARGE_CRATE_EXCEEDED", "BMS_SOH_DEGRADATION",
      "BMS_CELL_COUNT_MISMATCH"}),
    ("3", "ROS 2 bag, TF tree & QoS integrity",
     {"TF_TREE_CYCLE_OR_DISCONNECT", "ROSBAG_TOPIC_DROPOUT", "QOS_INCOMPATIBLE_MISMATCH", "ROSBAG_CLOCK_DRIFT_OR_JUMP"}),
    ("4", "PX4 / ArduPilot EKF, IMU, GNSS & ESC flight readiness",
     {"IMU_VIBRATION_CLIPPING", "EKF_INNOVATION_DIVERGENCE", "ESC_DESYNC_OR_OVERHEAT", "GNSS_DEGRADED_FIX", "FLIGHT_FAILSAFE_OR_PREARM_FAULT"}),
)
EVENT_CODES = ("BMS_CELL_DELTA_SPIKE", "ACTUATOR_THERMAL_DRIFT", "SOH_DEGRADATION_STEP", "TOPIC_RATE_REGRESSION")


def audit(srcs, degraded, violations):
    """Parse every source and run all four buckets -> result dict."""
    R = {"actuators": [], "bms": [], "bags": [], "flights": [], "models": [], "can_frames": [], "can_summary": [], "findings": list(violations),
         "joints": {}, "inputs": {}, "flight_summary": None}
    fs = {f.key: f for f in R["findings"]}
    specs, measured = [], []
    ncount = {r: 0 for r in ROLES}
    for s in srcs:
        role, name = s["role"], s["name"]
        ncount[role] += 1
        try:
            if role in ("actuators", "bms", "flight", "can") and not (role == "can" and name.lower().endswith(".log")) \
                    and not (role == "flight" and name.lower().endswith((".ulg", ".bin"))):
                rows = s["rows"]
                if rows is None:
                    rows, err = load_table_bytes(s["data"], {"actuators": "actuators", "bms": "packs", "flight": "samples", "can": "frames"}[role])
                    if err:
                        degraded.append("%s: %s" % (visible(name, 80), err))
                        continue
                scan_rows(rows, visible(name, 40))
                if role == "can":
                    frames, bad = [], 0
                    for n, r in enumerate(rows, 1):
                        try:
                            frames.append(can_frame_from_row(r))
                        except RowError as e:
                            bad += 1
                            if bad <= 5:
                                degraded.append("%s row %d skipped: %s" % (visible(name, 40), n, e))
                    if bad > 5:
                        degraded.append("%s: %d more CAN rows skipped" % (visible(name, 40), bad - 5))
                    R["can_frames"] += frames
                else:
                    parser = {"actuators": parse_actuator, "bms": parse_bms, "flight": parse_flight_row}[role]
                    out = []
                    for n, r in enumerate(rows, 1):
                        try:
                            out.append(parser(r))
                        except RowError as e:
                            degraded.append("%s row %d skipped: %s" % (visible(name, 40), n, e))
                    if role == "flight":
                        R["flights"].append({"name": name, "kind": "table", "rows": out, "meta": [], "problems": []})
                    else:
                        R[role].extend(out)
            elif role == "can":
                text = decode_text(s["data"])
                scan_text_lines(text, visible(name, 40))
                frames, skipped = parse_candump(text)
                if len(frames) > MAX_CAN_FRAMES:
                    degraded.append("%s: more than %d frames; analysed the first %d" % (visible(name, 40), MAX_CAN_FRAMES, MAX_CAN_FRAMES))
                    frames = frames[:MAX_CAN_FRAMES]
                if skipped:
                    degraded.append("%s: %d unparseable candump line(s) skipped" % (visible(name, 40), skipped))
                if not frames:
                    degraded.append("%s: no CAN frames found" % visible(name, 80))
                R["can_frames"] += frames
            elif role == "flight":
                data = s["data"]
                fl = parse_ulog(name, data) if name.lower().endswith(".ulg") else parse_ardupilot_bin(name, data)
                for pr in fl["problems"]:
                    degraded.append("%s: %s" % (visible(name, 60), pr))
                for f in credential_findings(name, fl["meta"]):
                    fs.setdefault(f.key, f)
                R["flights"].append(fl)
            elif role == "urdf":
                raw_text = s["data"][:MAX_META_BYTES].decode("utf-8", "replace") if s["data"] else ""
                for f in credential_findings(name, [("line %d" % n, ln) for n, ln in enumerate(raw_text.splitlines()[:20000], 1)]):
                    fs.setdefault(f.key, f)
                try:
                    model = parse_urdf(s["data"])
                except ValueError as e:
                    if "DTD" in str(e) or "encoding" in str(e):
                        f = mk("CRITICAL", "XML_ENTITY_SAFETY_VIOLATION", "%s rejected before parsing: %s" % (visible(name, 60), visible(e, 100)),
                               key="XML_ENTITY_SAFETY_VIOLATION:" + name, file=name)
                        fs[f.key] = f
                        degraded.append("%s: unsafe XML rejected (%s)" % (visible(name, 80), visible(e, 80)))
                    else:
                        degraded.append("%s: %s" % (visible(name, 80), visible(e, 100)))
                    continue
                except ET.ParseError as e:
                    degraded.append("%s: malformed XML (%s)" % (visible(name, 80), visible(e, 80)))
                    continue
                model["name"] = name
                for pr in model["problems"]:
                    degraded.append("%s: %s" % (visible(name, 80), pr))
                for f in credential_findings(name, model["strings"]):
                    fs.setdefault(f.key, f)
                for jn, j in model["joints"].items():
                    R["joints"].setdefault(jn, j)
                R["models"].append(model)
                for f in urdf_findings(name, model):
                    fs.setdefault(f.key, f)
                edges = {(j["parent"], j["child"]) for j in model["joints"].values() if j["parent"] and j["child"]}
                for f in tf_graph_findings(name, edges, "URDF/SDF link"):
                    fs.setdefault(f.key, f)
            elif role == "rosbag":
                ext = os.path.splitext(name)[1].lower()
                if ext == ".db3":
                    bag = parse_db3(name, path=s["path"], data=s["data"] if s["path"] is None else None)
                elif ext == ".mcap":
                    if s["path"]:
                        fh = open_regular(s["path"], MAX_BAG_BYTES)
                    else:
                        fh = io.BytesIO(s["data"])
                    try:
                        bag = parse_mcap(name, fh)
                    finally:
                        fh.close()
                else:
                    bag = parse_bag_summary(name, load_doc(s["data"]))
                for pr in bag["problems"]:
                    degraded.append("%s: %s" % (visible(name, 60), pr))
                for f in credential_findings(name, bag["meta"]):
                    fs.setdefault(f.key, f)
                (measured if bag["kind"] != "summary" or bag["has_counts"] else specs).append(bag)
        except UsageError:
            raise
        except (OSError, ValueError, KeyError, TypeError, struct.error, zipfile.BadZipFile, sqlite3.Error) as e:
            degraded.append("%s: unreadable or malformed %s (%s)" % (visible(name, 80), role, visible(e, 100)))
    mp = []
    merge_expectations(measured, specs, mp)
    degraded += mp
    R["bags"] = measured
    R["inputs"] = ncount
    for a in actuator_findings(R["actuators"], R["joints"]):
        put(fs, a)
    for b in bms_findings(R["bms"]):
        put(fs, b)
    for bag in R["bags"]:
        for f in bag_findings(bag):
            put(fs, f)
    cf, R["can_summary"] = analyze_can(R["can_frames"])
    for f in cf:
        put(fs, f)
    rows = [r for fl in R["flights"] for r in fl["rows"]]
    R["flight_summary"] = flight_summary(rows) if rows else None
    if R["flight_summary"]:
        for f in flight_findings(R["flight_summary"]):
            put(fs, f)
    R["findings"] = list(fs.values())
    return R


# ---------------------------------------------------------------- state + delta
def make_state(R):
    cur = {"actuators": {}, "bms": {}, "topics": {}}
    for a in R["actuators"]:
        if a["stator_temp_c"] is not None:
            old = cur["actuators"].get(a["joint_name"])
            if old is None or a["stator_temp_c"] > old["stator_temp_c"]:
                cur["actuators"][a["joint_name"]] = {"stator_temp_c": a["stator_temp_c"], "max_temp_c": a["max_temp_c"]}
    for b in R["bms"]:
        old = cur["bms"].get(b["pack_id"])
        if old is None or b["delta_mv"] > old["delta_mv"]:
            cur["bms"][b["pack_id"]] = {"delta_mv": b["delta_mv"], "soh_pct": b["soh_pct"], "cycle_count": b["cycle_count"]}
    for bag in R["bags"]:
        for n, t in bag["topics"].items():
            if t["rate_hz"] is not None and not is_tf_static(n):
                old = cur["topics"].get(n)
                if old is None or t["rate_hz"] < old["rate_hz"]:
                    cur["topics"][n] = {"rate_hz": t["rate_hz"], "expected_hz": t["expected_hz"]}
    cur["findings_by_key"] = {f.key: f.to_dict() for f in bc.sort_findings(R["findings"])}
    return clean(cur)


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

    pb, cb = prev.get("bms") or {}, cur["bms"]
    for pid in sorted(set(pb) & set(cb)):
        a, b = pb[pid], cb[pid]
        if a.get("delta_mv") is not None and b.get("delta_mv") is not None:
            inc = b["delta_mv"] - a["delta_mv"]
            if inc >= 25.0:
                add("HIGH" if inc >= 50.0 or b["delta_mv"] >= 80.0 else "MEDIUM", "BMS_CELL_DELTA_SPIKE",
                    "pack %s cell spread grew %.0f -> %.0f mV (+%.0f mV) since baseline" % (visible(pid, 40), a["delta_mv"], b["delta_mv"], inc),
                    pid, pack_id=pid, previous_mv=a["delta_mv"], delta_mv=b["delta_mv"])
        if a.get("soh_pct") is not None and b.get("soh_pct") is not None:
            drop = a["soh_pct"] - b["soh_pct"]
            if drop >= 2.0:
                add("HIGH" if drop >= 5.0 else "MEDIUM", "SOH_DEGRADATION_STEP",
                    "pack %s state of health fell %.1f%% -> %.1f%% (-%.1f points) since baseline" % (visible(pid, 40), a["soh_pct"], b["soh_pct"], drop),
                    pid, pack_id=pid, previous_soh_pct=a["soh_pct"], soh_pct=b["soh_pct"])
    pa, ca = prev.get("actuators") or {}, cur["actuators"]
    for j in sorted(set(pa) & set(ca)):
        inc = ca[j]["stator_temp_c"] - (pa[j].get("stator_temp_c") or 0.0)
        if inc >= 10.0:
            add("HIGH" if inc >= 20.0 else "MEDIUM", "ACTUATOR_THERMAL_DRIFT",
                "%s stator temperature rose %.1f -> %.1f C (+%.1f C) since baseline" % (visible(j, 40), pa[j]["stator_temp_c"], ca[j]["stator_temp_c"], inc),
                j, joint=j, previous_c=pa[j]["stator_temp_c"], stator_temp_c=ca[j]["stator_temp_c"])
    pt, ct = prev.get("topics") or {}, cur["topics"]
    for n in sorted(set(pt) & set(ct)):
        old, new = pt[n].get("rate_hz"), ct[n]["rate_hz"]
        if old and old > 0 and new <= 0.8 * old:
            add("HIGH" if new <= 0.5 * old else "MEDIUM", "TOPIC_RATE_REGRESSION",
                "topic %s rate fell %.2f -> %.2f Hz (-%.0f%%) since baseline" % (visible(n, 60), old, new, (1 - new / old) * 100),
                n, topic=n, previous_hz=old, rate_hz=new)
    return ev


def compute_exit(findings, degraded, fail_on):
    if fail_on != "none" and findings:
        worst = max(bc.SEV[f.sev] for f in findings)
        if worst >= bc.SEV[fail_on.upper()]:
            return bc.EXIT_CRITICAL if worst == 4 else bc.EXIT_HIGH if worst == 3 else bc.EXIT_MEDIUM
    return bc.EXIT_DEGRADED if degraded else bc.EXIT_OK


# ---------------------------------------------------------------- maintenance & flight-readiness checklist
ACTIONS = {
    "ACTUATOR_THERMAL_OVERLOAD": "Stop the joint, let it cool, check cooling path, load profile and thermal sensor before re-enabling",
    "JOINT_TORQUE_LIMIT_EXCEEDED": "Review torque limits/current clamps and the trajectory that produced the peak; inspect the gearbox for shock damage",
    "JOINT_VELOCITY_LIMIT_EXCEEDED": "Reduce the commanded velocity or re-tune the controller; confirm model velocity limits match the drive",
    "JOINT_POSITION_LIMIT_EXCEEDED": "Check soft limits, encoder zeroing and hard stops; re-home the joint",
    "ACTUATOR_HIGH_DUTY_CYCLE": "Rebalance the task schedule or add rest periods; check gear ratio / payload sizing",
    "HARMONIC_DRIVE_WEAR_THRESHOLD": "Schedule harmonic drive inspection: backlash measurement, grease change and flexspline check",
    "CAN_BUS_FAULT_OR_TIMEOUT": "Inspect termination (120 ohm), shielding, connectors and the faulting node; verify heartbeat producer settings",
    "XML_ENTITY_SAFETY_VIOLATION": "Re-export the robot model without a DTD / entity declarations",
    "ARCHIVE_SAFETY_VIOLATION": "Do not open the archive; re-create it from the original logs",
    "ACTUATOR_ERROR_FLAGS_SET": "Read the drive fault log offline and clear the root cause before the next run",
    "URDF_JOINT_LIMIT_INVALID": "Correct the joint <limit> in the model so limit checks are meaningful",
    "ACTUATOR_JOINT_NOT_IN_MODEL": "Align joint names between telemetry and the URDF/SDF",
    "BMS_CRITICAL_CELL_SAG": "Take the pack out of service; check the weak cell group, load, and cell internal resistance",
    "BMS_CELL_IMBALANCE_DELTA": "Run a full balance charge; if the spread returns, replace or inspect the weak cell group",
    "BMS_MOSFET_THERMAL_RISE": "Inspect BMS MOSFET heat sinking, current limit and ambient cooling",
    "BMS_DISCHARGE_CRATE_EXCEEDED": "Lower the peak current draw or use a higher-capacity / higher-C pack",
    "BMS_SOH_DEGRADATION": "Plan pack replacement or capacity test; derate the mission profile",
    "BMS_CELL_COUNT_MISMATCH": "Check the balance-lead harness and BMS channel configuration",
    "TF_TREE_CYCLE_OR_DISCONNECT": "Fix the duplicate / looping transform publishers so the tf graph is a single tree",
    "ROSBAG_TOPIC_DROPOUT": "Check the publisher node, driver, cabling and recording QoS for the dropped topic",
    "QOS_INCOMPATIBLE_MISMATCH": "Align publisher and subscriber QoS (reliability, durability, history depth)",
    "ROSBAG_CLOCK_DRIFT_OR_JUMP": "Check time sync (chrony / PTP) and use_sim_time consistency across nodes",
    "IMU_VIBRATION_CLIPPING": "Inspect IMU mounting/dampers, balance props, and check for loose frame parts before flight",
    "EKF_INNOVATION_DIVERGENCE": "Review sensor health (GNSS, baro, mag) and calibration; do not fly until innovations settle",
    "ESC_DESYNC_OR_OVERHEAT": "Inspect motor/ESC/prop combination, timing and cooling; replace a desyncing ESC",
    "GNSS_DEGRADED_FIX": "Check antenna placement, interference and sky view; delay outdoor flight until fix quality is good",
    "FLIGHT_FAILSAFE_OR_PREARM_FAULT": "Resolve the failsafe / pre-arm cause and repeat the pre-flight checks",
    "EMBEDDED_TELEMETRY_CREDENTIAL": "Rotate the exposed credential and re-export the log without it",
}
DUE = {"CRITICAL": "before next operation", "HIGH": "before next flight / shift", "MEDIUM": "next scheduled service (<= 30 days)", "LOW": "monitor", "INFO": "note"}
STATUS = {"CRITICAL": "NO-GO", "HIGH": "ACTION_REQUIRED", "MEDIUM": "SCHEDULE", "LOW": "MONITOR", "INFO": "NOTE"}
READINESS = (
    ("Actuators", "actuators", {"ACTUATOR_THERMAL_OVERLOAD", "JOINT_TORQUE_LIMIT_EXCEEDED", "JOINT_VELOCITY_LIMIT_EXCEEDED", "JOINT_POSITION_LIMIT_EXCEEDED",
                                "ACTUATOR_HIGH_DUTY_CYCLE", "HARMONIC_DRIVE_WEAR_THRESHOLD", "ACTUATOR_ERROR_FLAGS_SET"}),
    ("Battery / BMS", "bms", {c for c in BUCKETS[1][2]}),
    ("CAN / RS485 bus", "can_frames", {"CAN_BUS_FAULT_OR_TIMEOUT"}),
    ("ROS 2 topic rates", "bags", {"ROSBAG_TOPIC_DROPOUT"}),
    ("ROS 2 QoS", "bags", {"QOS_INCOMPATIBLE_MISMATCH"}),
    ("Clock sync", "bags", {"ROSBAG_CLOCK_DRIFT_OR_JUMP"}),
    ("TF tree", "bags", {"TF_TREE_CYCLE_OR_DISCONNECT"}),
    ("IMU vibration / clipping", "flight_summary", {"IMU_VIBRATION_CLIPPING"}),
    ("EKF health", "flight_summary", {"EKF_INNOVATION_DIVERGENCE"}),
    ("GNSS", "flight_summary", {"GNSS_DEGRADED_FIX"}),
    ("ESC", "flight_summary", {"ESC_DESYNC_OR_OVERHEAT"}),
    ("Failsafe / pre-arm", "flight_summary", {"FLIGHT_FAILSAFE_OR_PREARM_FAULT"}),
)
CATEGORY = {}
for _codes, _cat in (({"ACTUATOR_THERMAL_OVERLOAD", "JOINT_TORQUE_LIMIT_EXCEEDED", "JOINT_VELOCITY_LIMIT_EXCEEDED", "JOINT_POSITION_LIMIT_EXCEEDED",
                       "ACTUATOR_HIGH_DUTY_CYCLE", "HARMONIC_DRIVE_WEAR_THRESHOLD", "ACTUATOR_ERROR_FLAGS_SET", "URDF_JOINT_LIMIT_INVALID",
                       "ACTUATOR_JOINT_NOT_IN_MODEL"}, "Actuator / joint"), (BUCKETS[1][2], "Battery / BMS"), ({"CAN_BUS_FAULT_OR_TIMEOUT"}, "CAN / RS485 bus"),
                      (BUCKETS[2][2], "ROS 2 bag"), (BUCKETS[3][2], "Flight readiness"),
                      ({"XML_ENTITY_SAFETY_VIOLATION", "ARCHIVE_SAFETY_VIOLATION", "EMBEDDED_TELEMETRY_CREDENTIAL"}, "Data safety")):
    for _c in _codes:
        CATEGORY[_c] = _cat
MAINT_COLUMNS = ("priority", "category", "asset", "check", "status", "severity", "finding_code", "due", "action")


def maintenance_rows(R, findings):
    rows = []
    for f in bc.sort_findings(findings):
        rows.append((5 - bc.SEV[f.sev], CATEGORY.get(f.code, "Other"), f.evidence.get("joint") or f.evidence.get("pack_id") or f.evidence.get("topic") or f.evidence.get("bus") or "-",
                     f.msg, STATUS[f.sev], f.sev, f.code, DUE[f.sev], ACTIONS.get(f.code, "Review the finding")))
    for a in R["actuators"]:
        if a["operating_hours"] is not None and a["service_interval_hrs"]:
            left = a["service_interval_hrs"] - a["operating_hours"]
            st = "OVERDUE" if left <= 0 else "DUE_SOON" if left <= 0.1 * a["service_interval_hrs"] else "OK"
            rows.append((6 if st == "OK" else 3, "Preventive maintenance", a["joint_name"], "Harmonic drive service interval (%.0f of %.0f h)" % (a["operating_hours"], a["service_interval_hrs"]),
                         st, "-", "-", "now" if left <= 0 else "in %.0f operating hours" % left, "Grease / backlash / flexspline service per drive manual"))
    for b in R["bms"]:
        if b["cycle_count"] is not None:
            rated = b["rated_cycles"] or DEFAULT_RATED_CYCLES[b["chemistry"]]
            st = "OVERDUE" if b["cycle_count"] >= rated else "DUE_SOON" if b["cycle_count"] >= 0.9 * rated else "OK"
            rows.append((6 if st == "OK" else 3, "Preventive maintenance", b["pack_id"], "Battery cycle life (%.0f of %.0f cycles, SOH %s%%)" % (b["cycle_count"], rated, fd(b["soh_pct"])),
                         st, "-", "-", "now" if st == "OVERDUE" else "in %.0f cycles" % (rated - b["cycle_count"]), "Capacity test and replacement planning"))
    have = {"actuators": bool(R["actuators"]), "bms": bool(R["bms"]), "can_frames": bool(R["can_frames"]), "bags": bool(R["bags"]),
            "flight_summary": R["flight_summary"] is not None}
    for label, src, codes in READINESS:
        if not have[src]:
            continue
        hit = [f for f in findings if f.code in codes]
        if not hit:
            rows.append((6, "Flight readiness", "-", label, "PASS", "-", "-", "-", "No finding from the supplied logs"))
    worst = max((bc.SEV[f.sev] for f in findings if f.code != "EMBEDDED_TELEMETRY_CREDENTIAL"), default=-1)
    gate = "NO-GO" if worst >= 3 else "CAUTION" if worst == 2 else "GO"
    rows.append((0 if gate == "NO-GO" else 4 if gate == "CAUTION" else 7, "Flight readiness", "OVERALL", "Heuristic readiness gate over the supplied logs (not an airworthiness or safety approval)",
                 gate, "-", "-", "-", "Gate is NO-GO on any HIGH/CRITICAL, CAUTION on MEDIUM; always run the vehicle's own pre-flight checks"))
    return sorted(rows, key=lambda r: (r[0], r[1], str(r[2])))


def maint_csv_text(rows):
    def safe(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") and v != "-" else v
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(MAINT_COLUMNS)
    for r in rows:
        w.writerow([r[0]] + [safe(c) for c in r[1:]])
    return buf.getvalue()


# ---------------------------------------------------------------- built-in self-check fixtures
SELFCHECK_EXPECTED = {
    "ACTUATOR_THERMAL_OVERLOAD", "JOINT_TORQUE_LIMIT_EXCEEDED", "JOINT_VELOCITY_LIMIT_EXCEEDED", "JOINT_POSITION_LIMIT_EXCEEDED",
    "ACTUATOR_HIGH_DUTY_CYCLE", "HARMONIC_DRIVE_WEAR_THRESHOLD", "CAN_BUS_FAULT_OR_TIMEOUT", "BMS_CRITICAL_CELL_SAG", "BMS_CELL_IMBALANCE_DELTA",
    "BMS_MOSFET_THERMAL_RISE", "BMS_DISCHARGE_CRATE_EXCEEDED", "BMS_SOH_DEGRADATION", "TF_TREE_CYCLE_OR_DISCONNECT", "ROSBAG_TOPIC_DROPOUT",
    "QOS_INCOMPATIBLE_MISMATCH", "ROSBAG_CLOCK_DRIFT_OR_JUMP", "IMU_VIBRATION_CLIPPING", "EKF_INNOVATION_DIVERGENCE", "ESC_DESYNC_OR_OVERHEAT",
    "GNSS_DEGRADED_FIX", "FLIGHT_FAILSAFE_OR_PREARM_FAULT", "EMBEDDED_TELEMETRY_CREDENTIAL"}
SC_T0 = 1_761_523_200


def _cdr_stamped(sec, nsec):
    return b"\x00\x01\x00\x00" + struct.pack("<iI", sec, nsec) + b"\x00" * 16


def _cdr_at(t):
    """CDR payload whose header stamp is SC_T0 + t seconds."""
    whole = int(math.floor(t))
    return _cdr_stamped(SC_T0 + whole, int(round((t - whole) * 1e9)))


def _cdr_tf(edges):
    b = bytearray(b"\x00\x01\x00\x00")

    def al(n):
        while (len(b) - 4) % n:
            b.append(0)
    al(4)
    b += struct.pack("<I", len(edges))
    for p, c in edges:
        al(4)
        b += struct.pack("<iI", 0, 0)
        for s in (p, c):
            al(4)
            enc = s.encode() + b"\x00"
            b += struct.pack("<I", len(enc)) + enc
        al(8)
        b += b"\x00" * 56
    return bytes(b)


def _sc_db3(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE topics(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL, serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL)")
    con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, timestamp INTEGER NOT NULL, data BLOB NOT NULL)")
    topics = [("/scan", "sensor_msgs/msg/LaserScan", "- history: 1\n  depth: 5\n  reliability: 2\n  durability: 2\n"),
              ("/imu/data", "sensor_msgs/msg/Imu", "- history: 1\n  depth: 10\n  reliability: 1\n  durability: 2\n"),
              ("/joint_states", "sensor_msgs/msg/JointState", "- history: 2\n  depth: 0\n  reliability: 1\n  durability: 2\n"),
              ("/tf", "tf2_msgs/msg/TFMessage", ""), ("/tf_static", "tf2_msgs/msg/TFMessage", "")]
    for i, (n, t, q) in enumerate(topics, 1):
        con.execute("INSERT INTO topics VALUES (?,?,?,?,?)", (i, n, t, "cdr", q))
    rows = []
    ns = lambda s: int(round((SC_T0 + s) * 1e9))  # noqa: E731
    for k in list(range(40)) + list(range(56, 106)):                      # /scan: 90 msgs with a 4.25 s hole
        t = k * 0.25
        rows.append((ns(t), 1, _cdr_at(t - 0.005)))
    for k in range(1500):                                                 # /imu/data 50 Hz, one backward timestamp jump
        t = k * 0.02 - (0.5 if k == 700 else 0.0)
        rows.append((ns(t), 2, _cdr_at(t - 0.003)))
    for k in range(600):                                                  # /joint_states 20 Hz, header stamp 70 ms behind
        t = k * 0.05
        rows.append((ns(t), 3, _cdr_at(t - 0.07)))
    cyc = [("map", "odom"), ("odom", "base_link"), ("base_link", "lidar"), ("lidar", "map")]
    for k in range(150):
        rows.append((ns(k * 0.2), 4, _cdr_tf(cyc)))
    rows.append((ns(0.0), 5, _cdr_tf([("base_link", "imu_link"), ("world", "gps_link"), ("arm_base", "gripper")])))
    for ts, tid, data in rows:
        con.execute("INSERT INTO messages(topic_id, timestamp, data) VALUES (?,?,?)", (tid, ts, data))
    con.commit()
    con.close()


def _mc_rec(op, body):
    return bytes([op]) + struct.pack("<Q", len(body)) + body


def _mc_str(s):
    e = s.encode()
    return struct.pack("<I", len(e)) + e


def _mc_map(d):
    body = b"".join(_mc_str(k) + _mc_str(v) for k, v in d.items())
    return struct.pack("<I", len(body)) + body


def _sc_mcap(path):
    msgs = b"".join(_mc_rec(0x05, struct.pack("<HIQQ", 1, i, int((SC_T0 + i) * 1e9), int((SC_T0 + i) * 1e9)) + _cdr_stamped(SC_T0 + i, 0)) for i in range(20))
    chunk = struct.pack("<QQQI", int(SC_T0 * 1e9), int((SC_T0 + 19) * 1e9), len(msgs), 0) + _mc_str("") + struct.pack("<Q", len(msgs)) + msgs
    body = MCAP_MAGIC + _mc_rec(0x01, _mc_str("ros2") + _mc_str("rti-selfcheck"))
    body += _mc_rec(0x03, struct.pack("<H", 1) + _mc_str("sensor_msgs/msg/BatteryState") + _mc_str("ros2msg") + struct.pack("<I", 0))
    body += _mc_rec(0x04, struct.pack("<HH", 1, 1) + _mc_str("/battery_state") + _mc_str("cdr") + _mc_map({"offered_qos_profiles": "- reliability: 1\n  durability: 2\n"}))
    body += _mc_rec(0x06, chunk)
    body += _mc_rec(0x0C, _mc_str("network") + _mc_map({"ssid": "robot-lab", "wifi_password": "correct-horse-battery"}))
    body += _mc_rec(0x0F, struct.pack("<I", 0))
    summary = _mc_rec(0x0B, struct.pack("<QHIIIIQQ", 20, 1, 1, 0, 1, 1, int(SC_T0 * 1e9), int((SC_T0 + 19) * 1e9)) + struct.pack("<I", 10) + struct.pack("<HQ", 1, 20))
    start = len(body)
    body += summary + _mc_rec(0x02, struct.pack("<QQI", start, 0, 0)) + MCAP_MAGIC
    with open(path, "wb") as fh:
        fh.write(body)


def _ul(t, body):
    return struct.pack("<HB", len(body), ord(t)) + body


def _sc_ulg(path):
    out = ULOG_MAGIC + b"\x01" + struct.pack("<Q", 0) + _ul("B", b"\x00" * 40)
    key = b"char[3] sys_name"
    out += _ul("I", bytes([len(key)]) + key + b"PX4")
    fmts = ["estimator_status:uint64_t timestamp;float vel_test_ratio;float pos_test_ratio;float hgt_test_ratio;float mag_test_ratio;float[3] vibe;",
            "vehicle_gps_position:uint64_t timestamp;uint8_t fix_type;uint8_t satellites_used;float hdop;",
            "esc_report:uint64_t timestamp;uint32_t esc_errorcount;int32_t esc_rpm;float esc_current;float esc_temperature;",
            "esc_status:uint64_t timestamp;uint8_t esc_count;esc_report[4] esc;", "vehicle_status:uint64_t timestamp;bool failsafe;"]
    for f in fmts:
        out += _ul("F", f.encode())
    for mid, name in enumerate(("estimator_status", "vehicle_gps_position", "esc_status", "vehicle_status")):
        out += _ul("A", struct.pack("<BH", 0, mid) + name.encode())
    for i, (vel, vibe) in enumerate(((0.35, 12.0), (0.82, 22.0), (1.12, 35.0))):
        out += _ul("D", struct.pack("<H", 0) + struct.pack("<Qffff3f", i * 1000, vel, 0.4, 0.3, 0.2, vibe, vibe * 0.8, vibe * 0.5))
    out += _ul("D", struct.pack("<H", 1) + struct.pack("<QBBf", 0, 3, 5, 2.7))
    esc = b"".join(struct.pack("<QIiff", 0, 3 if i == 1 else 0, 5200 + (1400 if i == 3 else 0), 18.0, 108.0 if i == 1 else 62.0) for i in range(4))
    out += _ul("D", struct.pack("<H", 2) + struct.pack("<QB", 0, 4) + esc)
    out += _ul("D", struct.pack("<H", 3) + struct.pack("<Q?", 0, True))
    out += _ul("L", struct.pack("<BQ", 4, 0) + b"Preflight Fail: Accel inconsistent")
    with open(path, "wb") as fh:
        fh.write(out)


JOINTS = [(s + "_" + j) for s in ("l", "r") for j in ("hip_yaw", "hip_roll", "hip_pitch", "knee_pitch", "ankle_pitch", "ankle_roll")]
ACT_COLS = ("joint_name", "actuator_id", "stator_temp_c", "max_temp_c", "rms_torque_nm", "peak_torque_nm", "rated_torque_nm", "limit_torque_nm",
            "velocity_rad_s", "limit_velocity_rad_s", "duty_cycle_pct", "operating_hours", "service_interval_hrs", "backlash_arcmin",
            "max_backlash_arcmin", "harmonic_cycles", "error_flags", "position_rad")
BMS_COLS = ("pack_id", "chemistry", "series_cells", "pack_voltage_v", "current_a", "capacity_ah", "max_c_rate", "cell_min_mv", "cell_max_mv",
            "cell_voltages_mv", "load_state", "mosfet_temp_c", "pack_temp_c", "soh_pct", "cycle_count", "balance_active")


def build_selfcheck(td, args):
    """Write the multi-sensor fixtures (12-DOF humanoid, 16S LiFePO4 pack, ROS 2 nav bag, PX4 quad, CAN bus) into td and point args at them."""
    p = lambda n: os.path.join(td, n)  # noqa: E731
    base = {"stator_temp_c": 55, "max_temp_c": 90, "rms_torque_nm": 20, "peak_torque_nm": 60, "rated_torque_nm": 40, "limit_torque_nm": 150, "velocity_rad_s": 3.0,
            "limit_velocity_rad_s": 8.0, "duty_cycle_pct": 40, "operating_hours": 1000, "service_interval_hrs": 8000, "backlash_arcmin": 1.0,
            "max_backlash_arcmin": 3.0, "harmonic_cycles": 2_000_000, "error_flags": "0", "position_rad": 0.1}
    over = {"l_knee_pitch": {"stator_temp_c": 104, "peak_torque_nm": 135, "duty_cycle_pct": 88, "backlash_arcmin": 3.4},
            "r_knee_pitch": {"stator_temp_c": 93, "max_temp_c": 100, "rms_torque_nm": 45},
            "l_hip_roll": {"velocity_rad_s": 7.2, "position_rad": 0.9},
            "r_ankle_pitch": {"operating_hours": 7300, "duty_cycle_pct": 74}, "r_hip_yaw": {"operating_hours": 8100}}
    with open(p("humanoid-actuators.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ACT_COLS)
        for j in JOINTS:
            d = dict(base, joint_name=j, actuator_id="CAN-%02d" % (JOINTS.index(j) + 1))
            d.update(over.get(j, {}))
            w.writerow([d.get(c, "") for c in ACT_COLS])
    urdf = ['<?xml version="1.0"?>', '<robot name="humanoid12">', '  <link name="pelvis"/>']
    for j in JOINTS:
        urdf += ['  <link name="%s_link"/>' % j, '  <joint name="%s" type="revolute"><parent link="pelvis"/><child link="%s_link"/>' % (j, j),
                 '    <limit lower="-0.6" upper="0.5" effort="120" velocity="6.0"/></joint>']
    urdf.append("</robot>")
    with open(p("humanoid.urdf"), "w") as fh:
        fh.write("\n".join(urdf) + "\n")
    cells = [3220 + 2 * i for i in range(15)] + [2440]
    with open(p("bms.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(BMS_COLS)
        w.writerow(["HUM-PACK-A", "LiFePO4", 16, 51.2, 160, 100, 1.5, 2440, 3248, ";".join(str(c) for c in cells), "load", 78, 41, 78, 3250, "true"])
        w.writerow(["AUX-PACK-B", "LiPo", 6, 23.1, 4, 10, 5, 3830, 3840, "", "rest", 31, 28, 97, 40, "false"])
    can = []
    for k in range(51):
        t = SC_T0 + k * 0.1
        if k <= 10:
            can.append("(%.6f) can0 701#05" % t)
        can.append("(%.6f) can0 702#05" % t)
        can.append("(%.6f) can0 181#0102030405060708" % t)
    can.append("(%.6f) can0 082#1023010000000000" % (SC_T0 + 2.0))
    for k in range(6):
        can.append("(%.6f) can0 20000088#0000040800000000" % (SC_T0 + 3.0 + k * 0.1))
    can.append("(%.6f) can0 20000040#0000000000000000" % (SC_T0 + 4.0))
    can.sort(key=lambda s: float(s[1:s.index(")")]))
    with open(p("humanoid-bus.log"), "w") as fh:
        fh.write("\n".join(can) + "\n")
    _sc_db3(p("nav.db3"))
    with open(p("nav-expectations.json"), "w") as fh:
        json.dump({"topics": [{"name": "/scan", "expected_hz": 10, "required": True, "qos": {"subscriber": {"reliability": "RELIABLE", "durability": "VOLATILE"}}},
                              {"name": "/imu/data", "expected_hz": 50}, {"name": "/joint_states", "expected_hz": 20},
                              {"name": "/cmd_vel", "expected_hz": 10, "required": True}]}, fh)
    _sc_mcap(p("fleet.mcap"))
    _sc_ulg(p("quad.ulg"))
    args.actuators = [p("humanoid-actuators.csv")]
    args.urdf = [p("humanoid.urdf")]
    args.bms = [p("bms.csv")]
    args.can = [p("humanoid-bus.log")]
    args.rosbag = [p("nav.db3"), p("nav-expectations.json"), p("fleet.mcap")]
    args.flight_log = [p("quad.ulg")]


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
    s = "" if s is None else visible(s, 400)
    return s.replace("|", "\\|") if md else s


def table_lines(headers, rows, md):
    rows = [[_cell(c, md) for c in r] for r in rows]
    if md:
        return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + ["| " + " | ".join(r) + " |" for r in rows]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = lambda r: ("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r))).rstrip()  # noqa: E731
    return [line(headers), line(["-" * x for x in w])] + [line(r) for r in rows]


def build_blocks(D):
    """Format-neutral report: list of (title, [block]) where block is ('p', text) | ('t', headers, rows) | ('f', findings)."""
    R, F = D["R"], D["findings"]
    inp = D["inputs"]
    secs = []
    c = _counts(F)
    secs.append(("Summary", [("p", "Inputs: %d actuator row(s), %d BMS pack(s), %d bag(s) / %d topic(s), %d flight record(s), %d CAN frame(s), %d URDF/SDF joint(s)"
                              % (inp["actuators"], inp["bms"], inp["bags"], inp["topics"], inp["flight_records"], inp["can_frames"], inp["joints"])),
                 ("t", ["critical", "high", "medium", "low", "info", "worst"], [[c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], _worst(F)]])]))
    b1 = []
    if R["actuators"]:
        b1.append(("t", ["joint", "stator C", "max C", "peak Nm", "rms Nm", "vel rad/s", "duty %", "hours", "backlash arcmin"],
                   [[a["joint_name"], fd(a["stator_temp_c"]), fd(a["max_temp_c"]), fd(a["peak_torque_nm"], 1), fd(a["rms_torque_nm"], 1), fd(a["velocity_rad_s"], 2),
                     fd(a["duty_cycle_pct"], 0), fd(a["operating_hours"], 0), fd(a["backlash_arcmin"], 2)] for a in R["actuators"][:60]]))
    if R["models"]:
        b1.append(("p", "Model: %s" % "; ".join("%s (%s, %d joints)" % (visible(m["name"], 40), m["format"].upper(), len(m["joints"])) for m in R["models"])))
    if R["can_summary"]:
        b1.append(("t", ["bus", "frames", "errors", "err %", "CRC", "stuff", "form", "peak load %", "EMCY", "HB timeouts"],
                   [[s["bus"], s["frames"], s["error_frames"], fd(s["error_pct"], 1), s["crc"], s["stuff"], s["form"],
                     (fd(s["peak_load_pct"], 0) + (" (est.)" if s["load_estimated"] else "")) if s["peak_load_pct"] is not None else "-", s["emergency_frames"],
                     s["heartbeat_timeouts"]] for s in R["can_summary"]]))
    secs.append((BUCKETS[0][0] + "|" + BUCKETS[0][1], b1 + [("f", D["by_bucket"]["1"])]))
    b2 = []
    if R["bms"]:
        b2.append(("t", ["pack", "chem", "cells", "V", "A", "min mV", "max mV", "delta mV", "state", "MOSFET C", "pack C", "SOH %", "cycles"],
                   [[b["pack_id"], "LiFePO4" if b["chemistry"] == "LFP" else "LiPo", fd(b["series_cells"], 0), fd(b["pack_voltage_v"], 1), fd(b["current_a"], 1),
                     fd(b["cell_min_mv"], 0), fd(b["cell_max_mv"], 0), fd(b["delta_mv"], 0), "load" if b["loaded"] else "rest", fd(b["mosfet_temp_c"]),
                     fd(b["pack_temp_c"]), fd(b["soh_pct"]), fd(b["cycle_count"], 0)] for b in R["bms"]]))
    secs.append((BUCKETS[1][0] + "|" + BUCKETS[1][1], b2 + [("f", D["by_bucket"]["2"])]))
    b3 = []
    for bag in R["bags"]:
        topics = [t for n, t in sorted(bag["topics"].items())]
        b3.append(("p", "Bag %s (%s): %d message(s), %s s, %d topic(s), %d tf edge(s)" % (visible(bag["name"], 40), bag["kind"], bag["messages"],
                                                                                           fd(bag["duration_s"], 1), len(topics), len(bag["tf"] | bag["tf_static"]))))
        b3.append(("t", ["topic", "type", "msgs", "rate Hz", "expected Hz", "max gap s", "offset ms"],
                   [[t["name"], t["type"].split("/")[-1], t["count"], fd(t["rate_hz"], 2), fd(t["expected_hz"], 1), fd(t["known_gap_s"], 2), fd(t["offset_ms"], 1)]
                    for t in topics[:60]]))
    secs.append((BUCKETS[2][0] + "|" + BUCKETS[2][1], b3 + [("f", D["by_bucket"]["3"])]))
    b4 = []
    s = R["flight_summary"]
    if s:
        b4.append(("t", ["records", "EKF v/p/h/m", "vib x/y/z m/s2", "clip", "GNSS fix/sats/hdop", "failsafe", "pre-arm"],
                   [[s["records"], "/".join(fd(s["ekf_" + k], 2) for k in ("vel", "pos", "hgt", "mag")), "/".join(fd(s["vib_" + a], 1) for a in "xyz"), fd(s["clip"], 0),
                     "%s/%s/%s" % (fd(s["fix_min"], 0), fd(s["sats_min"], 0), fd(s["hdop_max"], 2)), "yes" if s["failsafe"] else "no", visible(s["prearm"], 40) or "-"]]))
        if s["esc"]:
            b4.append(("t", ["ESC", "mean RPM", "max temp C", "desync", "max A"],
                       [[k, fd(e["mean_rpm"], 0), fd(e["temp"]), fd(e["desync"], 0), fd(e["current"], 1)] for k, e in s["esc"].items()]))
    b4.append(("f", D["by_bucket"]["4"]))
    if D["maintenance"]:
        b4.append(("p", "Preventive maintenance & flight-readiness checklist (%d row(s); full list with --maintenance-out)" % len(D["maintenance"])))
        b4.append(("t", ["prio", "category", "asset", "check", "status", "due"],
                   [[r[0], r[1], r[2], visible(r[3], 70), r[4], r[7]] for r in D["maintenance"][:40]]))
    secs.append((BUCKETS[3][0] + "|" + BUCKETS[3][1], b4))
    if D["by_bucket"]["other"]:
        secs.append(("Other", [("f", D["by_bucket"]["other"])]))
    if D["delta"] is not None:
        dl = D["delta"]
        if dl.get("skipped"):
            blocks = [("p", "skipped: %s" % dl["skipped"])]
        elif dl.get("baseline"):
            blocks = [("p", "baseline run: %d finding(s) recorded as NEW; no prior state" % len(dl["new"]))]
        else:
            blocks = []
            for label in ("events", "new", "changed", "resolved"):
                if dl[label]:
                    blocks.append(("p", label.upper()))
                    blocks.append(("f", dl[label]))
            blocks = blocks or [("p", "(no changes since baseline)")]
        secs.append(("Delta since baseline", blocks))
    if D["degraded"]:
        secs.append(("Degraded inputs", [("l", D["degraded"]), ("p", "DEGRADED: result may be incomplete; this is not a clean pass.")]))
    return secs


def render(D, fmt):
    md, secs = fmt == "markdown", build_blocks(D)
    esc = html.escape
    if fmt == "html":
        o = ["<!DOCTYPE html>", '<html lang="en"><head><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             "<title>Humanoid &amp; Fleet Telemetry Inspector Report</title>",
             "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}table{border-collapse:collapse;margin:.5rem 0}"
             "td,th{border:1px solid #bbb;padding:2px 8px;font-size:.85rem;text-align:left}th{background:#eee}.CRITICAL{color:#b00020;font-weight:700}"
             ".HIGH{color:#d35400;font-weight:700}.MEDIUM{color:#9a7d0a}.LOW{color:#2c7a4b}.INFO{color:#555}pre{background:#f5f5f5;padding:.5rem}</style></head><body>",
             "<h1>Humanoid &amp; Fleet Telemetry Inspector Report</h1>", "<pre>%s</pre>" % esc(D["header"])]
        for title, blocks in secs:
            num_, _, name = title.partition("|")
            o.append("<h2>%s</h2>" % esc("Bucket %s - %s" % (num_, name) if name else title))
            for b in blocks:
                if b[0] == "p":
                    o.append("<p>%s</p>" % esc(b[1]))
                elif b[0] == "t":
                    o.append("<table><tr>%s</tr>%s</table>" % ("".join("<th>%s</th>" % esc(h) for h in b[1]),
                                                                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(visible(c, 400)) for c in r) for r in b[2])))
                elif b[0] == "f":
                    o.append("<ul>%s</ul>" % ("".join('<li><span class="%s">%s</span> <code>%s</code> %s</li>' % (f.sev, f.sev, esc(f.code), esc(f.msg)) for f in bc.sort_findings(b[1]))
                                              or "<li>(no findings)</li>"))
                elif b[0] == "l":
                    o.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(x) for x in b[1]))
        o += ["<p>exit_code %d (fail-on=%s)</p>" % (D["code"], esc(D["fail_on"])), "<hr><p>%s</p>" % esc(SCOPE_NOTE), "</body></html>"]
        return "\n".join(o)
    L = []
    if md:
        L += ["# Humanoid & Fleet Telemetry Inspector Report", "", "```", D["header"], "```", ""]
    else:
        L += [D["header"]]
    for title, blocks in secs:
        num_, _, name = title.partition("|")
        if name:
            title = "Bucket %s - %s" % (num_, name)
        L += (["", "## " + title, ""] if md else ["", "== " + title + " =="])
        for b in blocks:
            if b[0] == "p":
                L += [b[1]] + ([""] if md else [])
            elif b[0] == "t":
                L += table_lines(b[1], b[2], md) + [""]
            elif b[0] == "f":
                fs = bc.sort_findings(b[1])
                if not fs:
                    L += ["- (no findings)" if md else "  (no findings)"]
                else:
                    L += [("- **%s** `%s` %s" % (f.sev, f.code, _cell(f.msg, True))) if md else "  [%-8s] %s: %s" % (f.sev, f.code, f.msg) for f in fs]
                L += [""] if md else []
            elif b[0] == "l":
                L += [("- " if md else "  ") + x for x in b[1]] + [""]
    L += ["", "exit_code %d (fail-on=%s)" % (D["code"], D["fail_on"])]
    L += ["", "---", "", SCOPE_NOTE, ""] if md else ["", SCOPE_NOTE]
    return "\n".join(L)


# ---------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = _Parser(prog="robotics_telemetry_inspector.py", allow_abbrev=False,
                description="Read-only, offline humanoid & fleet telemetry inspector (actuators, BMS, ROS 2 bags, PX4/ArduPilot, URDF/SDF, CAN)")
    p.add_argument("paths", nargs="*", metavar="PATH", help="telemetry files, archives (.zip) or directories (role auto-detected)")
    p.add_argument("--dir", action="append", default=[], metavar="DIR")
    p.add_argument("--actuators", action="append", default=[], metavar="PATH")
    p.add_argument("--bms", action="append", default=[], metavar="PATH")
    p.add_argument("--rosbag", action="append", default=[], metavar="PATH")
    p.add_argument("--flight-log", dest="flight_log", action="append", default=[], metavar="PATH")
    p.add_argument("--urdf", action="append", default=[], metavar="PATH")
    p.add_argument("--can-log", dest="can", action="append", default=[], metavar="PATH")
    p.add_argument("--maintenance-out", metavar="PATH")
    p.add_argument("--self-check", dest="self_check", action="store_true")
    p.add_argument("--state", metavar="PATH")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--now", metavar="ISO", help="override the report clock (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)")
    p.add_argument("--format", type=str.lower, choices=["text", "markdown", "md", "json", "html"], default="text")
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


def check_target(path, flag, protected, csv_only=False):
    if os.path.splitext(path)[1].lower() in TELEMETRY_EXTS:
        raise UsageError("%s refuses to write a telemetry/model path (%s): input formats are never written" % (flag, visible(path, 80)))
    if csv_only and not path.lower().endswith(".csv"):
        raise UsageError("%s must end in .csv" % flag)
    if os.path.isdir(path):
        raise UsageError("%s %s is a directory" % (flag, visible(path, 80)))
    if os.path.islink(path):
        raise UsageError("%s %s is a symlink; refusing to write through it" % (flag, visible(path, 80)))
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
    fd_, tmp = tempfile.mkstemp(dir=d, prefix=".rti-", suffix=".tmp")
    try:
        with os.fdopen(fd_, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def by_bucket_split(findings):
    out = {k: [f for f in findings if f.code in codes] for k, _, codes in BUCKETS}
    known = set().union(*(c for _, _, c in BUCKETS))
    out["other"] = [f for f in findings if f.code not in known]
    return out


def run(argv, out=None, err=None, now=None):
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(argv)
    hit = scan_argv(argv)
    if hit:
        print("error: refusing to run: argument %d looks like a %s; credentials, tokens, signing keys, DDS private keys, Wi-Fi passwords "
              "and SSH keys are never accepted (value not echoed)" % hit, file=err)
        return bc.EXIT_USAGE
    try:
        args = build_parser().parse_args(argv)
        if args.delta and not args.state:
            raise UsageError("--delta requires --state PATH")
        clock = now or (_parse_now(args.now) if args.now else None)
        if args.now and clock is None:
            raise UsageError("--now must be an ISO date or timestamp")
        if args.self_check:
            if args.paths or args.dir or any(getattr(args, k) for k in ("actuators", "bms", "rosbag", "flight_log", "urdf", "can")):
                raise UsageError("--self-check uses built-in fixtures and cannot be combined with input paths")
            clock = clock or _parse_now("2026-10-27T00:00:00Z")
            with tempfile.TemporaryDirectory(prefix="rti-selfcheck-") as td:
                os.chmod(td, 0o700)
                build_selfcheck(td, args)
                return _execute(args, out, err, clock or bc.utc_now(), selfcheck=True)
        return _execute(args, out, err, clock or bc.utc_now())
    except UsageError as e:
        print("error: %s" % e, file=err)
        return bc.EXIT_USAGE
    except SystemExit as e:  # --help
        return int(e.code or 0)


def _execute(args, out, err, clock, selfcheck=False):
    degraded, violations = [], []
    srcs = collect_sources(args, degraded, violations)
    protected = {os.path.realpath(s["path"]) for s in srcs if s["path"]}
    for p in args.paths + args.dir + args.actuators + args.bms + args.rosbag + args.flight_log + args.urdf + args.can:
        protected.add(os.path.realpath(p))
    outs = [("--state", args.state, False), ("--maintenance-out", args.maintenance_out, True), ("--out", args.out, False)]
    if not selfcheck:
        for flag, path, csv_only in outs:
            if path:
                others = {os.path.realpath(p) for f2, p, _ in outs if p and f2 != flag}
                check_target(path, flag, protected | others, csv_only)
    elif args.maintenance_out and not args.maintenance_out.lower().endswith(".csv"):
        raise UsageError("--maintenance-out must end in .csv")
    R = audit(srcs, degraded, violations)
    if not srcs and not violations:
        degraded.append("no inputs provided; nothing to audit" if not degraded else "no valid inputs could be read")
    elif not (R["actuators"] or R["bms"] or R["bags"] or R["flights"] or R["models"] or R["can_frames"]) and not violations:
        degraded.append("no valid inputs could be read")
    findings = R["findings"]
    delta, events = None, []
    if args.state:
        store = bc.StateStore(args.state)
        prev = store.get(STATE_KEY)
        cur = make_state(R)
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
                print("error: cannot write --state: %s" % visible(e, 100), file=err)
                return bc.EXIT_USAGE
    code = compute_exit(findings + events, bool(degraded), args.fail_on)
    maint = maintenance_rows(R, findings)
    topics = sum(len(b["topics"]) for b in R["bags"])
    inputs = {"actuators": len(R["actuators"]), "bms": len(R["bms"]), "bags": len(R["bags"]), "topics": topics,
              "flight_records": sum(len(f["rows"]) for f in R["flights"]), "can_frames": len(R["can_frames"]), "joints": len(R["joints"])}
    header = bc.snapshot_header(TOOL, ["local-files"], None, now=clock)
    fmt = "json" if args.json else ("markdown" if args.format == "md" else args.format)
    D = {"header": header, "R": R, "inputs": inputs, "findings": findings, "by_bucket": by_bucket_split(findings), "maintenance": maint, "delta": delta,
         "degraded": degraded, "code": code, "fail_on": args.fail_on}
    if fmt == "json":
        c = _counts(findings)
        payload = {"tool": TOOL, "version": VERSION, "retrieved_at": clock.strftime("%Y-%m-%dT%H:%M:%SZ"), "inputs": inputs,
                   "actuators": [{k: v for k, v in a.items()} for a in R["actuators"]],
                   "bms": [{k: v for k, v in b.items() if k != "cells"} for b in R["bms"]],
                   "bags": [{"name": b["name"], "kind": b["kind"], "messages": b["messages"], "duration_s": b["duration_s"], "tf_edges": len(b["tf"] | b["tf_static"]),
                             "topics": [{k: t[k] for k in ("name", "type", "count", "rate_hz", "expected_hz", "required", "known_gap_s", "offset_ms", "drift_ms", "ts_back", "stamp_back")}
                                        for _, t in sorted(b["topics"].items())]} for b in R["bags"]],
                   "flight": R["flight_summary"], "can": R["can_summary"],
                   "urdf": [{"name": m["name"], "format": m["format"], "joints": sorted(m["joints"])} for m in R["models"]],
                   "maintenance": [dict(zip(MAINT_COLUMNS, r)) for r in maint],
                   "summary": {"counts": c, "worst": _worst(findings), "events": [f.to_dict() for f in events]},
                   "findings": [f.to_dict() for f in bc.sort_findings(findings)],
                   "delta": None if delta is None else {k: ([f.to_dict() for f in v] if isinstance(v, list) else v) for k, v in delta.items()},
                   "exit_code": code, "fail_on": args.fail_on, "degraded_sources": degraded, "scope_note": SCOPE_NOTE}
        text = json.dumps(clean(payload), indent=2, sort_keys=True, ensure_ascii=False, default=str)
    else:
        text = render(D, fmt)
    if selfcheck:
        missing = sorted(SELFCHECK_EXPECTED - {f.code for f in findings})
        if missing:
            print("SELF_CHECK_FAILED: expected finding code(s) missing: %s" % ", ".join(missing), file=err)
            return bc.EXIT_DEGRADED
    try:
        if args.maintenance_out:
            write_atomic(args.maintenance_out, maint_csv_text(maint))
            print("wrote %s (%d checklist row(s))" % (args.maintenance_out, len(maint)), file=err)
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
