#!/usr/bin/env python3
"""Shared helpers for the Wave 2 bots (stdlib only).

* http_json      - urllib client with retry/backoff, 429/5xx handling, per-host throttle
* Finding / SEV  - uniform severity ladder INFO < LOW < MEDIUM < HIGH < CRITICAL
* render_text / render_json / snapshot_header
* exit_code      - shared exit-code contract
* StateStore / diff_findings - schema-versioned atomic state + NEW / CHANGED / RESOLVED delta
"""
import datetime
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BotCommon/1.0"
STATE_SCHEMA = 1

SEV = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
SEV_NAMES = {v: k for k, v in SEV.items()}

# Exit-code contract: 0 clean, 1 MEDIUM, 2 HIGH, 3 CRITICAL, 10 bad input, 11 degraded data.
EXIT_OK, EXIT_MEDIUM, EXIT_HIGH, EXIT_CRITICAL, EXIT_USAGE, EXIT_DEGRADED = 0, 1, 2, 3, 10, 11
_SEV_EXIT = {"MEDIUM": EXIT_MEDIUM, "HIGH": EXIT_HIGH, "CRITICAL": EXIT_CRITICAL}


# ---------------------------------------------------------------- time
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def ts_str(ms):
    """Epoch milliseconds -> 'YYYY-MM-DD HH:MM:SS UTC' ('none' when falsy)."""
    if not ms:
        return "none"
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------- HTTP
class HttpResult:
    """status -1 means a transport failure (see .error). .data is parsed JSON, or {'_raw': ...}."""

    def __init__(self, status, data=None, error=None, headers=None, attempts=1, raw=b""):
        self.raw = raw  # response bytes (capped at MAX_RAW), for hash verification
        self.status = status
        self.data = data if data is not None else {}
        self.error = error
        self.headers = headers or {}
        self.attempts = attempts

    @property
    def ok(self):
        return 200 <= self.status < 300

    def __iter__(self):  # allows: status, data = http_json(...)
        yield self.status
        yield self.data


MAX_RAW = 2_000_000
_urlopen = urllib.request.urlopen  # tests monkeypatch this
_sleep = time.sleep
_last_call = {}
MIN_INTERVAL = {}  # host -> seconds; set via set_min_interval()


def set_min_interval(host, seconds):
    MIN_INTERVAL[host] = float(seconds)


def _throttle(url):
    host = urllib.parse.urlparse(url).netloc
    gap = MIN_INTERVAL.get(host, 0)
    if gap:
        wait = _last_call.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            _sleep(wait)
        _last_call[host] = time.monotonic()


def _parse(raw):
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text[:300]}


def _retry_after(headers):
    try:
        return max(0.0, min(float(headers.get("Retry-After", "")), 30.0))
    except (TypeError, ValueError):
        return None


