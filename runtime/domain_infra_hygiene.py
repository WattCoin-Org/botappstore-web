#!/usr/bin/env python3
"""Domain, TLS & Email Auth Infrastructure Hygiene Auditor (strictly read-only, stdlib + bot_common).

Audits one or more domains the user owns or is authorised to review using ordinary public lookups only:
DNS-over-HTTPS (Cloudflare primary, Google fallback) for A/AAAA/CNAME/NS/MX/CAA/TXT/DNSKEY + DNSSEC `AD`,
SPF (recursive RFC 7208 lookup counting), DMARC, DKIM selectors (RSA key size), MTA-STS, TLS-RPT, CAA,
dangling-CNAME takeover fingerprints on the domain's own records, one TLS handshake on :443, one HTTPS GET for
security headers, one plain-HTTP GET on :80 for the HTTPS redirect, and Certificate Transparency (Cert Spotter,
crt.sh fallback).  It prints copy-paste DNS records and header snippets; it never changes anything.
Exit codes: 0 clean, 1 MEDIUM (or LOW/INFO with --fail-on low/info), 2 HIGH, 3 CRITICAL, 10 bad input,
11 degraded data sources.

Offline fixture (JSON, --offline): {"now": "2026-10-27T00:00:00Z" (optional), "doh_down": false,
"dns": {name: {TYPE: <DoH JSON> | [data, ...] | "FAIL"}}, "tls": {domain: <tls result> | {"error": ...}},
"https": {domain: {"status": 200, "headers": {...}} | {"error": ...}},
"http80": {domain: {"status": 301, "headers": {"location": ...}} | {"error": ...}},
"ct": {domain: {"certspotter": [...] | "FAIL", "crtsh": [...] | "FAIL"}}}.
DNS names missing from the fixture answer NXDOMAIN; a present name with a missing type answers NOERROR/NODATA.
"""
import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import types
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))