def http_json(url, body=None, headers=None, retries=4, backoff=0.5, timeout=20, method=None):
    """GET (or POST when body is given) JSON. Retries transport errors, 429 and 5xx with exponential backoff.

    Other 4xx responses are returned immediately (404 is data, not an error to retry).
    Never raises; inspect .status / .error.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last = HttpResult(-1, error="no attempt")
    for attempt in range(1, retries + 2):
        _throttle(url)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        retry_after = None
        try:
            with _urlopen(req, timeout=timeout) as r:
                raw = r.read()[:MAX_RAW]
                last = HttpResult(r.status, _parse(raw), headers=dict(r.headers.items()), attempts=attempt, raw=raw)
                return last
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            hd = dict(e.headers.items()) if e.headers else {}
            last = HttpResult(e.code, _parse(raw), error="HTTP %d" % e.code, headers=hd, attempts=attempt)
            if e.code != 429 and e.code < 500:
                return last
            retry_after = _retry_after(hd)
        except Exception as e:  # URLError, timeouts, resets
            last = HttpResult(-1, {"_error": str(e)}, error=str(e), attempts=attempt)
        if attempt <= retries:
            _sleep(retry_after if retry_after is not None else backoff * (2 ** (attempt - 1)))
    return last


# ---------------------------------------------------------------- findings
class Finding:
    def __init__(self, sev, code, msg, evidence=None, key=None):
        if sev not in SEV:
            raise ValueError("bad severity %r" % (sev,))
        self.sev = sev
        self.code = code
        self.msg = msg
        self.evidence = evidence or {}
        self.key = key or code  # stable identity used by delta mode

    def to_dict(self):
        return {"sev": self.sev, "code": self.code, "key": self.key, "msg": self.msg, "evidence": self.evidence}

    @classmethod
    def from_dict(cls, d):
        return cls(d["sev"], d["code"], d.get("msg", ""), d.get("evidence"), d.get("key"))

    def __repr__(self):
        return "Finding(%s %s)" % (self.sev, self.key)


def sort_findings(findings):
    return sorted(findings, key=lambda f: (-SEV[f.sev], f.code, f.key))


def highest(findings):
    return max((SEV[f.sev] for f in findings), default=-1)


def exit_code(findings, degraded=False, fail_on="medium"):
    """Highest severity at or above fail_on maps to 1/2/3.

    A degraded run with nothing at/above the threshold returns 11 so it is never mistaken for a clean pass.
    """
    threshold = SEV[fail_on.upper()]
    worst = max((f for f in findings), key=lambda f: SEV[f.sev], default=None)
    if worst is not None and SEV[worst.sev] >= threshold and worst.sev in _SEV_EXIT:
        return _SEV_EXIT[worst.sev]
    return EXIT_DEGRADED if degraded else EXIT_OK


# ---------------------------------------------------------------- rendering
def snapshot_header(tool, endpoints=None, fallbacks=None, now=None):
    now = now or utc_now()
    lines = ["snapshot   %s | tool=%s" % (now.strftime("%Y-%m-%d %H:%M:%S UTC"), tool)]
    if endpoints:
        lines.append("endpoints  " + ", ".join(sorted(endpoints)))
    if fallbacks:
        lines.append("fallbacks  " + ", ".join(fallbacks))
    return "\n".join(lines)


def render_text(findings, header="", footer=""):
    out = [header] if header else []
    for f in sort_findings(findings):
        out.append("  [%-8s] %s: %s" % (f.sev, f.code, f.msg))
    if not findings:
        out.append("  (no findings)")
    if footer:
        out.append(footer)
    return "\n".join(out)


def render_json(findings, meta=None, degraded=False):
    payload = {"meta": meta or {}, "degraded": bool(degraded),
               "findings": [f.to_dict() for f in sort_findings(findings)]}
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- state + delta
class StateStore:
    """JSON state file keyed by namespace (e.g. mint). Atomic write, schema versioned."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {"schema": STATE_SCHEMA, "items": {}}
        if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA or not isinstance(doc.get("items"), dict):
            return {"schema": STATE_SCHEMA, "items": {}}
        return doc

    def get(self, name):
        return self.load()["items"].get(name)

    def put(self, name, snapshot):
        doc = self.load()
        doc["items"][name] = snapshot
        doc["updated"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write(doc)

    def _write(self, doc):
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def finding_fingerprint(f):
    """What counts as 'changed' for a finding with the same key: severity + message."""
    return {"sev": f.sev, "msg": f.msg}


def diff_findings(old, new):
    """old/new: iterables of Finding or dict. Returns {'NEW': [...], 'CHANGED': [...], 'RESOLVED': [...]}.

    old=None means no baseline: everything is NEW (full baseline run).
    """
    def norm(items):
        return {f.key: f for f in (Finding.from_dict(i) if isinstance(i, dict) else i for i in items)}

    new_m = norm(new)
    old_m = norm(old) if old is not None else {}
    res = {"NEW": [], "CHANGED": [], "RESOLVED": []}
    for k in sorted(new_m):
        if k not in old_m:
            res["NEW"].append(new_m[k])
        elif finding_fingerprint(old_m[k]) != finding_fingerprint(new_m[k]):
            res["CHANGED"].append(new_m[k])
    for k in sorted(old_m):
        if k not in new_m:
            res["RESOLVED"].append(old_m[k])
    return res