def _make_fallback_bc():
    """Minimal inline stand-in for lib/bot_common.py so the script also works standalone."""
    import time

    m = types.SimpleNamespace()
    m.USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BotCommon/1.0"
    m.SEV = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    m.EXIT_OK, m.EXIT_MEDIUM, m.EXIT_HIGH, m.EXIT_CRITICAL, m.EXIT_USAGE, m.EXIT_DEGRADED = 0, 1, 2, 3, 10, 11
    sev_exit = {"MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    def utc_now():
        return datetime.datetime.now(datetime.timezone.utc)

    class HttpResult:
        def __init__(self, status, data=None, error=None, headers=None, attempts=1, raw=b""):
            self.status, self.data, self.error = status, (data if data is not None else {}), error
            self.headers, self.attempts, self.raw = headers or {}, attempts, raw

        @property
        def ok(self):
            return 200 <= self.status < 300

    def _parse(raw):
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            return json.loads(text)
        except ValueError:
            return {"_raw": text[:300]}

    def http_json(url, body=None, headers=None, retries=4, backoff=0.5, timeout=20, method=None):
        hdrs = {"User-Agent": m.USER_AGENT, "Accept": "application/json"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last = HttpResult(-1, error="no attempt")
        for attempt in range(1, retries + 2):
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()[:2_000_000]
                    return HttpResult(r.status, _parse(raw), headers=dict(r.headers.items()), attempts=attempt, raw=raw)
            except urllib.error.HTTPError as e:
                raw = e.read() if e.fp else b""
                last = HttpResult(e.code, _parse(raw), error="HTTP %d" % e.code, attempts=attempt)
                if e.code != 429 and e.code < 500:
                    return last
            except Exception as e:  # noqa: BLE001 - URLError, timeouts, resets
                last = HttpResult(-1, {"_error": str(e)}, error=str(e), attempts=attempt)
            if attempt <= retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
        return last

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

    m.utc_now, m.HttpResult, m.http_json, m.Finding = utc_now, HttpResult, http_json, Finding
    m.sort_findings, m.exit_code, m.snapshot_header, m.StateStore = sort_findings, exit_code, snapshot_header, StateStore
    return m


try:
    import bot_common as bc  # noqa: E402
except ImportError:
    bc = _make_fallback_bc()

TOOL = "domain-infra-hygiene"
VERSION = "1.0.0"
SCOPE_NOTE = ("SCOPE_NOTE: Passive, read-only public posture inspection for domains the user owns or is authorised to "
              "assess: DNS-over-HTTPS lookups (Cloudflare, Google fallback), one TLS handshake on :443, one HTTPS GET "
              "for response headers, one plain-HTTP GET on :80 for the redirect check, and public Certificate "
              "Transparency logs (Cert Spotter, crt.sh fallback). No port scanning, brute-forcing, fuzzing, subdomain "
              "enumeration beyond public CT logs, mail-server or credential probing, and no exploitation; the "
              "dangling-CNAME check only inspects the audited domain's own records against known provider "
              "fingerprints and never claims or registers anything. Results are point-in-time: DNS caches, CT log "
              "lag, CDN edge variation and resolver behaviour can differ by location and minute; DKIM presence is "
              "only confirmed for the selectors tried; an empty crt.sh answer is INCONCLUSIVE, not clean. The "
              "HTTP->HTTPS redirect is reported UNVERIFIED when outbound port 80 is blocked (common in sandboxes "
              "and corporate networks). Remediation records are templates to review before publishing. Not a "
              "penetration test, compliance attestation, or legal advice. Refuses: scanning domains the user does "
              "not control, subdomain-takeover hunting against third parties, and credential or mail-server probing.")

DOH = [("cloudflare", "https://cloudflare-dns.com/dns-query"), ("google", "https://dns.google/resolve")]
CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
CRTSH = "https://crt.sh/"
RTYPE = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28, "DNSKEY": 48, "CAA": 257}
DEFAULT_SELECTORS = ["default", "google", "selector1", "selector2", "k1", "s1", "s2", "mail", "dkim"]
SPECIAL_SUFFIXES = ("localhost", "local", "internal", "localdomain", "invalid", "home.arpa")
MAX_DOMAINS = 50
SPF_LOOKUP_LIMIT = 10
SPF_MAX_HOPS = 15
SPF_HARD_CAP = 60
CT_MAX_PAGES = 3
HSTS_MIN_AGE = 15552000  # 180 days
LEGACY_TLS = ("SSLv2", "SSLv3", "TLSv1", "TLSv1.1")
WEAK_CIPHER_RE = re.compile(r"RC4|3DES|DES-CBC3|DES_CBC|NULL|EXPORT|EXP-|MD5|anon", re.I)
UA = bc.USER_AGENT

# Known dangling-CNAME / subdomain-takeover-prone provider suffixes (target -> provider).
TAKEOVER_FINGERPRINTS = [
    (r"\.github\.io$", "GitHub Pages"),
    (r"\.herokuapp\.com$|\.herokudns\.com$", "Heroku"),
    (r"\.s3\.amazonaws\.com$|\.s3[.-][a-z0-9-]+\.amazonaws\.com$", "AWS S3"),
    (r"\.s3-website[.-][a-z0-9-]+\.amazonaws\.com$", "AWS S3 website"),
    (r"\.elasticbeanstalk\.com$", "AWS Elastic Beanstalk"),
    (r"\.cloudfront\.net$", "AWS CloudFront"),
    (r"\.azurewebsites\.net$", "Azure App Service"),
    (r"\.cloudapp\.net$|\.cloudapp\.azure\.com$", "Azure Cloud Services / VM"),
    (r"\.trafficmanager\.net$", "Azure Traffic Manager"),
    (r"\.blob\.core\.windows\.net$", "Azure Blob Storage"),
    (r"\.azureedge\.net$", "Azure CDN"),
    (r"\.azure-api\.net$", "Azure API Management"),
    (r"\.azurecontainer\.io$", "Azure Container Instances"),
    (r"\.azurestaticapps\.net$", "Azure Static Web Apps"),
    (r"\.pantheonsite\.io$", "Pantheon"),
    (r"\.zendesk\.com$", "Zendesk"),
    (r"\.myshopify\.com$", "Shopify"),
    (r"\.surge\.sh$", "Surge.sh"),
    (r"\.ghost\.io$", "Ghost"),
    (r"\.fly\.dev$", "Fly.io"),
    (r"\.onrender\.com$", "Render"),
    (r"\.vercel\.app$", "Vercel"),
    (r"\.netlify\.app$|\.netlify\.com$", "Netlify"),
    (r"\.pages\.dev$", "Cloudflare Pages"),
    (r"\.fastly\.net$", "Fastly"),
    (r"\.readme\.io$", "ReadMe"),
    (r"\.statuspage\.io$", "Atlassian Statuspage"),
    (r"\.cargocollective\.com$", "Cargo Collective"),
    (r"\.webflow\.io$", "Webflow"),
    (r"\.unbouncepages\.com$", "Unbounce"),
    (r"\.helpjuice\.com$", "Helpjuice"),
    (r"\.helpscoutdocs\.com$", "Help Scout Docs"),
    (r"\.ngrok\.io$|\.ngrok\.app$", "ngrok"),
    (r"\.wordpress\.com$", "WordPress.com"),
    (r"\.tumblr\.com$", "Tumblr"),
    (r"\.bitbucket\.io$", "Bitbucket"),
    (r"\.firebaseapp\.com$|\.web\.app$", "Firebase Hosting"),
    (r"\.freshdesk\.com$", "Freshdesk"),
    (r"\.uservoice\.com$", "UserVoice"),
    (r"\.gitbook\.io$", "GitBook"),
    (r"\.wpengine\.com$", "WP Engine"),
    (r"\.kinsta\.cloud$", "Kinsta"),
    (r"\.strikinglydns\.com$", "Strikingly"),
    (r"\.teamwork\.com$", "Teamwork"),
    (r"\.intercom\.help$", "Intercom"),
    (r"\.hs-sites\.com$|\.hubspot\.net$", "HubSpot"),
    (r"\.canny\.io$", "Canny"),
    (r"\.launchrock\.com$", "LaunchRock"),
]
_TAKEOVER_RE = [(re.compile(p), name) for p, name in TAKEOVER_FINGERPRINTS]

# Issuer organisation keyword -> CAA issuer-domain values that authorise it.
CA_ISSUER_CAA = [
    ("let's encrypt", {"letsencrypt.org"}),
    ("lets encrypt", {"letsencrypt.org"}),
    ("google trust services", {"pki.goog", "google.com"}),
    ("zerossl", {"zerossl.com", "sectigo.com"}),
    ("digicert", {"digicert.com", "symantec.com", "geotrust.com", "rapidssl.com", "thawte.com", "digitalcertvalidation.com"}),
    ("geotrust", {"digicert.com", "geotrust.com"}),
    ("rapidssl", {"digicert.com", "rapidssl.com"}),
    ("thawte", {"digicert.com", "thawte.com"}),
    ("sectigo", {"sectigo.com", "comodoca.com", "comodo.com", "usertrust.com", "trust-provider.com"}),
    ("comodo", {"sectigo.com", "comodoca.com", "comodo.com", "usertrust.com"}),
    ("usertrust", {"sectigo.com", "usertrust.com", "comodoca.com"}),
    ("amazon", {"amazon.com", "amazontrust.com", "awstrust.com", "amazonaws.com"}),
    ("cloudflare", {"cloudflare.com", "digicert.com", "pki.goog", "letsencrypt.org", "sectigo.com", "ssl.com"}),
    ("globalsign", {"globalsign.com"}),
    ("entrust", {"entrust.net", "affirmtrust.com"}),
    ("godaddy", {"godaddy.com", "starfieldtech.com"}),
    ("starfield", {"godaddy.com", "starfieldtech.com"}),
    ("ssl.com", {"ssl.com"}),
    ("buypass", {"buypass.com", "buypass.no"}),
    ("microsoft", {"microsoft.com"}),
    ("actalis", {"actalis.it"}),
    ("harica", {"harica.gr"}),
]

HEADER_CODES = ("HSTS_MISSING", "HSTS_SHORT_MAX_AGE", "HSTS_NO_INCLUDESUBDOMAINS", "CSP_MISSING", "XCTO_MISSING",
                "REFERRER_POLICY_MISSING", "FRAME_PROTECTION_MISSING", "PERMISSIONS_POLICY_MISSING")
EXPIRY_CODES = ("TLS_CERT_EXPIRED", "TLS_EXPIRING_CRITICAL", "TLS_EXPIRING_HIGH", "TLS_EXPIRING_SOON", "TLS_CERT_VALID")
TIME_VARYING = set(EXPIRY_CODES) | {"CT_SUMMARY"}
DMARC_RANK = {"none": 0, "quarantine": 1, "reject": 2}
REC_HSTS = "max-age=63072000; includeSubDomains; preload"
REC_CSP = "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; upgrade-insecure-requests"
REC_HEADERS = [
    ("Strict-Transport-Security", REC_HSTS),
    ("Content-Security-Policy", REC_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("X-Frame-Options", "DENY"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]


class UsageError(Exception):
    pass


class DomainError(ValueError):
    pass


# ---------------------------------------------------------------- domain normalisation
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(raw):
    """User input -> lowercase ASCII (IDNA) apex/host name. Raises DomainError on anything that is not a public DNS name."""
    s = (raw or "").strip()
    if not s:
        raise DomainError("empty domain")
    s = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):
        raise DomainError("IP literals are not domains")
    if s.count(":") == 1:
        s, port = s.split(":")
        if port and not port.isdigit():
            raise DomainError("invalid port")
    elif s.count(":") > 1:
        raise DomainError("IP literals are not domains")
    s = s.strip().rstrip(".").lower()
    if not s:
        raise DomainError("empty domain")
    try:
        ipaddress.ip_address(s)
        raise DomainError("IP literals are not domains")
    except ValueError as e:
        if isinstance(e, DomainError):
            raise
    try:
        s = s.encode("idna").decode("ascii")
    except UnicodeError:
        raise DomainError("invalid IDNA name") from None
    labels = s.split(".")
    if len(s) > 253 or any(not _LABEL_RE.match(lb) for lb in labels):
        raise DomainError("invalid DNS label")
    if len(labels) < 2:
        raise DomainError("single-label names are not public domains")
    if labels[-1].isdigit():
        raise DomainError("numeric TLD (IP-like) is not a domain")
    for suf in SPECIAL_SUFFIXES:
        if s == suf or s.endswith("." + suf):
            raise DomainError("non-public / special-use name (.%s)" % suf)
    return s


def split_tokens(values):
    out = []
    for v in values or []:
        out.extend(t.strip() for t in str(v).split(",") if t.strip())
    return out


def read_domains_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError("cannot read --domains-file: %s" % e) from None
    out = []
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()
        if ln:
            out.extend(ln.split())
    return out


# ---------------------------------------------------------------- DNS helpers
_TXT_SEG = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape(s):
    s = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\\(.)", r"\1", s)


def unquote_txt(data):
    """'"v=spf1 " "include:x ~all"' -> 'v=spf1 include:x ~all'. Unquoted data is returned unchanged."""
    data = (data or "").strip()
    segs = _TXT_SEG.findall(data)
    if segs and data.startswith('"'):
        return "".join(_unescape(x) for x in segs)
    return data


def parse_doh(data, rtype):
    """DoH JSON -> {status, ad, answers, cnames}. TXT answers are unquoted, names lowercased without the trailing dot."""
    tnum = RTYPE.get(rtype)
    ans = [a for a in (data.get("Answer") or []) if isinstance(a, dict)]
    cnames = [str(a.get("data", "")).rstrip(".").lower() for a in ans if a.get("type") == 5]
    vals = []
    for a in ans:
        if a.get("type") != tnum or rtype == "CNAME":
            continue
        d = str(a.get("data", ""))
        if rtype == "TXT":
            d = unquote_txt(d)
        elif rtype in ("NS",):
            d = d.rstrip(".").lower()
        vals.append(d)
    if rtype == "CNAME":
        vals = list(cnames)
    return {"status": int(data.get("Status", 2)), "ad": bool(data.get("AD")), "answers": vals, "cnames": cnames,
            "comment": data.get("Comment")}


def norm_mx(records):
    out = []
    for r in records or []:
        parts = str(r).split()
        if len(parts) >= 2:
            host = parts[1].rstrip(".").lower() or "."
            out.append("%s %s" % (parts[0], host))
        elif parts:
            out.append(parts[0])
    return sorted(out, key=lambda x: (int(x.split()[0]) if x.split()[0].isdigit() else 0, x))


def parse_caa(data):
    """'0 issue "letsencrypt.org"' or RFC 3597 '\\# 22 00 05 ...' -> {flags, tag, value, ca}."""
    d = str(data).strip()
    if d.startswith("\\#"):
        try:
            raw = bytes.fromhex("".join(d.split()[2:]))
            flags, tlen = raw[0], raw[1]
            tag = raw[2:2 + tlen].decode("ascii", "replace")
            value = raw[2 + tlen:].decode("utf-8", "replace")
        except (ValueError, IndexError):
            return {"flags": 0, "tag": "?", "value": d, "ca": ""}
    else:
        parts = d.split(None, 2)
        if len(parts) < 3:
            return {"flags": 0, "tag": "?", "value": d, "ca": ""}
        flags = int(parts[0]) if parts[0].isdigit() else 0
        tag, value = parts[1], parts[2].strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
    tag = tag.lower()
    ca = value.split(";", 1)[0].strip().lower() if tag in ("issue", "issuewild") else ""
    return {"flags": flags, "tag": tag, "value": value, "ca": ca}


def match_takeover_provider(target):
    t = (target or "").rstrip(".").lower()
    for rx, name in _TAKEOVER_RE:
        if rx.search(t):
            return name
    return None


# ---------------------------------------------------------------- SPF
def is_spf(txt):
    t = (txt or "").strip().lower()
    return t == "v=spf1" or t.startswith("v=spf1 ")


def spf_all_qualifier(record):
    for term in record.split()[1:]:
        tl = term.lower()
        if tl in ("all", "+all", "-all", "~all", "?all"):
            return "+" if tl in ("all", "+all") else tl[0]
    return None


def spf_redirect(record):
    for term in record.split()[1:]:
        if term.lower().startswith("redirect="):
            return term.split("=", 1)[1].rstrip(".").lower()
    return None


def spf_analyze(record, fetch_txt):
    """Recursive RFC 7208 §4.6.4 DNS-lookup counter.

    fetch_txt(name) -> list of TXT strings, or None when the lookup itself failed.
    Counts include, a, mx, ptr, exists and redirect (redirect is ignored when an `all` is present).
    """
    st = {"lookups": 0, "loop": None, "ptr": False, "missing": [], "failed": [], "depth_exceeded": False,
          "macros": [], "multiple": [], "tree": []}

    def walk(rec, stack):
        has_all = spf_all_qualifier(rec) is not None
        for term in rec.split()[1:]:
            if st["lookups"] > SPF_HARD_CAP:
                return
            mech = term.lower().lstrip("+-~?")
            if mech.startswith("include:"):
                st["lookups"] += 1
                follow(mech[8:], stack, "include")
            elif mech.startswith("redirect="):
                if has_all:
                    continue
                st["lookups"] += 1
                follow(mech[9:], stack, "redirect")
            elif mech == "a" or mech.startswith(("a:", "a/")):
                st["lookups"] += 1
            elif mech == "mx" or mech.startswith(("mx:", "mx/")):
                st["lookups"] += 1
            elif mech == "ptr" or mech.startswith(("ptr:", "ptr/")):
                st["lookups"] += 1
                st["ptr"] = True
            elif mech.startswith("exists:"):
                st["lookups"] += 1

    def follow(target, stack, kind):
        target = target.rstrip(".").lower()
        st["tree"].append({"from": stack[-1], "kind": kind, "to": target, "depth": len(stack)})
        if "%" in target:
            st["macros"].append(target)
            return
        if target in stack:
            if st["loop"] is None:
                st["loop"] = " -> ".join(stack + [target])
            return
        if len(stack) >= SPF_MAX_HOPS:
            st["depth_exceeded"] = True
            return
        recs = fetch_txt(target)
        if recs is None:
            st["failed"].append(target)
            return
        spfs = [r for r in recs if is_spf(r)]
        if not spfs:
            st["missing"].append(target)
            return
        if len(spfs) > 1:
            st["multiple"].append(target)
        walk(spfs[0], stack + [target])

    walk(record, ["@"])
    return st


# ---------------------------------------------------------------- DMARC / DKIM
def parse_tags(record):
    tags = {}
    for part in (record or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip().lower()
            if k and k not in tags:
                tags[k] = v.strip()
    return tags


def is_dmarc(txt):
    return bool(re.match(r"^\s*v\s*=\s*DMARC1\s*(;|$)", txt or "", re.I))


def _der_tlv(buf, i):
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or n > 4:
            raise ValueError("bad DER length")
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    if i + ln > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[i:i + ln], i + ln


def rsa_key_bits(p_b64):
    """Modulus size of a DKIM RSA key (SubjectPublicKeyInfo or PKCS#1 RSAPublicKey). None if undecodable."""
    s = re.sub(r"\s+", "", p_b64 or "")
    if not s:
        return None
    try:
        der = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
        tag, body, _ = _der_tlv(der, 0)
        if tag != 0x30:
            raise ValueError("not a SEQUENCE")
        t1, v1, nxt = _der_tlv(body, 0)
        if t1 == 0x30:  # SPKI: AlgorithmIdentifier, BIT STRING
            t2, bits, _ = _der_tlv(body, nxt)
            if t2 != 0x03:
                raise ValueError("no BIT STRING")
            _, rsa, _ = _der_tlv(bits[1:], 0)
            t3, mod, _ = _der_tlv(rsa, 0)
        else:  # PKCS#1 RSAPublicKey
            t3, mod = t1, v1
        if t3 != 0x02:
            raise ValueError("no INTEGER modulus")
        return int.from_bytes(mod, "big").bit_length()
    except (ValueError, IndexError, TypeError):
        try:
            der = base64.b64decode(s + "=" * (-len(s) % 4))
        except ValueError:
            return None
        # rough SPKI-size heuristic (162 bytes ~ 1024 bit, 294 ~ 2048 bit)
        return int(round((len(der) - 38) * 8 / 512.0)) * 512 if len(der) > 60 else None


# ---------------------------------------------------------------- TLS helpers
def parse_cert_time(s):
    s = " ".join(str(s).split())
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def _rdn(name_seq):
    out = {}
    for rdn in name_seq or []:
        for pair in rdn:
            if len(pair) == 2 and pair[0] not in out:
                out[pair[0]] = pair[1]
    return out


def san_names(cert):
    return [v.lower() for k, v in (cert.get("subjectAltName") or []) if k == "DNS"]


def hostname_matches(host, names):
    host = host.lower().rstrip(".")
    for n in names:
        n = n.lower().rstrip(".")
        if n == host:
            return True
        if n.startswith("*.") and host.count(".") == n.count(".") and host.split(".", 1)[1] == n[2:]:
            return True
    return False


def issuer_label(cert):
    iss = _rdn(cert.get("issuer"))
    return iss.get("organizationName") or iss.get("commonName") or ""


def issuer_caa_domains(issuer):
    i = (issuer or "").lower()
    out = set()
    for key, doms in CA_ISSUER_CAA:
        if key in i:
            out |= doms
    return out


def classify_verify_error(msg):
    m = (msg or "").lower()
    if "expired" in m:
        return "TLS_EXPIRED"
    if "hostname" in m or "not valid for" in m or "doesn't match" in m or "ip address mismatch" in m:
        return "TLS_HOSTNAME_MISMATCH"
    return "TLS_VERIFY_FAILED"


def _decode_der(der):
    """Parse a DER cert into getpeercert()-style dict (used only on the CERT_NONE inspection retry)."""
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as fh:
            fh.write(ssl.DER_cert_to_PEM_cert(der))
        return ssl._ssl._test_decode_cert(path)  # stdlib helper; best effort
    except Exception:  # noqa: BLE001
        return {}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _handshake(host, ctx, timeout, verified):
    with socket.create_connection((host, 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            der = s.getpeercert(binary_form=True) or b""
            cert = s.getpeercert() if verified else _decode_der(der)
            c = s.cipher() or ("", "", 0)
            return {"version": s.version(), "cipher": [c[0], c[1], c[2]],
                    "cert": json.loads(json.dumps(cert or {})), "sha256": hashlib.sha256(der).hexdigest() if der else None,
                    "verified": verified, "verify_error": None}


def tls_probe(host, timeout):
    """Live TLS handshake on :443 with SNI. Never raises."""
    try:
        return _handshake(host, ssl.create_default_context(), timeout, True)
    except ssl.SSLCertVerificationError as e:
        reason = getattr(e, "verify_message", None) or str(e)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            res = _handshake(host, ctx, timeout, False)
        except (OSError, ValueError) as e2:
            return {"error": "certificate verification failed (%s); inspection retry failed: %s" % (reason, e2),
                    "verify_error": reason}
        res["verify_error"] = reason
        return res
    except ssl.SSLError as e:
        msg = str(e)
        if "UNSUPPORTED_PROTOCOL" in msg.upper() or "VERSION" in msg.upper():
            try:  # server may only speak legacy TLS: retry permissively purely to identify the protocol
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ctx.minimum_version = ssl.TLSVersion.TLSv1
                ctx.set_ciphers("ALL:@SECLEVEL=0")
                res = _handshake(host, ctx, timeout, False)
                res["verify_error"] = None
                res["legacy_retry"] = True
                return res
            except (OSError, ValueError):
                pass
        return {"error": msg or e.__class__.__name__}
    except (OSError, ValueError) as e:
        return {"error": str(e) or e.__class__.__name__}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _lower_headers(msg):
    out = {}
    for k, v in (msg.items() if msg is not None else []):
        k = k.lower()
        out[k] = (out[k] + ", " + v) if k in out else v
    return out


def http_fetch(url, timeout):
    """Single GET without following redirects -> {status, headers} or {status: -1, error}. Never raises."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"}, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            return {"status": r.status, "headers": _lower_headers(r.headers)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "headers": _lower_headers(e.headers)}
    except Exception as e:  # noqa: BLE001 - URLError, timeouts, TLS errors, resets
        reason = getattr(e, "reason", None)
        return {"status": -1, "headers": {}, "error": str(reason or e) or e.__class__.__name__}


# ---------------------------------------------------------------- data source
class Source:
    """DoH (Cloudflare -> Google), TLS, HTTP and CT access; or a deterministic --offline fixture."""

    def __init__(self, timeout=15, offline=None):
        self.timeout, self.fx = timeout, offline
        self.endpoints, self.fallbacks, self.notes = set(), [], []
        self._cache = {}
        self._primary_fail = 0
        self.doh_ok = 0
        self.doh_fail = 0

    def _fallback(self, note):
        if note not in self.fallbacks:
            self.fallbacks.append(note)

    # -- DNS
    def doh(self, name, rtype, cd=False):
        name = name.rstrip(".").lower()
        key = (name, rtype, cd)
        if key not in self._cache:
            res = self._doh_offline(name, rtype, cd) if self.fx is not None else self._doh_live(name, rtype, cd)
            if res is None:
                self.doh_fail += 1
            else:
                self.doh_ok += 1
            self._cache[key] = res
        return self._cache[key]

    def _doh_offline(self, name, rtype, cd):
        if self.fx.get("doh_down"):
            return None
        if self.fx.get("doh_primary_down"):
            self._fallback("dns: cloudflare-dns.com -> dns.google")
        dns = self.fx.get("dns") or {}
        node = dns.get(name)
        if node is None:
            return {"status": 3, "ad": False, "answers": [], "cnames": [], "comment": None}
        entry = node.get(rtype + ("+CD" if cd else ""), node.get(rtype) if cd else None) if isinstance(node, dict) else None
        if entry == "FAIL":
            return None
        if entry is None:
            return {"status": 0, "ad": bool(node.get("_AD", False)), "answers": [], "cnames": [], "comment": None}
        if isinstance(entry, list):
            entry = {"Status": 0, "AD": bool(node.get("_AD", False)),
                     "Answer": [{"type": RTYPE[rtype], "data": d} for d in entry]}
        return parse_doh(entry, rtype)

    def _doh_live(self, name, rtype, cd):
        params = {"name": name, "type": rtype}
        if cd:
            params["cd"] = "1"
        for i, (label, base) in enumerate(DOH):
            if i == 0 and self._primary_fail >= 3:
                continue
            url = base + "?" + urllib.parse.urlencode(params)
            r = bc.http_json(url, headers={"Accept": "application/dns-json"}, retries=1, timeout=self.timeout)
            if r.status == 200 and isinstance(r.data, dict) and isinstance(r.data.get("Status"), int):
                self.endpoints.add(urllib.parse.urlparse(base).netloc)
                if i > 0:
                    self._fallback("dns: cloudflare-dns.com -> dns.google")
                elif self._primary_fail:
                    self._primary_fail = 0
                return parse_doh(r.data, rtype)
            if i == 0:
                self._primary_fail += 1
        return None

    def txt(self, name):
        r = self.doh(name, "TXT")
        return None if r is None or r["status"] not in (0, 3) else r["answers"]

    # -- TLS / HTTP
    def tls(self, domain):
        if self.fx is not None:
            return (self.fx.get("tls") or {}).get(domain) or {"error": "no TLS fixture"}
        self.endpoints.add("tls:443")
        return tls_probe(domain, self.timeout)

    def fetch(self, url, kind, timeout=None):
        if self.fx is not None:
            host = urllib.parse.urlparse(url).hostname
            r = (self.fx.get(kind) or {}).get(host)
            if isinstance(r, dict) and "chain" in r:
                path = urllib.parse.urlparse(url).path or "/"
                r = r["chain"].get(path, {"error": "no fixture"})
            if not r or "error" in r:
                return {"status": -1, "headers": {}, "error": (r or {}).get("error", "no fixture")}
            return {"status": int(r.get("status", 200)), "headers": {k.lower(): v for k, v in (r.get("headers") or {}).items()}}
        self.endpoints.add("https:443" if kind == "https" else "http:80")
        return http_fetch(url, timeout or self.timeout)

    # -- CT
    def ct(self, domain):
        if self.fx is not None:
            node = (self.fx.get("ct") or {}).get(domain) or {}
            cs, cr = node.get("certspotter", "FAIL"), node.get("crtsh", "FAIL")
            if isinstance(cs, list):
                return {"source": "certspotter", "items": [_ct_from_certspotter(x) for x in cs], "truncated": False}
            self._fallback("ct: certspotter -> crt.sh")
            if isinstance(cr, list):
                return {"source": "crt.sh", "items": [_ct_from_crtsh(x) for x in cr], "truncated": False}
            return None
        items, after, truncated, ok = [], None, False, False
        q = [("domain", domain), ("include_subdomains", "true"), ("expand", "dns_names"), ("expand", "issuer")]
        for page in range(CT_MAX_PAGES):
            url = CERTSPOTTER + "?" + urllib.parse.urlencode(q + ([("after", after)] if after else []))
            r = bc.http_json(url, retries=1, timeout=self.timeout)
            if r.status != 200 or not isinstance(r.data, list):
                break
            ok = True
            self.endpoints.add("api.certspotter.com")
            items += [_ct_from_certspotter(x) for x in r.data if isinstance(x, dict)]
            link = str(r.headers.get("Link") or r.headers.get("link") or "")
            if not r.data or 'rel="next"' not in link:
                break
            after = str(r.data[-1].get("id"))
            truncated = page == CT_MAX_PAGES - 1
        if ok:
            return {"source": "certspotter", "items": items, "truncated": truncated}
        self._fallback("ct: certspotter -> crt.sh")
        url = CRTSH + "?" + urllib.parse.urlencode({"q": "%." + domain, "output": "json"})
        r = bc.http_json(url, retries=1, timeout=max(self.timeout, 30))
        if r.status == 200 and isinstance(r.data, list):
            self.endpoints.add("crt.sh")
            return {"source": "crt.sh", "items": [_ct_from_crtsh(x) for x in r.data if isinstance(x, dict)], "truncated": False}
        return None


def _org_from_dn(dn):
    m = re.search(r"(?:^|,\s*)O=([^,]+)", dn or "")
    return m.group(1).strip().strip('"') if m else (dn or "")


def _ct_from_certspotter(x):
    iss = x.get("issuer") or {}
    name = iss.get("friendly_name") or _org_from_dn(iss.get("name", "")) if isinstance(iss, dict) else str(iss)
    return {"id": str(x.get("id", "")), "dns_names": sorted({n.lower() for n in x.get("dns_names") or []}),
            "issuer": name or "unknown", "not_before": x.get("not_before"), "not_after": x.get("not_after"),
            "cert_sha256": x.get("cert_sha256")}


def _ct_from_crtsh(x):
    names = sorted({n.strip().lower() for n in str(x.get("name_value", "")).split("\n") if n.strip()})
    return {"id": str(x.get("id", "")), "dns_names": names, "issuer": _org_from_dn(x.get("issuer_name", "")) or "unknown",
            "not_before": x.get("not_before"), "not_after": x.get("not_after"), "cert_sha256": None}


# ---------------------------------------------------------------- analysis
def analyze_headers(headers):
    """-> (list of (sev, code, msg), summary). headers: lowercase-keyed dict."""
    h = headers or {}
    out = []
    hsts = h.get("strict-transport-security")
    csp = h.get("content-security-policy")
    summ = {"hsts": hsts, "csp": bool(csp), "max_age": None}
    if not hsts:
        out.append(("HIGH", "HSTS_MISSING", "no Strict-Transport-Security header: browsers can be downgraded to plain HTTP"))
    else:
        m = re.search(r"max-age\s*=\s*\"?(\d+)", hsts, re.I)
        age = int(m.group(1)) if m else 0
        summ["max_age"] = age
        if age < HSTS_MIN_AGE:
            out.append(("MEDIUM", "HSTS_SHORT_MAX_AGE", "HSTS max-age=%d is below 180 days (15552000)" % age))
        if "includesubdomains" not in hsts.lower():
            out.append(("LOW", "HSTS_NO_INCLUDESUBDOMAINS", "HSTS lacks includeSubDomains: subdomains can still be downgraded"))
    if not csp:
        out.append(("MEDIUM", "CSP_MISSING", "no Content-Security-Policy header (XSS / injection blast radius unbounded)"))
    xcto = h.get("x-content-type-options")
    if not xcto or xcto.strip().lower() != "nosniff":
        out.append(("LOW", "XCTO_MISSING", "X-Content-Type-Options is not 'nosniff'"))
    if not h.get("referrer-policy"):
        out.append(("LOW", "REFERRER_POLICY_MISSING", "no Referrer-Policy header"))
    if not h.get("x-frame-options") and not (csp and "frame-ancestors" in csp.lower()):
        out.append(("LOW", "FRAME_PROTECTION_MISSING", "no X-Frame-Options and no CSP frame-ancestors (clickjacking)"))
    if not h.get("permissions-policy"):
        out.append(("LOW", "PERMISSIONS_POLICY_MISSING", "no Permissions-Policy header"))
    return out, summ


def analyze_port80(res):
    """-> (status_label, finding tuple or None)."""
    st = res.get("status", -1)
    if st == -1:
        return "UNVERIFIED (outbound port 80 unreachable/blocked)", None
    loc = (res.get("headers") or {}).get("location", "")
    if st in (301, 302, 303, 307, 308):
        if loc.lower().startswith("https://"):
            return "REDIRECT %d -> %s" % (st, loc), None
        return "REDIRECT %d -> %s" % (st, loc or "(no location)"), (
            "HIGH", "HTTP_NO_HTTPS_REDIRECT", "port 80 redirects (%d) to a non-HTTPS location %r" % (st, loc))
    if st == 200:
        return "200 OK over plain HTTP", ("HIGH", "HTTP_NO_HTTPS_REDIRECT",
                                         "port 80 serves 200 OK over plain HTTP instead of redirecting to https://")
    return "HTTP %d" % st, None


def expiry_finding(days, not_after):
    when = not_after.strftime("%Y-%m-%d") if not_after else "?"
    if days < 0:
        return "CRITICAL", "TLS_CERT_EXPIRED", "certificate expired %d day(s) ago (%s)" % (-days, when)
    if days < 7:
        return "CRITICAL", "TLS_EXPIRING_CRITICAL", "certificate expires in %d day(s) (%s)" % (days, when)
    if days < 14:
        return "HIGH", "TLS_EXPIRING_HIGH", "certificate expires in %d days (%s)" % (days, when)
    if days < 30:
        return "MEDIUM", "TLS_EXPIRING_SOON", "certificate expires in %d days (%s)" % (days, when)
    return "INFO", "TLS_CERT_VALID", "certificate valid for %d more days (expires %s)" % (days, when)


def weak_cipher(cipher):
    name, _, bits = (list(cipher or []) + ["", "", 0])[:3]
    return bool(WEAK_CIPHER_RE.search(name or "")) or (isinstance(bits, int) and 0 < bits < 128)


class Audit:
    def __init__(self, domain):
        self.d = domain
        self.findings = []
        self.degraded = []

    def add(self, sev, code, msg, target=None, **ev):
        ev = dict(ev, domain=self.d)
        if target:
            ev["target"] = target
        key = "%s:%s" % (self.d, code) + (":%s" % target if target else "")
        self.findings.append(bc.Finding(sev, code, "%s: %s" % (self.d, msg), ev, key=key))


def audit_domain(src, d, opts, now):
    A = Audit(d)
    rep = {"domain": d, "dns": {}, "mail": {}, "spf": None, "dmarc": None, "dkim": None, "mta_sts": None,
           "tls_rpt": None, "dnssec": None, "caa": None, "cnames": [], "tls": None, "http": None, "port80": None,
           "ct": None}
    q = {t: src.doh(d, t) for t in ("A", "AAAA", "CNAME", "NS", "MX", "CAA", "TXT", "DNSKEY")}
    failed = sorted(t for t, r in q.items() if r is None)
    dns_ok = len(failed) < len(q)
    if failed:
        A.degraded.append("dns(%s)" % ",".join(failed))

    def vals(t):
        r = q[t]
        return None if r is None else r["answers"]

    apex_status = next((r["status"] for t, r in sorted(q.items()) if r is not None and t in ("NS", "A", "TXT")), None)
    rep["dns"] = {"status": apex_status, "a": vals("A"), "aaaa": vals("AAAA"), "cname": vals("CNAME"),
                  "ns": sorted(vals("NS") or []) if vals("NS") is not None else None, "mx": None, "caa": None,
                  "txt": vals("TXT"), "dnskey_count": len(vals("DNSKEY") or []), "ad": bool(q["DNSKEY"] and q["DNSKEY"]["ad"]),
                  "failed_queries": failed}

    if dns_ok and apex_status == 3:
        A.add("HIGH", "DOMAIN_NXDOMAIN", "resolvers return NXDOMAIN for the apex (unregistered, expired or delegation lost)")
        return rep, A, {"dns": True, "tls": False, "http": False}

    # -- DNSSEC
    if q["DNSKEY"] is not None:
        dk = q["DNSKEY"]
        if dk["status"] == 2:
            cdr = src.doh(d, "DNSKEY", cd=True)
            if cdr is not None and cdr["status"] == 0 and cdr["answers"]:
                rep["dnssec"] = "BOGUS"
                A.add("HIGH", "DNSSEC_BROKEN_CHAIN", "validating resolver SERVFAILs but answers with checking disabled: "
                      "DNSSEC chain is bogus (DS/DNSKEY mismatch or expired RRSIG); the domain may be unresolvable for validating clients")
            else:
                rep["dnssec"] = "SERVFAIL"
                A.degraded.append("dnssec(SERVFAIL)")
        elif not dk["answers"]:
            rep["dnssec"] = "UNSIGNED"
            A.add("MEDIUM", "DNSSEC_NOT_ENABLED", "DNSSEC not enabled (no DNSKEY at apex): DNS answers can be spoofed in transit")
        elif not dk["ad"]:
            rep["dnssec"] = "UNVALIDATED"
            A.add("HIGH", "DNSSEC_BROKEN_CHAIN", "DNSKEY present at apex but validating resolver returned AD=false "
                  "(missing/mismatched DS at the registrar: possible broken chain of trust)")
        else:
            rep["dnssec"] = "VALIDATED"
            A.add("INFO", "DNSSEC_VALIDATED", "DNSSEC validated (DNSKEY present, AD=true)")

    # -- NS drift
    ns = rep["dns"]["ns"]
    if opts.get("expect_ns") and ns is not None:
        exp = [e.rstrip(".").lower() for e in opts["expect_ns"]]
        unmatched_ns = [n for n in ns if not any(e == n or e in n for e in exp)]
        unmatched_exp = [e for e in exp if not any(e == n or e in n for n in ns)]
        rep["dns"]["ns_expected"] = exp
        if not ns or unmatched_ns or unmatched_exp:
            A.add("HIGH", "NS_DRIFT", "nameservers %s do not match expected %s (unexpected: %s; missing: %s)"
                  % (ns, exp, unmatched_ns or "-", unmatched_exp or "-"), unexpected=unmatched_ns, missing=unmatched_exp)

    # -- CAA
    caa_raw = vals("CAA")
    if caa_raw is not None:
        caa = [parse_caa(x) for x in caa_raw]
        rep["caa"] = caa
        rep["dns"]["caa"] = caa_raw
        if not caa:
            A.add("LOW", "CAA_MISSING", "no CAA record: any public CA may issue certificates for this domain")

    # -- dangling CNAMEs (own records only)
    check_cnames(src, d, q, rep, A, opts)

    # -- mail
    mx = norm_mx(vals("MX")) if vals("MX") is not None else None
    rep["dns"]["mx"] = mx
    null_mx = bool(mx) and all(x.split()[-1] == "." for x in mx)
    mails = (not opts.get("no_mail")) and (mx is None or (bool(mx) and not null_mx))
    rep["mail"] = {"mails": mails, "mx": mx, "null_mx": null_mx, "no_mail_asserted": bool(opts.get("no_mail")),
                   "mx_known": mx is not None}
    if mx is not None and not mx:
        A.add("INFO", "MX_MISSING", "domain has no MX record; if it never receives mail publish RFC 7505 null MX '0 .'")
    if opts.get("no_mail") and mx and not null_mx:
        A.add("MEDIUM", "NO_MAIL_MX_PRESENT", "--no-mail asserted but MX records exist: %s" % ", ".join(mx))
    check_spf(src, d, vals("TXT"), mails, opts, rep, A)
    check_dmarc(src, d, mails, opts, rep, A)
    check_dkim(src, d, mails, opts, rep, A)
    if mails:
        for code, name, prefix, key in (("MTA_STS_MISSING", "_mta-sts." + d, "v=stsv1", "mta_sts"),
                                        ("TLS_RPT_MISSING", "_smtp._tls." + d, "v=tlsrptv1", "tls_rpt")):
            recs = src.txt(name)
            if recs is None:
                A.degraded.append("dns(%s)" % name)
                continue
            got = [r for r in recs if r.lower().replace(" ", "").startswith(prefix)]
            rep[key] = got[0] if got else None
            if not got:
                what = "MTA-STS (_mta-sts TXT)" if key == "mta_sts" else "SMTP TLS reporting (_smtp._tls TXT)"
                A.add("LOW", code, "mail-enabled domain has no %s: inbound SMTP TLS can be silently downgraded" % what)

    # -- TLS / HTTP
    has_addr = bool(vals("A") or vals("AAAA"))
    addr_unknown = vals("A") is None and vals("AAAA") is None
    core = {"dns": dns_ok, "tls": None, "http": None}
    if not has_addr and not addr_unknown and (not opts.get("no_tls") or not opts.get("no_http")):
        A.add("INFO", "NO_ADDRESS_RECORDS", "apex has no A/AAAA records; TLS and HTTP checks skipped")
    else:
        if not opts.get("no_tls"):
            core["tls"] = check_tls(src, d, rep, A, now)
        if not opts.get("no_http"):
            core["http"] = check_http(src, d, rep, A)
    if opts.get("ct", True):
        check_ct(src, d, rep, A, now)
    return rep, A, core


def check_cnames(src, d, q, rep, A, opts):
    cands = []
    if q["CNAME"] is not None and q["CNAME"]["answers"]:
        cands.append(d)
    cands.append("www." + d)
    for name in cands:
        r = src.doh(name, "A")
        if r is None or not r["cnames"]:
            continue
        chain = r["cnames"]
        _cname_verdict(src, name, chain, r, rep, A)


def _cname_verdict(src, name, chain, r, rep, A):
    target = chain[-1]
    provider = next((p for p in (match_takeover_provider(c) for c in chain) if p), None)
    resolves = bool(r["answers"])
    if not resolves and r["status"] == 0:
        r6 = src.doh(name, "AAAA")
        resolves = bool(r6 and r6["answers"])
    entry = {"name": name, "chain": chain, "target": target, "provider": provider, "status": r["status"], "resolves": resolves}
    rep["cnames"].append(entry)
    if r["status"] == 3 or (not resolves and provider and r["status"] == 0):
        why = "NXDOMAIN" if r["status"] == 3 else "no A/AAAA"
        A.add("HIGH", "DANGLING_CNAME_TAKEOVER_RISK", "%s CNAME -> %s (%s)%s: whoever claims that name controls %s"
              % (name, target, why, " on takeover-prone provider %s" % provider if provider else "", name),
              target=name, cname=target, provider=provider)


def check_spf(src, d, txt, mails, opts, rep, A):
    if txt is None:
        A.degraded.append("dns(TXT)")
        return
    spfs = [t for t in txt if is_spf(t)]
    info = {"records": spfs, "record": spfs[0] if spfs else None}
    rep["spf"] = info
    if len(spfs) > 1:
        A.add("HIGH", "SPF_MULTIPLE_RECORDS", "%d v=spf1 TXT records at apex: RFC 7208 §3.2 permerror, SPF fails for all mail" % len(spfs))
    if not spfs:
        sev = "HIGH" if mails else "MEDIUM"
        A.add(sev, "SPF_MISSING", "no SPF record" + ("" if mails else "; domain sends no mail: publish 'v=spf1 -all' to block spoofing"))
        return
    rec = spfs[0]
    if opts.get("no_mail") and " ".join(rec.lower().split()) != "v=spf1 -all":
        A.add("MEDIUM", "SPF_NO_MAIL_NOT_STRICT", "--no-mail asserted but SPF is %r instead of 'v=spf1 -all'" % rec)
    st = spf_analyze(rec, src.txt)
    info.update({"lookups": st["lookups"], "loop": st["loop"], "ptr": st["ptr"], "missing_includes": st["missing"],
                 "failed_includes": st["failed"], "tree": st["tree"], "macros": st["macros"]})
    if st["failed"]:
        A.degraded.append("dns(spf-include)")
    if st["loop"] or st["depth_exceeded"]:
        A.add("HIGH", "SPF_RECURSIVE_LOOP", "SPF include/redirect %s: permerror" %
              ("loop " + st["loop"] if st["loop"] else "nesting exceeds %d hops" % SPF_MAX_HOPS), path=st["loop"])
    if st["lookups"] > SPF_LOOKUP_LIMIT:
        A.add("HIGH", "SPF_LOOKUP_LIMIT_EXCEEDED", "SPF needs %d/%d DNS lookups: RFC 7208 §4.6.4 permerror (receivers treat SPF as failed)"
              % (st["lookups"], SPF_LOOKUP_LIMIT), lookups=st["lookups"])
    elif st["lookups"] >= 8:
        A.add("LOW", "SPF_LOOKUP_NEAR_LIMIT", "SPF uses %d/%d DNS lookups: close to the 10-lookup ceiling" % (st["lookups"], SPF_LOOKUP_LIMIT),
              lookups=st["lookups"])
    if st["ptr"]:
        A.add("LOW", "SPF_DEPRECATED_PTR", "SPF uses the 'ptr' mechanism, which RFC 7208 §5.5 says SHOULD NOT be used")
    if st["missing"]:
        A.add("MEDIUM", "SPF_INCLUDE_NO_RECORD", "SPF include/redirect target(s) without an SPF record: %s (permerror)"
              % ", ".join(st["missing"]))
    q = spf_all_qualifier(rec)
    redir = spf_redirect(rec)
    if q is None and redir:
        recs = src.txt(redir) or []
        tgt = [r for r in recs if is_spf(r)]
        q = spf_all_qualifier(tgt[0]) if tgt else None
        info["all_via_redirect"] = redir
    info["all"] = q
    if q == "+":
        A.add("CRITICAL", "SPF_PASS_ALL", "SPF ends in '+all': allows any host on the internet to send authenticated mail as this domain")
    elif q == "?":
        A.add("MEDIUM", "SPF_NEUTRAL_ALL", "SPF ends in '?all' (neutral): unauthorised senders are not rejected")
    elif q is None and not redir:
        A.add("MEDIUM", "SPF_NO_TERMINAL_ALL", "SPF has no 'all' mechanism and no redirect=: defaults to neutral")
    elif q == "~":
        A.add("INFO", "SPF_SOFTFAIL_ALL", "SPF ends in '~all' (softfail); rely on DMARC enforcement or move to '-all'")
    elif q == "-":
        A.add("INFO", "SPF_HARDFAIL_ALL", "SPF ends in '-all' (hardfail)")


def check_dmarc(src, d, mails, opts, rep, A):
    recs = src.txt("_dmarc." + d)
    if recs is None:
        A.degraded.append("dns(_dmarc)")
        return
    dm = [r for r in recs if is_dmarc(r)]
    rep["dmarc"] = {"records": dm, "record": dm[0] if dm else None, "tags": parse_tags(dm[0]) if dm else {}}
    if not dm:
        sev = "HIGH" if mails else "MEDIUM"
        A.add(sev, "DMARC_MISSING", "no _dmarc record" + ("" if mails else "; domain sends no mail: publish 'v=DMARC1; p=reject;'"))
        return
    if len(dm) > 1:
        A.add("HIGH", "DMARC_MULTIPLE_RECORDS", "%d DMARC records at _dmarc: receivers ignore DMARC entirely (RFC 7489 §6.6.3)" % len(dm))
        return
    tags = rep["dmarc"]["tags"]
    p = tags.get("p", "").lower()
    sp = tags.get("sp", "").lower()
    if p not in DMARC_RANK:
        A.add("MEDIUM", "DMARC_INVALID_POLICY", "DMARC p=%r is missing/invalid: record is ignored" % tags.get("p"))
        return
    if p == "none":
        A.add("MEDIUM", "DMARC_POLICY_NONE", "DMARC p=none: monitoring-only policy does not block spoofed mail")
    if sp == "none" and p in ("quarantine", "reject"):
        A.add("MEDIUM", "DMARC_SUBDOMAIN_POLICY_NONE", "DMARC sp=none: subdomains left unprotected despite p=%s" % p)
    try:
        pct = int(tags.get("pct", "100"))
    except ValueError:
        pct = 100
    if pct < 100:
        A.add("LOW", "DMARC_PARTIAL_PCT", "DMARC policy applied to only %d%% of messages (pct=%d)" % (pct, pct), pct=pct)
    if p in ("quarantine", "reject") and not tags.get("rua"):
        A.add("LOW", "DMARC_NO_RUA", "DMARC enforcement enabled without aggregate reporting (rua=mailto:...)")
    if opts.get("no_mail") and p != "reject":
        A.add("MEDIUM", "DMARC_NO_MAIL_NOT_REJECT", "--no-mail asserted but DMARC p=%s instead of p=reject" % p)


def check_dkim(src, d, mails, opts, rep, A):
    sels = opts.get("selectors") or DEFAULT_SELECTORS
    found, failed = [], []
    for sel in sels:
        name = "%s._domainkey.%s" % (sel, d)
        r = src.doh(name, "TXT")
        if r is None:
            failed.append(sel)
            continue
        if r["cnames"] and r["status"] == 3:
            _cname_verdict(src, name, r["cnames"], r, rep, A)
        keys = [t for t in r["answers"] if "p=" in t.replace(" ", "") or t.lower().startswith("v=dkim1")]
        if not keys:
            continue
        tags = parse_tags(keys[0])
        k = (tags.get("k") or "rsa").lower()
        p = re.sub(r"\s+", "", tags.get("p", ""))
        entry = {"selector": sel, "k": k, "bits": None, "revoked": not p, "cname": r["cnames"][-1] if r["cnames"] else None}
        if not p:
            A.add("INFO", "DKIM_REVOKED_KEY", "DKIM selector '%s' publishes an empty p= (revoked key)" % sel, target=sel)
        elif k == "rsa":
            bits = rsa_key_bits(p)
            entry["bits"] = bits
            if bits is not None and bits < 1024:
                A.add("HIGH", "DKIM_WEAK_KEY", "DKIM selector '%s' RSA key is %d bits (<1024: forgeable)" % (sel, bits), target=sel, bits=bits)
            elif bits is not None and bits < 2048:
                A.add("LOW", "DKIM_1024_BIT_KEY", "DKIM selector '%s' RSA key is %d bits; rotate to 2048" % (sel, bits), target=sel, bits=bits)
        elif k == "ed25519":
            entry["bits"] = 256
        found.append(entry)
    rep["dkim"] = {"probed": list(sels), "found": found, "failed": failed}
    if failed:
        A.degraded.append("dns(dkim)")
    if mails and not found and not failed:
        A.add("LOW", "DKIM_NOT_FOUND", "no DKIM key at the %d selector(s) tried (%s); pass --dkim-selector with your provider's selector"
              % (len(sels), ",".join(sels)))


def check_tls(src, d, rep, A, now):
    t = src.tls(d)
    info = {"version": t.get("version"), "cipher": t.get("cipher"), "sha256": t.get("sha256"), "verified": t.get("verified"),
            "verify_error": t.get("verify_error"), "error": t.get("error")}
    rep["tls"] = info
    vcode = None
    if t.get("verify_error"):
        vcode = classify_verify_error(t["verify_error"])
        A.add("CRITICAL", vcode, "TLS certificate verification failed: %s" % t["verify_error"])
    if t.get("error") and not t.get("cert"):
        if not vcode:
            A.add("INFO", "TLS_UNREACHABLE", "TLS handshake on :443 failed: %s" % t["error"])
            A.degraded.append("tls")
        return False
    cert = t.get("cert") or {}
    subj = _rdn(cert.get("subject"))
    sans = san_names(cert)
    na = parse_cert_time(cert.get("notAfter")) if cert.get("notAfter") else None
    nb = parse_cert_time(cert.get("notBefore")) if cert.get("notBefore") else None
    issuer = issuer_label(cert)
    info.update({"subject_cn": subj.get("commonName"), "issuer": issuer, "issuer_cn": _rdn(cert.get("issuer")).get("commonName"),
                 "serial": cert.get("serialNumber"), "not_before": nb.strftime("%Y-%m-%dT%H:%M:%SZ") if nb else None,
                 "not_after": na.strftime("%Y-%m-%dT%H:%M:%SZ") if na else None, "san": sans, "days_remaining": None})
    if na:
        days = int((na - now).total_seconds() // 86400)
        info["days_remaining"] = days
        sev, code, msg = expiry_finding(days, na)
        if not (code == "TLS_CERT_EXPIRED" and vcode == "TLS_EXPIRED"):
            A.add(sev, code, msg, days=days)
    if sans and not hostname_matches(d, sans) and vcode != "TLS_HOSTNAME_MISMATCH":
        A.add("CRITICAL", "TLS_HOSTNAME_MISMATCH", "certificate SANs %s do not cover %s" % (sans[:6], d))
    ver = t.get("version")
    if ver in LEGACY_TLS:
        A.add("HIGH", "TLS_LEGACY_PROTOCOL", "server negotiated %s (< TLSv1.2)" % ver)
    if t.get("cipher") and weak_cipher(t["cipher"]):
        A.add("HIGH", "TLS_WEAK_CIPHER", "weak cipher negotiated: %s (%s-bit)" % (t["cipher"][0], t["cipher"][2]))
    caa = [c for c in (rep.get("caa") or []) if c["tag"] in ("issue", "issuewild")]
    if caa and issuer:
        wildcard = any(s.startswith("*.") for s in sans)
        tagset = [c for c in caa if c["tag"] == "issuewild"] if wildcard and any(c["tag"] == "issuewild" for c in caa) \
            else [c for c in caa if c["tag"] == "issue"]
        allowed = {c["ca"] for c in tagset if c["ca"]}
        known = issuer_caa_domains(issuer + " " + (info.get("issuer_cn") or ""))
        info["caa_allowed"] = sorted(allowed)
        if allowed and known and not (allowed & known):
            A.add("HIGH", "TLS_ISSUER_CAA_MISMATCH", "live certificate issuer %r is not authorised by CAA %s (expected one of %s)"
                  % (issuer, sorted(allowed), sorted(known)), issuer=issuer, caa=sorted(allowed))
    return True


def check_http(src, d, rep, A):
    url = "https://%s/" % d
    chain, res, first = [], None, None
    for _ in range(5):
        res = src.fetch(url, "https")
        first = first or res
        chain.append({"url": url, "status": res.get("status")})
        loc = (res.get("headers") or {}).get("location")
        if res.get("status") in (301, 302, 303, 307, 308) and loc:
            nxt = urllib.parse.urljoin(url, loc)
            pu, pn = urllib.parse.urlparse(url), urllib.parse.urlparse(nxt)
            if (pn.scheme, pn.netloc) == (pu.scheme, pu.netloc):
                url = nxt
                continue
            chain[-1]["cross_origin_redirect"] = nxt
        break
    info = {"url": "https://%s/" % d, "status": res.get("status"), "chain": chain, "error": res.get("error"), "headers": {}}
    rep["http"] = info
    if res.get("status", -1) == -1:
        A.add("INFO", "HTTPS_UNREACHABLE", "HTTPS GET %s failed: %s" % (url, res.get("error")))
        A.degraded.append("https")
        port80(src, d, rep, A)
        return False
    hdrs = dict(res.get("headers") or {})
    if "strict-transport-security" not in hdrs and first and (first.get("headers") or {}).get("strict-transport-security"):
        hdrs["strict-transport-security"] = first["headers"]["strict-transport-security"]
    keep = ("strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy",
            "x-frame-options", "permissions-policy", "server", "location")
    info["headers"] = {k: hdrs[k] for k in keep if k in hdrs}
    found, summ = analyze_headers(hdrs)
    info.update(summ)
    info["missing"] = [c for _, c, _ in found]
    for sev, code, msg in found:
        A.add(sev, code, msg)
    port80(src, d, rep, A)
    return True


def port80(src, d, rep, A):
    res = src.fetch("http://%s/" % d, "http80", timeout=min(src.timeout, 5))
    label, f = analyze_port80(res)
    rep["port80"] = {"status": label, "code": res.get("status"), "error": res.get("error")}
    if f:
        A.add(*f)


def check_ct(src, d, rep, A, now):
    r = src.ct(d)
    if r is None:
        rep["ct"] = {"status": "UNAVAILABLE", "source": None, "issuances": 0, "names": [], "issuers": []}
        A.add("INFO", "CT_UNAVAILABLE", "Certificate Transparency lookup failed (Cert Spotter and crt.sh); CT not assessed")
        return
    items = sorted(r["items"], key=lambda x: (x.get("not_before") or "", x.get("id") or ""), reverse=True)
    names = sorted({n for x in items for n in x["dns_names"]})
    issuers = sorted({x["issuer"] for x in items})
    status = "OK"
    if r["source"] == "crt.sh" and not items:
        status = "INCONCLUSIVE"
        A.add("INFO", "CT_INCONCLUSIVE", "crt.sh returned no certificates (fallback source; empty is inconclusive, not clean)")
    rep["ct"] = {"status": status, "source": r["source"], "issuances": len(items), "names": names, "issuers": issuers,
                 "truncated": r.get("truncated", False),
                 "recent": [{k: x.get(k) for k in ("id", "issuer", "not_before", "not_after", "dns_names")} for x in items[:10]]}
    allowed = {c["ca"] for c in (rep.get("caa") or []) if c["tag"] in ("issue", "issuewild") and c["ca"]}
    if allowed:
        nowiso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        bad = sorted({x["issuer"] for x in items if (x.get("not_after") or "9") > nowiso
                      and issuer_caa_domains(x["issuer"]) and not (issuer_caa_domains(x["issuer"]) & allowed)})
        if bad:
            A.add("MEDIUM", "CT_ISSUER_NOT_IN_CAA", "unexpired CT-logged certificates from issuer(s) %s not authorised by CAA %s"
                  % (bad, sorted(allowed)), issuers=bad)


# ---------------------------------------------------------------- remediation
def build_remediation(rep, findings):
    d = rep["domain"]
    codes = {f.code for f in findings}
    mails = rep["mail"].get("mails")
    dns, notes = [], []
    if codes & {"SPF_MISSING", "SPF_NO_MAIL_NOT_STRICT", "SPF_PASS_ALL", "SPF_NEUTRAL_ALL", "SPF_NO_TERMINAL_ALL", "SPF_MULTIPLE_RECORDS"}:
        if mails:
            dns.append('%s. 3600 IN TXT "v=spf1 mx include:<your-mail-provider-spf> -all"' % d)
            notes.append("SPF: publish exactly ONE v=spf1 record; replace the include with your provider's (e.g. _spf.google.com).")
        else:
            dns.append('%s. 3600 IN TXT "v=spf1 -all"' % d)
    if codes & {"SPF_LOOKUP_LIMIT_EXCEEDED", "SPF_LOOKUP_NEAR_LIMIT", "SPF_RECURSIVE_LOOP", "SPF_DEPRECATED_PTR"}:
        notes.append("SPF: remove 'ptr', unused includes and nested loops; replace stable senders with ip4:/ip6: to stay under 10 lookups.")
    if codes & {"DMARC_MISSING", "DMARC_POLICY_NONE", "DMARC_NO_MAIL_NOT_REJECT", "DMARC_MULTIPLE_RECORDS",
                "DMARC_SUBDOMAIN_POLICY_NONE", "DMARC_PARTIAL_PCT", "DMARC_NO_RUA", "DMARC_INVALID_POLICY"}:
        if mails:
            dns.append('_dmarc.%s. 3600 IN TXT "v=DMARC1; p=quarantine; sp=quarantine; pct=100; adkim=s; aspf=s; rua=mailto:dmarc-reports@%s"' % (d, d))
            notes.append("DMARC: once aggregate reports show all legitimate senders aligned, move p=quarantine -> p=reject.")
        else:
            dns.append('_dmarc.%s. 3600 IN TXT "v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s"' % d)
    if "MX_MISSING" in codes and not mails:
        dns.append("%s. 3600 IN MX 0 ." % d)
    if "CAA_MISSING" in codes or "TLS_ISSUER_CAA_MISMATCH" in codes:
        cas = set()
        tls = rep.get("tls") or {}
        for iss in [tls.get("issuer") or ""] + list((rep.get("ct") or {}).get("issuers") or []):
            k = issuer_caa_domains(iss)
            for pref in ("letsencrypt.org", "pki.goog", "digicert.com", "sectigo.com", "amazon.com", "globalsign.com",
                         "zerossl.com", "entrust.net", "godaddy.com", "ssl.com", "buypass.com"):
                if pref in k:
                    cas.add(pref)
                    break
        for ca in sorted(cas or {"letsencrypt.org"}):
            dns.append('%s. 3600 IN CAA 0 issue "%s"' % (d, ca))
        wild = any(n.startswith("*.") for n in ((rep.get("ct") or {}).get("names") or []) + list(tls.get("san") or []))
        if not wild:
            dns.append('%s. 3600 IN CAA 0 issuewild ";"' % d)
        dns.append('%s. 3600 IN CAA 0 iodef "mailto:security@%s"' % (d, d))
        notes.append("CAA: list every CA your CDN/host uses (check its docs) before publishing, or renewals will fail.")
    if "MTA_STS_MISSING" in codes:
        dns.append('_mta-sts.%s. 3600 IN TXT "v=STSv1; id=%s01"' % (d, "YYYYMMDD"))
        notes.append("MTA-STS: also serve https://mta-sts.%s/.well-known/mta-sts.txt (version: STSv1 / mode: enforce / mx: ... / max_age: 604800)." % d)
    if "TLS_RPT_MISSING" in codes:
        dns.append('_smtp._tls.%s. 3600 IN TXT "v=TLSRPTv1; rua=mailto:tlsrpt@%s"' % (d, d))
    if codes & {"DNSSEC_NOT_ENABLED", "DNSSEC_BROKEN_CHAIN"}:
        notes.append("DNSSEC: enable signing at your DNS host, then publish the DS record it shows at your registrar "
                     "(Cloudflare: DNS > Settings > DNSSEC; Route 53: Hosted zone > DNSSEC signing).")
    if "DANGLING_CNAME_TAKEOVER_RISK" in codes:
        notes.append("Dangling CNAME: delete the record (or re-claim the resource at the provider) immediately.")
    if codes & {"DKIM_WEAK_KEY", "DKIM_1024_BIT_KEY"}:
        notes.append("DKIM: generate a 2048-bit key at your mail provider under a NEW selector, publish it, switch signing, then revoke the old one (p=).")
    hdr = [(n, v) for n, v in REC_HEADERS if _hdr_code(n) & codes]
    if "CSP_MISSING" in codes:
        notes.append("CSP: the starter policy blocks third-party scripts/styles; ship it first as "
                     "Content-Security-Policy-Report-Only, add the origins you need, then enforce.")
    if codes & {"HSTS_MISSING", "HSTS_SHORT_MAX_AGE", "HSTS_NO_INCLUDESUBDOMAINS"}:
        notes.append("HSTS: includeSubDomains/preload commit EVERY subdomain to HTTPS for the max-age; confirm none serve "
                     "plain HTTP before adding them (drop 'preload' unless you will submit to hstspreload.org).")
    if "HTTP_NO_HTTPS_REDIRECT" in codes:
        notes.append("Port 80: redirect every plain-HTTP request with 301 to https:// (nginx: return 301 https://$host$request_uri;).")
    out = {"dns": dns, "headers": hdr, "notes": notes}
    if hdr:
        out["nginx"] = ['add_header %s "%s" always;' % (n, v.replace('"', '\\"')) for n, v in hdr]
        out["vercel_json"] = json.dumps({"headers": [{"source": "/(.*)", "headers": [{"key": n, "value": v} for n, v in hdr]}]}, indent=2)
        out["netlify_headers"] = ["/*"] + ["  %s: %s" % (n, v) for n, v in hdr]
    return out


def _hdr_code(name):
    return {"Strict-Transport-Security": {"HSTS_MISSING", "HSTS_SHORT_MAX_AGE", "HSTS_NO_INCLUDESUBDOMAINS"},
            "Content-Security-Policy": {"CSP_MISSING"}, "X-Content-Type-Options": {"XCTO_MISSING"},
            "Referrer-Policy": {"REFERRER_POLICY_MISSING"}, "X-Frame-Options": {"FRAME_PROTECTION_MISSING"},
            "Permissions-Policy": {"PERMISSIONS_POLICY_MISSING"}}[name]


# ---------------------------------------------------------------- state + delta
def make_state(rep, findings):
    tls = rep.get("tls") or {}
    ct = rep.get("ct") or {}
    return {"domain": rep["domain"], "findings": [f.to_dict() for f in bc.sort_findings(findings)],
            "tls": {k: tls.get(k) for k in ("sha256", "serial", "not_after", "issuer")} if tls.get("sha256") else None,
            "ns": (rep.get("dns") or {}).get("ns"), "spf": (rep.get("spf") or {}).get("record") if rep.get("spf") is not None else None,
            "spf_known": rep.get("spf") is not None, "dmarc": (rep.get("dmarc") or {}).get("record"),
            "dmarc_known": rep.get("dmarc") is not None, "dnssec": rep.get("dnssec"),
            "ct": {"count": ct.get("issuances", 0), "names": ct.get("names", [])} if ct.get("status") == "OK" else None}


def diff_findings(old, new):
    """NEW / CHANGED / RESOLVED keyed by finding key; time-varying messages (days left) only change on severity."""
    o = {x["key"] if isinstance(x, dict) else x.key: (bc.Finding.from_dict(x) if isinstance(x, dict) else x) for x in old or []}
    n = {f.key: f for f in new}
    res = {"NEW": [], "CHANGED": [], "RESOLVED": []}
    for k in sorted(n):
        if k not in o:
            res["NEW"].append(n[k])
        elif o[k].sev != n[k].sev or (n[k].code not in TIME_VARYING and o[k].msg != n[k].msg):
            res["CHANGED"].append(n[k])
    res["RESOLVED"] = [o[k] for k in sorted(o) if k not in n]
    return res


def compute_events(prev, cur, new_findings):
    d = cur["domain"]
    ev = []

    def add(sev, code, msg, **e):
        ev.append(bc.Finding(sev, code, "%s: %s" % (d, msg), dict(e, domain=d), key="%s:EVENT:%s" % (d, code)))

    pt, ct = prev.get("tls"), cur.get("tls")
    if pt and ct and pt.get("sha256") != ct.get("sha256"):
        sev = "LOW" if pt.get("issuer") != ct.get("issuer") else "INFO"
        add(sev, "CERT_ROTATED", "certificate rotated: serial %s -> %s, sha256 %s.. -> %s.., not_after %s -> %s, issuer %s -> %s"
            % (pt.get("serial"), ct.get("serial"), (pt.get("sha256") or "")[:12], (ct.get("sha256") or "")[:12],
               pt.get("not_after"), ct.get("not_after"), pt.get("issuer"), ct.get("issuer")))
    if prev.get("ns") is not None and cur.get("ns") is not None and sorted(prev["ns"]) != sorted(cur["ns"]):
        add("MEDIUM", "NS_CHANGED", "nameservers changed: %s -> %s" % (sorted(prev["ns"]), sorted(cur["ns"])),
            old=sorted(prev["ns"]), new=sorted(cur["ns"]))
    if prev.get("spf_known", True) and cur.get("spf_known") and prev.get("spf") != cur.get("spf"):
        add("MEDIUM" if not cur.get("spf") else "LOW", "SPF_CHANGED", "SPF changed: %r -> %r" % (prev.get("spf"), cur.get("spf")))
    if prev.get("dmarc_known", True) and cur.get("dmarc_known") and prev.get("dmarc") != cur.get("dmarc"):
        op = DMARC_RANK.get(parse_tags(prev.get("dmarc")).get("p", "").lower(), -1)
        np_ = DMARC_RANK.get(parse_tags(cur.get("dmarc")).get("p", "").lower(), -1)
        add("MEDIUM" if np_ < op else "LOW", "DMARC_CHANGED", "DMARC changed: %r -> %r" % (prev.get("dmarc"), cur.get("dmarc")))
    if prev.get("dnssec") == "VALIDATED" and cur.get("dnssec") not in (None, "VALIDATED"):
        add("HIGH", "DNSSEC_LOST", "DNSSEC was validated at baseline and is now %s" % cur.get("dnssec"))
    pc, cc = prev.get("ct"), cur.get("ct")
    if pc and cc:
        fresh = sorted(set(cc["names"]) - set(pc["names"]))
        if fresh:
            add("MEDIUM", "NEW_CT_NAMES", "new names in CT logs since baseline: %s" % ", ".join(fresh[:20]), names=fresh)
        if cc["count"] > pc["count"]:
            add("INFO", "NEW_CT_ISSUANCES", "%d new CT issuance(s) since baseline (%d -> %d)" % (cc["count"] - pc["count"], pc["count"], cc["count"]))
    old_exp = {f["code"] for f in prev.get("findings", []) if f["code"] in EXPIRY_CODES}
    for f in new_findings:
        if f.code in EXPIRY_CODES and f.code != "TLS_CERT_VALID" and f.code not in old_exp:
            add(f.sev, "CERT_EXPIRY_WINDOW", "certificate entered expiry window: %s" % f.msg.split(": ", 1)[-1])
    old_codes = {f["code"] for f in prev.get("findings", [])}
    regressed = sorted({f.code for f in new_findings if f.code in HEADER_CODES and f.code not in old_codes})
    if regressed:
        add("MEDIUM", "HEADER_REGRESSED", "security header regression since baseline: %s" % ", ".join(regressed), codes=regressed)
    return ev


# ---------------------------------------------------------------- exit code
def compute_exit(findings, degraded, fail_on, force_degraded=False):
    if force_degraded:
        return bc.EXIT_DEGRADED
    if fail_on != "none" and findings:
        worst = max(bc.SEV[f.sev] for f in findings)
        if worst >= bc.SEV[fail_on.upper()]:
            return bc.EXIT_CRITICAL if worst == 4 else bc.EXIT_HIGH if worst == 3 else bc.EXIT_MEDIUM
    return bc.EXIT_DEGRADED if degraded else bc.EXIT_OK


# ---------------------------------------------------------------- rendering
def _sev_counts(findings):
    c = {k: 0 for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
    for f in findings:
        c[f.sev] += 1
    return c


def _worst(findings):
    return max((f.sev for f in findings), key=lambda s: bc.SEV[s], default="NONE")


def _j(v):
    return ", ".join(v) if v else "-"


def _yn(v):
    return "yes" if v else "no"


def _dns_line(r):
    dn = r["dns"]
    return ("domain    %s  A=%s AAAA=%s NS=%d MX=%s CAA=%d TXT=%d DNSKEY=%d AD=%s dnssec=%s"
            % (r["domain"], _j(dn.get("a")), _j(dn.get("aaaa")), len(dn.get("ns") or []),
               "?" if dn.get("mx") is None else len(dn["mx"]), len(r.get("caa") or []), len(dn.get("txt") or []),
               dn.get("dnskey_count", 0), str(dn.get("ad")).lower(), r.get("dnssec") or "?"))


def _tls_line(t):
    if not t:
        return "  tls     skipped"
    if t.get("error") and not t.get("issuer"):
        return "  tls     UNREACHABLE (%s)" % t["error"]
    c = t.get("cipher") or ["?", "?", 0]
    return ("  tls     %s %s (%s-bit) issuer=%s expires=%s (%sd) verified=%s serial=%s sha256=%s.. SAN=%s"
            % (t.get("version"), c[0], c[2], t.get("issuer"), (t.get("not_after") or "?")[:10], t.get("days_remaining"),
               _yn(t.get("verified")), t.get("serial"), (t.get("sha256") or "")[:16], t.get("san")))


def _mail_line(r):
    m, spf, dm, dk = r["mail"], r.get("spf") or {}, r.get("dmarc") or {}, r.get("dkim") or {}
    why = "--no-mail" if m.get("no_mail_asserted") else ("null MX" if m.get("null_mx") else ("no MX" if not m.get("mx") else "MX"))
    return ("  mail    sends_mail=%s (%s) mx=%s spf=%s lookups=%s dmarc=%s dkim=%d/%d selectors mta-sts=%s tls-rpt=%s"
            % (_yn(m.get("mails")), why, _j(m.get("mx")), repr(spf.get("record")) if spf.get("record") else "none",
               spf.get("lookups", "-"), repr(dm.get("record")) if dm.get("record") else "none",
               len(dk.get("found") or []), len(dk.get("probed") or []),
               "yes" if r.get("mta_sts") else "-", "yes" if r.get("tls_rpt") else "-"))


def _http_line(h):
    if not h:
        return "  http    skipped"
    if h.get("status") == -1:
        return "  http    UNREACHABLE (%s)" % h.get("error")
    return "  http    %s %s hsts=%s missing=%s" % (h["url"], h["status"], h.get("hsts") or "none", _j(h.get("missing")))


def _ct_line(c):
    if not c:
        return "  ct      skipped"
    return "  ct      %s %s issuances=%d names=%d issuers=%s%s" % (
        c.get("source") or "-", c["status"], c["issuances"], len(c["names"]), _j(c["issuers"]), " (truncated)" if c.get("truncated") else "")


def _delta_lines(delta):
    if delta is None:
        return []
    lines = ["", "delta"]
    for d in sorted(delta):
        x = delta[d]
        if x.get("skipped"):
            lines.append("  %s: state not updated (%s)" % (d, x["skipped"]))
            continue
        if x["baseline"]:
            lines.append("  %s: baseline run, no prior state; recorded %d finding(s)" % (d, len(x["new"])))
            continue
        items = [("EVENT", x["events"]), ("NEW", x["new"]), ("CHANGED", x["changed"]), ("RESOLVED", x["resolved"])]
        for label, fs in items:
            for f in fs:
                lines.append("  %-8s [%s] %s: %s" % (label, f.sev, f.code, f.msg))
        if not any(fs for _, fs in items):
            lines.append("  %s: (no changes)" % d)
    return lines


def render_text(reports, findings, delta, code, degraded, header, fail_on):
    L = [header]
    for r in reports:
        fs = [f for f in findings if f.evidence.get("domain") == r["domain"]]
        L += ["", _dns_line(r), "  ns      %s" % _j(r["dns"].get("ns"))]
        for c in r.get("cnames") or []:
            L.append("  cname   %s -> %s%s status=%s resolves=%s" % (c["name"], c["target"], " [%s]" % c["provider"] if c["provider"] else "",
                                                                    c["status"], _yn(c["resolves"])))
        L += [_mail_line(r), _tls_line(r.get("tls")), _http_line(r.get("http")),
              "  port80  %s" % ((r.get("port80") or {}).get("status") or "skipped"), _ct_line(r.get("ct")), "  findings"]
        L += ["    [%-8s] %s: %s" % (f.sev, f.code, f.msg.split(": ", 1)[-1]) for f in bc.sort_findings(fs)] or ["    (no findings)"]
        rem = r.get("remediation") or {}
        if rem.get("dns"):
            L.append("  fix     copy-paste DNS records (review first; --format markdown adds header snippets):")
            L += ["    " + x for x in rem["dns"]]
        if rem.get("headers"):
            L.append("  fix     add headers: " + ", ".join(n for n, _ in rem["headers"]))
    c = _sev_counts(findings)
    L += ["", "summary   %d domain(s)  critical=%d high=%d medium=%d low=%d info=%d  worst=%s"
          % (len(reports), c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], _worst(findings))]
    if degraded:
        L.append("DEGRADED  incomplete checks: %s (result may be incomplete; not a clean pass)" % ", ".join(degraded))
    L += _delta_lines(delta)
    L += ["", "exit_code %d (fail-on=%s)" % (code, fail_on), SCOPE_NOTE]
    return "\n".join(L)


def _md(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(reports, findings, delta, code, degraded, header, fail_on):
    c = _sev_counts(findings)
    L = ["# Domain, TLS & Email Auth Hygiene Report", "", "```", header, "```", "",
         "**Domains:** %d | **Worst:** %s | Critical %d · High %d · Medium %d · Low %d · Info %d | **Exit code:** %d (fail-on=%s)"
         % (len(reports), _worst(findings), c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], code, fail_on), ""]
    if degraded:
        L += ["> **DEGRADED** incomplete checks: %s. Result may be incomplete; not a clean pass." % ", ".join(degraded), ""]
    L += ["| Domain | Worst | Crit | High | Med | Low | Info | DNSSEC | Mail | TLS expires | HSTS |", "|---|---|---:|---:|---:|---:|---:|---|---|---|---|"]
    for r in reports:
        fs = [f for f in findings if f.evidence.get("domain") == r["domain"]]
        k, t = _sev_counts(fs), r.get("tls") or {}
        L.append("| `%s` | %s | %d | %d | %d | %d | %d | %s | %s | %s | %s |" % (
            r["domain"], _worst(fs), k["CRITICAL"], k["HIGH"], k["MEDIUM"], k["LOW"], k["INFO"], r.get("dnssec") or "?",
            "sends" if r["mail"].get("mails") else "no mail", "%s (%sd)" % ((t.get("not_after") or "?")[:10], t.get("days_remaining"))
            if t.get("not_after") else "-", _md((r.get("http") or {}).get("hsts") or "-")))
    for r in reports:
        d, dn = r["domain"], r["dns"]
        fs = bc.sort_findings([f for f in findings if f.evidence.get("domain") == d])
        L += ["", "## `%s`" % d, "", "### DNS", "", "| Record | Value |", "|---|---|",
              "| A | %s |" % _md(_j(dn.get("a"))), "| AAAA | %s |" % _md(_j(dn.get("aaaa"))),
              "| NS | %s |" % _md(_j(dn.get("ns"))), "| MX | %s |" % _md(_j(dn.get("mx"))),
              "| CAA | %s |" % _md(_j(dn.get("caa"))), "| DNSKEY / AD | %d / %s (%s) |" % (dn.get("dnskey_count", 0), dn.get("ad"), r.get("dnssec"))]
        for cn in r.get("cnames") or []:
            L.append("| CNAME `%s` | %s%s (status %s, resolves %s) |" % (cn["name"], _md(" -> ".join(cn["chain"])),
                                                                     " [%s]" % cn["provider"] if cn["provider"] else "", cn["status"], _yn(cn["resolves"])))
        spf, dm, dk = r.get("spf") or {}, r.get("dmarc") or {}, r.get("dkim") or {}
        L += ["", "### Email authentication", "", "| Check | Result |", "|---|---|",
              "| Sends mail | %s |" % _yn(r["mail"].get("mails")),
              "| SPF | %s |" % ("`%s` (%s DNS lookups, all=%s)" % (_md(spf["record"]), spf.get("lookups"), spf.get("all")) if spf.get("record") else "none"),
              "| DMARC | %s |" % ("`%s`" % _md(dm["record"]) if dm.get("record") else "none"),
              "| DKIM | %s (tried: %s) |" % (_md(_j(["%s (%s %s)" % (x["selector"], x["k"], "%s-bit" % x["bits"] if x["bits"] else "revoked" if x["revoked"] else "?")
                                                   for x in dk.get("found") or []])), _md(",".join(dk.get("probed") or []))),
              "| MTA-STS | %s |" % _md(r.get("mta_sts") or "-"), "| TLS-RPT | %s |" % _md(r.get("tls_rpt") or "-")]
        t = r.get("tls")
        L += ["", "### TLS (:443)", ""]
        if not t:
            L.append("_skipped_")
        elif t.get("error") and not t.get("issuer"):
            L.append("UNREACHABLE: %s" % _md(t["error"]))
        else:
            cph = t.get("cipher") or ["?", "?", 0]
            L += ["| Field | Value |", "|---|---|", "| Protocol / cipher | %s / %s (%s-bit) |" % (t.get("version"), cph[0], cph[2]),
                  "| Issuer | %s (%s) |" % (_md(t.get("issuer")), _md(t.get("issuer_cn"))), "| Subject CN | %s |" % _md(t.get("subject_cn")),
                  "| Valid | %s → %s (%s days left) |" % (t.get("not_before"), t.get("not_after"), t.get("days_remaining")),
                  "| Verified | %s%s |" % (_yn(t.get("verified")), " (%s)" % _md(t["verify_error"]) if t.get("verify_error") else ""),
                  "| Serial | `%s` |" % t.get("serial"), "| SHA-256 | `%s` |" % t.get("sha256"), "| SAN | %s |" % _md(_j(t.get("san")))]
        h = r.get("http")
        L += ["", "### HTTPS headers & port 80", ""]
        if not h:
            L.append("_skipped_")
        else:
            L += ["| Header | Value |", "|---|---|"]
            for k in ("strict-transport-security", "content-security-policy", "x-content-type-options", "referrer-policy",
                      "x-frame-options", "permissions-policy"):
                L.append("| `%s` | %s |" % (k, _md((h.get("headers") or {}).get(k, "**missing**"))))
            L.append("| port 80 | %s |" % _md((r.get("port80") or {}).get("status") or "-"))
        ct = r.get("ct")
        L += ["", "### Certificate Transparency", ""]
        if not ct:
            L.append("_skipped_")
        else:
            L.append("Source: %s · status **%s** · issuances %d · issuers %s · names: %s"
                     % (ct.get("source") or "-", ct["status"], ct["issuances"], _md(_j(ct["issuers"])), _md(_j(["`%s`" % n for n in ct["names"][:50]]))))
        L += ["", "### Findings", "", "| Severity | Code | Finding |", "|---|---|---|"]
        L += ["| %s | `%s` | %s |" % (f.sev, f.code, _md(f.msg.split(": ", 1)[-1])) for f in fs] or ["| - | - | no findings |"]
        rem = r.get("remediation") or {}
        if rem.get("dns") or rem.get("headers") or rem.get("notes"):
            L += ["", "### Copy-paste remediation", ""]
            if rem.get("dns"):
                L += ["DNS zone records (BIND syntax; in Cloudflare/Route 53 enter name, type and value):", "", "```dns"] + rem["dns"] + ["```", ""]
            if rem.get("headers"):
                L += ["Nginx (`server {}` block):", "", "```nginx"] + rem["nginx"] + ["```", "",
                      "Vercel (`vercel.json`):", "", "```json", rem["vercel_json"], "```", "",
                      "Netlify / Cloudflare Pages (`_headers`):", "", "```text"] + rem["netlify_headers"] + ["```", ""]
            L += ["- %s" % n for n in rem.get("notes") or []]
    if delta is not None:
        L += ["", "## Delta", "", "```"] + _delta_lines(delta)[2:] + ["```"]
    L += ["", "---", "", SCOPE_NOTE, ""]
    return "\n".join(L)


def _delta_json(delta):
    if delta is None:
        return None
    out = {}
    for d, x in delta.items():
        out[d] = {k: (v if k in ("baseline", "skipped") else [f.to_dict() for f in v]) for k, v in x.items()}
    return out


# ---------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = _Parser(prog="domain_infra_hygiene.py", description="Read-only domain, TLS & email-auth hygiene auditor")
    p.add_argument("domains", nargs="*", metavar="DOMAIN")
    p.add_argument("--domains-file", metavar="PATH")
    p.add_argument("--dkim-selector", "--dkim-selectors", dest="selectors", action="append", default=[], metavar="SEL[,SEL]")
    p.add_argument("--no-mail", action="store_true", help="assert the domain never sends mail (expects -all, p=reject, null MX)")
    p.add_argument("--no-http", action="store_true")
    p.add_argument("--no-tls", action="store_true")
    p.add_argument("--ct", dest="ct", action="store_true", default=True)
    p.add_argument("--no-ct", dest="ct", action="store_false")
    p.add_argument("--expect-ns", action="append", default=[], metavar="NS[,NS]")
    p.add_argument("--state", metavar="PATH")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--format", type=str.lower, choices=["text", "markdown", "md", "json"], default="text")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", metavar="PATH")
    p.add_argument("--fail-on", type=str.lower, choices=["none", "info", "low", "medium", "high", "critical"], default="medium")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--offline", metavar="PATH")
    p.add_argument("--no-color", action="store_true")
    return p


def run(argv, out=None, now=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        if args.delta and not args.state:
            raise UsageError("--delta requires --state PATH")
        if args.timeout <= 0:
            raise UsageError("--timeout must be positive")
        raw = list(args.domains)
        if args.domains_file:
            raw += read_domains_file(args.domains_file)
        if not raw:
            raise UsageError("no domains given (pass DOMAIN ... and/or --domains-file PATH)")
        domains = []
        for tok in raw:
            try:
                d = normalize_domain(tok)
            except DomainError as e:
                raise UsageError("invalid domain %r: %s" % (tok[:80], e)) from None
            if d not in domains:
                domains.append(d)
        if len(domains) > MAX_DOMAINS:
            raise UsageError("at most %d domains per run" % MAX_DOMAINS)
        selectors = split_tokens(args.selectors)
        for s in selectors:
            if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", s) or len(s) > 63:
                raise UsageError("invalid DKIM selector %r" % s[:40])
        expect_ns = split_tokens(args.expect_ns)
        fx = None
        if args.offline:
            try:
                with open(args.offline, "r", encoding="utf-8") as fh:
                    fx = json.load(fh)
            except (OSError, ValueError) as e:
                raise UsageError("cannot read --offline fixture: %s" % e) from None
            if not isinstance(fx, dict):
                raise UsageError("--offline fixture must be a JSON object")
    except UsageError as e:
        print("error: %s" % e, file=err)
        return bc.EXIT_USAGE
    except SystemExit as e:  # --help
        return int(e.code or 0)

    fmt = "json" if args.json else ("markdown" if args.format == "md" else args.format)
    if now is None and fx and fx.get("now"):
        now = parse_cert_time(fx["now"])
    now = now or bc.utc_now()
    src = Source(timeout=args.timeout, offline=fx)
    opts = {"selectors": list(dict.fromkeys(selectors)), "no_mail": args.no_mail, "no_tls": args.no_tls,
            "no_http": args.no_http, "ct": args.ct, "expect_ns": expect_ns}

    reports, findings, degraded, events_all, all_core_failed = [], [], [], [], True
    store = bc.StateStore(args.state) if args.state else None
    delta = {} if args.delta else None
    for d in domains:
        rep, A, core = audit_domain(src, d, opts, now)
        rep["degraded"] = sorted(set(A.degraded))
        rep["findings"] = [f.to_dict() for f in bc.sort_findings(A.findings)]
        rep["remediation"] = build_remediation(rep, A.findings)
        rep["worst"] = _worst(A.findings)
        degraded += ["%s:%s" % (d, x) for x in rep["degraded"]]
        if any(core.get(k) for k in ("dns", "tls", "http")):
            all_core_failed = False
        state = make_state(rep, A.findings)
        prev = store.get(d) if store else None
        incomplete = bool(rep["degraded"])
        if delta is not None:
            if prev is None:
                delta[d] = {"baseline": True, "events": [], "new": list(A.findings), "changed": [], "resolved": []}
            elif incomplete:
                delta[d] = {"baseline": False, "skipped": "degraded run: %s" % ", ".join(rep["degraded"]),
                            "events": [], "new": [], "changed": [], "resolved": []}
            else:
                dd = diff_findings(prev.get("findings", []), A.findings)
                ev = compute_events(prev, state, dd["NEW"])
                events_all += ev
                delta[d] = {"baseline": False, "events": ev, "new": dd["NEW"], "changed": dd["CHANGED"], "resolved": dd["RESOLVED"]}
        if store and not incomplete:
            store.put(d, state)
        reports.append(rep)
        findings += A.findings

    doh_down = src.doh_ok == 0 and src.doh_fail > 0
    code = compute_exit(findings + events_all, bool(degraded), args.fail_on, force_degraded=all_core_failed)
    if doh_down and "dns: all DoH resolvers unreachable" not in degraded:
        degraded.insert(0, "dns: all DoH resolvers unreachable (cloudflare-dns.com, dns.google)")
    endpoints = sorted(src.endpoints) if fx is None else ["offline:" + os.path.basename(args.offline)]
    header = bc.snapshot_header(TOOL, endpoints, src.fallbacks, now=now)
    if fmt == "json":
        c = _sev_counts(findings)
        payload = {"tool": TOOL, "version": VERSION, "retrieved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "domains": reports, "summary": {"domains": len(reports), "counts": c, "worst": _worst(findings),
                                                   "per_domain": {r["domain"]: r["worst"] for r in reports},
                                                   "events": [f.to_dict() for f in bc.sort_findings(events_all)]},
                   "delta": _delta_json(delta), "exit_code": code, "fail_on": args.fail_on, "degraded_sources": degraded,
                   "endpoints": endpoints, "fallbacks": list(src.fallbacks), "scope_note": SCOPE_NOTE}
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    elif fmt == "markdown":
        text = render_markdown(reports, findings, delta, code, degraded, header, args.fail_on)
    else:
        text = render_text(reports, findings, delta, code, degraded, header, args.fail_on)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as e:
            print("error: cannot write --out: %s" % e, file=err)
            return bc.EXIT_USAGE
        print("wrote %s (exit_code %d)" % (args.out, code), file=err)
    else:
        print(text, file=out)
    return code


def main():
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
