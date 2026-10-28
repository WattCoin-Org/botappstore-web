#!/usr/bin/env python3
"""SEC EDGAR Supply Chain & Counterparty Mapper v1.1 (read-only, 100% Python 3 stdlib).

Turns SEC primary-source filings (10-K/20-F, 8-K Item 1.01, 6-K, Exhibit 21, Schedule 13D/13G) into a
deterministic, cited relationship table, a concentration score, a year-over-year counterparty diff and a
self-contained dark-mode HTML graph.  Rule based: no model inference, no third-party sites.

    sec_supply_chain_mapper.py --tickers NVDA,TSLA --json
    sec_supply_chain_mapper.py --preset robotaxi --html-out map.html
    echo '{"preset":"robotaxi"}' | sec_supply_chain_mapper.py --stdin-json

v1.1 over v1.0: U1 disclosed-concentration scorer (CR-01..04), U2 YoY 10-K counterparty diff,
U3 Schedule 13D/13G equity-stake edges (EQ-01).
"""
import argparse
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

VERSION = "1.1.0"
TOOL = "sec-supply-chain-mapper"
SCHEMA = "sec-supply-chain-mapper/2"
RECEIPT_ENGINE_DIR = "/workspace/social-hub"
SEC_UA_FILE = "/workspace/.wattcoin_sec_ua"
DEFAULT_USER_AGENT = "WattCoinSupplyChainMapper/1.1 (https://botappstore.com; contact@wattcoin.org)"
CACHE_DIR = "/tmp/sec_mapper_cache"
CACHE_TTL_SEC = 12 * 3600
RUNS_ROOT = "/tmp/sec_mapper_runs"

ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "efts.sec.gov"})
MAX_REQUESTS = 80
MAX_ENTITIES = 10
MIN_REQ_INTERVAL = 0.22            # <= 4.5 req/s (hard limit 5)
TIMEOUT_SEC = 20
MAX_DOC_BYTES = 12 * 1024 * 1024    # annual reports with inline XBRL are large
MAX_INDEX_BYTES = 5 * 1024 * 1024
QUOTE_MAX = 300
MAX_EDGES_PER_FILING = 40
MAX_SUBSIDIARIES = 60
LOOKBACK_DEFAULT = 730

PRESETS = {
    "nvidia-physical-ai": ["NVDA", "TSLA", "MBLY", "AUR", "SYM"],
    "robotaxi": ["TSLA", "AUR", "PONY", "WRD", "MBLY"],
    "humanoid-robotics": ["TSLA", "RR", "SERV", "SYM", "OUST"],
}
ANNUAL_FORMS = ("10-K", "20-F")
ALL_FORMS = ("10-K", "20-F", "8-K", "6-K")
EQUITY_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
REL_TYPES = ("supplier", "customer", "foundry_compute", "strategic_partner", "subsidiary", "equity_stake")

SCOPE_NOTE = ("Primary-source SEC EDGAR filings only (www.sec.gov, data.sec.gov, efts.sec.gov). Relationships are extracted by fixed "
              "phrase rules from registrant statements and are not verified facts, ratings, price targets, investment advice or legal "
              "conclusions (for example about breach or exclusivity). Absence means 'Not found in queried primary sources': contracts below "
              "materiality, unfiled agreements and undisclosed suppliers are invisible. Exhibit 21 subsidiaries are ownership records and are "
              "never counted as suppliers or customers. Filing text is treated as untrusted data and is never executed or followed.")

NON_CORE_KEYS = {"retrieved_at_utc", "generated_at_utc", "request_stats", "audit_sha256"}


class MapperError(Exception):
    pass


# --------------------------------------------------------------------------- guarded HTTP client
class Fetcher:
    """HTTPS-only, allow-listed, IP-pinned (global addresses only), no redirects, size-capped, cached, rate-limited."""

    def __init__(self, max_requests=MAX_REQUESTS, cache_dir=CACHE_DIR, use_cache=True):
        self.max_requests, self.cache_dir, self.use_cache = max_requests, cache_dir, use_cache
        self.requests = self.cache_hits = 0
        self._last = 0.0
        self.user_agent = self._ua()

    @staticmethod
    def _ua():
        try:
            if os.path.isfile(SEC_UA_FILE) and not os.path.islink(SEC_UA_FILE):
                with open(SEC_UA_FILE, "r", encoding="utf-8") as fh:
                    ua = fh.read(256).strip()
                if 10 <= len(ua) <= 200 and "@" in ua and "\n" not in ua:
                    return ua
        except OSError:
            pass
        return DEFAULT_USER_AGENT

    @property
    def budget_left(self):
        return self.max_requests - self.requests

    @staticmethod
    def check_url(url):
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or host not in ALLOWED_HOSTS or u.username or u.password or (u.port not in (None, 443)):
            raise MapperError("url_not_allowed")
        return u, host

    def _cache_path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".bin")

    def get(self, url, max_bytes=MAX_DOC_BYTES):
        """Return response bytes; raises MapperError.  Cache hits are free; network calls count toward the 80-request cap."""
        u, host = self.check_url(url)
        path = self._cache_path(url)
        if self.use_cache:
            try:
                if time.time() - os.path.getmtime(path) < CACHE_TTL_SEC:
                    with open(path, "rb") as fh:
                        data = fh.read()
                    self.cache_hits += 1
                    return data
            except OSError:
                pass
        if self.requests >= self.max_requests:
            raise MapperError("request_budget_exhausted")
        wait = MIN_REQ_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        data = self._fetch(u, host, max_bytes)
        if self.use_cache:
            try:
                os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            except OSError:
                pass
        return data

    def get_json(self, url, max_bytes=MAX_INDEX_BYTES):
        try:
            return json.loads(self.get(url, max_bytes).decode("utf-8", "replace"))
        except ValueError:
            raise MapperError("bad_json") from None

    def _fetch(self, u, host, max_bytes):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise MapperError("dns_failure") from None
        ips = sorted({i[4][0] for i in infos})
        if not ips or any(not _ip_ok(ip) for ip in ips):
            raise MapperError("non_global_address")
        ctx = ssl.create_default_context()
        conn = _PinnedConnection(host, ips[0], ctx, TIMEOUT_SEC)
        try:
            conn.request("GET", u.path + (("?" + u.query) if u.query else ""),
                         headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity", "Accept": "*/*", "Host": host})
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                raise MapperError("redirect_refused")
            if resp.status != 200:
                raise MapperError(f"http_{resp.status}")
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise MapperError("response_too_large")
            return data
        except (OSError, http.client.HTTPException):
            raise MapperError("network_error") from None
        finally:
            conn.close()


_EGRESS_PROXY_NET = ipaddress.ip_network("198.18.0.0/15")  # RFC 2544 range used by some sandbox egress interceptors


def _ip_ok(ip):
    """Globally routable only.  Operators behind a transparent egress interceptor may opt in to 198.18.0.0/15 (and nothing else)."""
    a = ipaddress.ip_address(ip)
    if a.is_global:
        return True
    return os.environ.get("SEC_MAPPER_ALLOW_EGRESS_PROXY_NET") == "1" and a in _EGRESS_PROXY_NET


class _PinnedConnection(http.client.HTTPSConnection):
    """Connect to a pre-validated IP while presenting the original hostname for SNI / certificate checks."""

    def __init__(self, host, ip, context, timeout):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


# --------------------------------------------------------------------------- EDGAR helpers
def cik10(cik):
    return str(int(cik)).zfill(10)


def archive_url(cik, accession, doc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"


def resolve_entities(fetcher, tokens):
    """tickers or 10-digit CIKs -> [{ticker, cik, name}] (company_tickers.json), order preserved, duplicates dropped."""
    tickers = fetcher.get_json("https://www.sec.gov/files/company_tickers.json")
    by_ticker, by_cik = {}, {}
    for row in (tickers.values() if isinstance(tickers, dict) else []):
        try:
            c = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        by_ticker.setdefault(str(row.get("ticker", "")).upper(), {"ticker": str(row.get("ticker", "")).upper(), "cik": cik10(c), "name": str(row.get("title", ""))})
        by_cik.setdefault(c, by_ticker[str(row.get("ticker", "")).upper()])
    out, errors, seen = [], [], set()
    for t in tokens:
        t = t.strip().upper()
        ent = by_cik.get(int(t)) if t.isdigit() and len(t) <= 10 else by_ticker.get(t)
        if not ent:
            errors.append({"input": t, "error": "not_found_in_company_tickers"})
        elif ent["cik"] not in seen:
            seen.add(ent["cik"])
            out.append(dict(ent))
    return out, errors


def recent_filings(sub):
    r = (sub.get("filings") or {}).get("recent") or {}
    keys = ("accessionNumber", "filingDate", "form", "primaryDocument", "items")
    cols = [r.get(k) or [] for k in keys]
    rows = []
    for i in range(min((len(c) for c in cols), default=0)):
        rows.append({"accession": cols[0][i], "date": cols[1][i], "form": cols[2][i], "doc": cols[3][i], "items": cols[4][i] or ""})
    return rows


# --------------------------------------------------------------------------- text extraction
_TAG_RE = re.compile(r"<[^>]+>")
_DROP_RE = re.compile(r"<(script|style|ix:header)\b.*?</\1>", re.I | re.S)
ABBREV = ("Inc.", "Corp.", "Co.", "Ltd.", "U.S.", "S.A.", "N.V.", "No.", "Mr.", "Dr.", "vs.", "St.", "L.P.", "L.L.C.", "Jr.", "e.g.", "i.e.", "etc.")


def html_to_text(raw):
    s = _DROP_RE.sub(" ", raw)
    s = re.sub(r"</(p|div|tr|li|h\d|br)\s*>|<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]\s*>", " | ", s, flags=re.I)
    s = html.unescape(_TAG_RE.sub(" ", s)).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*", "\n", s)).strip()


def split_sentences(text):
    t = text
    for i, ab in enumerate(ABBREV):
        t = t.replace(ab, ab.replace(".", f"\x00{i}\x01"))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"“(])|\n+", t)
    out = []
    for p in parts:
        p = re.sub(r"\x00(\d+)\x01", lambda m: ".", p)
        p = re.sub(r"\s+", " ", p).strip()
        if 40 <= len(p) <= 1800:
            out.append(p)
    return out


SECTION_TITLES_10K = {"1": "Item 1 Business", "1A": "Item 1A Risk Factors", "7": "Item 7 MD&A", "8": "Item 8 Financial Statements and Notes",
                      "1C": "Item 1C Cybersecurity", "2": "Item 2 Properties", "15": "Item 15 Exhibits"}
SECTION_TITLES_20F = {"3": "Item 3 Key Information (Risk Factors)", "4": "Item 4 Information on the Company", "5": "Item 5 Operating and Financial Review",
                      "18": "Item 18 Financial Statements", "19": "Item 19 Exhibits"}
_ITEM_RE = re.compile(r"(?im)^\s*item\s+(\d{1,2}[A-C]?)\s*[.:\-–—]")


def split_sections(text, form):
    """[(title, start, end)] for the principal Items; table-of-contents hits (tiny spans) are discarded."""
    titles = SECTION_TITLES_20F if form == "20-F" else SECTION_TITLES_10K
    marks = [(m.start(), m.group(1).upper()) for m in _ITEM_RE.finditer(text)]
    spans = []
    for i, (pos, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if num in titles and end - pos > 1500:
            spans.append((titles[num], pos, end))
    return spans or [("Full text", 0, len(text))]


# Rule table: ordered; the first matching rule decides relationship_type for a sentence.
REL_RULES = (
    ("RL-FOUNDRY", "foundry_compute", re.compile(r"\b(foundr(?:y|ies)|wafer|fabricat\w+|cloud (?:computing|infrastructure|services|platform)|data cent(?:er|re)s?|compute capacity|contract manufactur\w+|assembly and test)\b", re.I)),
    ("RL-SUPPLIER", "supplier", re.compile(r"\b(we rely (?:on|upon)|rel(?:y|ies|iance) (?:on|upon)|depend(?:s|ent)? (?:on|upon)|sole[- ]source|single[- ]source|sole supplier|single supplier|supplied by|suppliers?|vendors?|manufactured by|purchase[sd]? (?:\w+ ){0,4}from|procure\w* (?:\w+ ){0,4}from)\b", re.I)),
    ("RL-CUSTOMER", "customer", re.compile(r"\b(customers?|accounted for \d|distributors?|resellers?|sales to|revenue from|offtake|OEMs?)\b", re.I)),
    ("RL-PARTNER", "strategic_partner", re.compile(r"\b(strategic (?:partnership|alliance|relationship|collaboration|investment)|collaborat\w+|joint venture|partner(?:ed|ship|s)?(?: with)?|master (?:services|supply|purchase) agreement|(?:license|licensing|development|supply|purchase|offtake|commercial) agreement|entered into (?:a|an) [\w\s,-]{0,60}agreement)\b", re.I)),
)
LEXICON = (
    "Taiwan Semiconductor Manufacturing Company", "Taiwan Semiconductor", "TSMC", "Samsung Electronics", "Samsung", "SK Hynix", "SK hynix", "Micron",
    "GlobalFoundries", "Intel", "NVIDIA", "Advanced Micro Devices", "AMD", "Qualcomm", "Broadcom", "Texas Instruments", "STMicroelectronics",
    "Infineon", "NXP", "ON Semiconductor", "Analog Devices", "Amazon Web Services", "AWS", "Microsoft Azure", "Microsoft", "Google Cloud", "Oracle",
    "CoreWeave", "Foxconn", "Hon Hai", "Flex Ltd", "Jabil", "Wistron", "Quanta", "Pegatron", "Panasonic", "LG Energy Solution", "CATL", "Contemporary Amperex",
    "Toyota", "Volkswagen", "Uber", "Lyft", "Continental AG", "Magna", "Aptiv", "Bosch", "ZF", "Hyundai", "Kia", "Ford", "General Motors",
    "Luminar", "Hesai", "Ouster", "Velodyne", "Innoviz", "Sony", "Valeo", "Denso", "Baidu", "Geely", "Mercedes-Benz", "BMW", "Stellantis",
    "Super Micro Computer", "Dell", "Hewlett Packard Enterprise", "Cisco", "Meta Platforms", "Apple", "Tesla", "Mobileye", "Aurora Innovation",
    "SoundHound", "Serve Robotics", "Rolls-Royce", "Nissan", "Honda", "Renault", "Lucid", "Rivian", "Waymo", "WeRide", "Pony.ai", "Pony AI",
)
LEXICON_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(x) for x in sorted(LEXICON, key=len, reverse=True)) + r")(?![A-Za-z0-9])")
NAME_STOP = {"The", "Our", "We", "In", "Item", "Risk", "Company", "Companies", "Such", "These", "This", "That", "Certain", "Other", "Any", "Under", "As", "If", "For",
             "GAAP", "SEC", "U.S.", "US", "USD", "FASB", "IRS", "EU", "AI", "IP", "OEM", "OEMs", "ASC", "FTC", "DOJ", "NHTSA", "COVID", "ESG", "ITEM", "Annual",
             "Form", "Note", "Notes", "Table", "Part", "Section", "Exhibit", "Amendment", "Board", "Common", "Class", "United", "States", "China", "Chinese", "Taiwan"}
NAME_SUFFIX_RE = re.compile(r"((?:[A-Z][\w&.\-]*\s+){0,5}[A-Z][\w&.\-]*)\s?,?\s+(Inc|Corp|Corporation|Ltd|Limited|LLC|L\.L\.C|Co|Company|plc|PLC|N\.V|S\.A|AG|GmbH|SE)\b\.?")
ALIASES = {"tsmc": "Taiwan Semiconductor Manufacturing Company", "taiwan semiconductor": "Taiwan Semiconductor Manufacturing Company",
           "aws": "Amazon Web Services", "catl": "Contemporary Amperex Technology", "contemporary amperex": "Contemporary Amperex Technology",
           "sk hynix": "SK Hynix", "hon hai": "Hon Hai Precision Industry", "pony ai": "Pony.ai", "advanced micro devices": "AMD"}
COMPETITION_RE = re.compile(r"\b(competit\w+|compete[sd]?|rival)\b", re.I)


def canonical(name):
    """Alias + corporate-suffix normalisation so 'TSMC' / 'Taiwan Semiconductor Manufacturing Company Limited' collapse to one counterparty."""
    base = re.sub(r"[\s,]+(?:Inc|Corp|Corporation|Ltd|Limited|LLC|Co|Company|plc|PLC|N\.V|S\.A|AG|GmbH|SE)\.?$", "", name.strip(), flags=re.I).strip(" ,.")
    low = base.lower()
    for k, v in ALIASES.items():
        if low == k or low.startswith(k + " "):
            return v
    return base


def clean_name(n):
    return re.sub(r"[\s,;:.]+$", "", n.strip())


def counterparties_in(sentence, own_names):
    """Lexicon hits plus corporate-suffix names (Inc/Corp/Ltd/...); own-company names, stop words and competitor lists removed."""
    found = {}
    for m in LEXICON_RE.finditer(sentence):
        c = canonical(m.group(1))
        found.setdefault(c.lower(), c)
    for m in NAME_SUFFIX_RE.finditer(sentence):
        words = clean_name(m.group(1)).split()
        while words and (words[0] in NAME_STOP or not words[0][0].isupper()):
            words = words[1:]
        if not words:
            continue
        c = canonical(" ".join(words) + " " + m.group(2))
        found.setdefault(c.lower(), c)
    own = {canonical(o).lower() for o in own_names if o}
    out = []
    for k, v in sorted(found.items()):
        if any(k == o or k.startswith(o + " ") or o.startswith(k + " ") for o in own):
            continue
        if any(k != k2 and k in k2 for k2 in found):
            continue
        out.append(v)
    return out


def own_names_for(ent):
    name = ent["name"]
    short = re.sub(r"[\s,.]*(?:/[A-Z]+/)?\s*(?:INC|CORP|CORPORATION|CO|LTD|PLC|HOLDINGS|N\.V\.)\.?$", "", name, flags=re.I).strip()
    return {ent["ticker"], name, short, short.title(), name.title()}


def quote_for(sentence, needle):
    """Verbatim <=300-char window of the sentence that contains `needle` when possible."""
    s = " ".join(sentence.split())
    if len(s) <= QUOTE_MAX:
        return s
    pos = max(0, s.find(needle))
    start = max(0, min(pos - QUOTE_MAX // 3, len(s) - QUOTE_MAX))
    return s[start:start + QUOTE_MAX].strip()


def extract_edges(text, form, filing, ent, section_spans=None):
    """Rule-based supplier/customer/foundry_compute/strategic_partner edges for one filing's main document."""
    own = own_names_for(ent)
    edges, seen = [], set()
    for title, a, b in (section_spans or split_sections(text, form)):
        for sent in split_sentences(text[a:b]):
            rule = next(((rid, rt) for rid, rt, rx in REL_RULES if rx.search(sent)), None)
            if not rule or (COMPETITION_RE.search(sent) and rule[1] != "strategic_partner"):
                continue
            for name in counterparties_in(sent, own):
                key = (name.lower(), rule[1], title)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(make_edge(ent, name, rule[1], rule[0], filing, title, quote_for(sent, name)))
                if len(edges) >= MAX_EDGES_PER_FILING:
                    return edges
    return edges


def make_edge(ent, counterparty, rtype, rule_id, filing, section, quote, **extra):
    e = {"source_entity": {"name": ent["name"], "cik": ent["cik"], "ticker": ent["ticker"]}, "counterparty": counterparty,
         "relationship_type": rtype, "rule_id": rule_id, "accession_number": filing["accession"], "form": filing["form"],
         "filing_date": filing["date"], "section": section, "source_url": filing["url"], "verbatim_quote": quote[:QUOTE_MAX]}
    e.update(extra)
    return e


# --------------------------------------------------------------------------- Exhibit 21 (subsidiaries only)
def find_ex21_doc(index_json):
    items = ((index_json.get("directory") or {}).get("item")) or []
    names = [i.get("name", "") for i in items if isinstance(i, dict)]
    for n in sorted(names):
        if re.search(r"(?i)ex[-_]?21", n) and re.search(r"(?i)\.(htm|html|txt)$", n):
            return n
    return None


def parse_exhibit21(raw, ent, filing):
    """Rows of the subsidiary table -> relationship_type 'subsidiary' edges (section 'Exhibit 21')."""
    edges, seen = [], set()
    for line in html_to_text(raw).split("\n"):
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if not cells:
            continue
        name = cells[0]
        if len(name) < 3 or len(name) > 120 or re.search(r"(?i)^(subsidiar|name of|jurisdiction|exhibit|list of|state or|\(?\d+\)?$)", name):
            continue
        if not re.search(r"[A-Za-z]{3}", name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        edges.append(make_edge(ent, name, "subsidiary", "EX-21", filing, "Exhibit 21", " | ".join(cells)[:QUOTE_MAX],
                               jurisdiction=cells[1][:80] if len(cells) > 1 else None))
        if len(edges) >= MAX_SUBSIDIARIES:
            break
    return edges


# --------------------------------------------------------------------------- U1 concentration scorer (CR-01..04)
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?(?:%|percent\b)")
CONC_CTX_RE = re.compile(r"\b(customers?|suppliers?|vendors?|distributors?|resellers?|direct customer)\b", re.I)
CONC_VERB_RE = re.compile(r"\b(accounted for|account for|represent(?:ed|ing|s)?|comprised?|concentrat\w+|derived|generated|contributed|made up)\b", re.I)
SOLE_RE = re.compile(r"\b(sole[- ]source|single[- ]source|sole supplier|single supplier|only (?:one )?(?:supplier|source)|single vendor|sole vendor)\b", re.I)
IX_NONFRAC_RE = re.compile(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", re.I | re.S)
IX_CONTEXT_RE = re.compile(r"<xbrli:context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</xbrli:context>", re.I | re.S)


def _attr(attrs, name):
    m = re.search(r"\b" + name + r"=\"([^\"]*)\"", attrs, re.I)
    return m.group(1) if m else None


def ixbrl_concentrations(raw):
    """[(pct, dimension_member, kind)] from ix:nonFraction ConcentrationRiskPercentage facts with a customer/supplier axis."""
    ctx = {}
    for cid, body in IX_CONTEXT_RE.findall(raw):
        members = re.findall(r"<xbrldi:explicitMember[^>]*dimension=\"([^\"]+)\"[^>]*>([^<]+)<", body, re.I)
        ctx[cid] = members
    out = []
    for attrs, inner in IX_NONFRAC_RE.findall(raw):
        name = _attr(attrs, "name") or ""
        if "ConcentrationRiskPercentage" not in name:
            continue
        val = re.sub(r"[^0-9.]", "", _TAG_RE.sub("", inner))
        try:
            v = float(val)
        except ValueError:
            continue
        pct = v if (_attr(attrs, "scale") == "-2" or v > 1) else v * 100
        members = ctx.get(_attr(attrs, "contextRef") or "", [])
        axis = " ".join(d for d, _ in members)
        kind = "supplier" if re.search(r"Supplier|Vendor", axis + name, re.I) else "customer"
        if members or "Customer" in name or "Supplier" in name:
            out.append((round(pct, 2), "; ".join(m.strip() for _, m in members), kind))
    return out


def concentration_items(text, raw, filing, section_spans):
    """CR-01..CR-04 items from inline-XBRL facts and filing text.  Never reads Exhibit 21."""
    items, seen = [], set()

    def add(rule, sev, kind, pct, quote, section, method, label):
        label = " ".join(label.replace("-", " ").split())
        if method == "inline_xbrl":
            m = [x.split(":")[-1] for x in label.split("; ") if re.search(r"Customer(One|Two|Three|[A-Z0-9])|Supplier", x.split(":")[-1])]
            label = (m or [kind])[0].replace("Member", "")
        key = (kind, pct) if pct is not None else (rule, kind, label)
        if key in seen:
            return
        seen.add(key)
        items.append({"rule_id": rule, "severity": sev, "kind": kind, "percentage": pct, "label": label, "method": method,
                      "accession_number": filing["accession"], "form": filing["form"], "filing_date": filing["date"],
                      "section": section, "source_url": filing["url"], "verbatim_quote": quote[:QUOTE_MAX]})

    def rule_for(p):
        return ("CR-03", "CRITICAL") if p >= 50 else ("CR-02", "WARN") if p >= 25 else ("CR-01", "INFO")

    for pct, member, kind in ixbrl_concentrations(raw):
        if pct >= 10:
            rid, sev = rule_for(pct)
            add(rid, sev, kind, pct, f"ix:nonFraction ConcentrationRiskPercentage {pct}% ({member or kind})", "Item 8 Financial Statements and Notes", "inline_xbrl", member or kind)
    for title, a, b in section_spans:
        for sent in split_sentences(text[a:b]):
            if CONC_CTX_RE.search(sent) and CONC_VERB_RE.search(sent):
                kind = "supplier" if re.search(r"supplier|vendor", sent, re.I) and not re.search(r"customer", sent, re.I) else "customer"
                label = (re.search(r"\b(Customer [A-Z0-9]\b)", sent) or LEXICON_RE.search(sent))
                label = label.group(1) if label else kind
                for m in PCT_RE.finditer(sent):
                    p = float(m.group(1))
                    if 10 <= p <= 100:
                        rid, sev = rule_for(p)
                        add(rid, sev, kind, p, quote_for(sent, m.group(0)), title, "text", label)
            if SOLE_RE.search(sent):
                add("CR-04", "WARN", "supplier", None, quote_for(sent, SOLE_RE.search(sent).group(0)), title, "text", SOLE_RE.search(sent).group(0).lower())
    items.sort(key=lambda i: (i["rule_id"], i["kind"], -(i["percentage"] or 0), i["label"]))
    return items


def concentration_score(items):
    """0-100: max disclosed % + 10 per additional >=10% disclosure (max +20) + 15 if any sole/single-source supplier."""
    pcts = sorted((i["percentage"] for i in items if i["percentage"] is not None), reverse=True)
    if not pcts and not any(i["rule_id"] == "CR-04" for i in items):
        return 0
    score = (pcts[0] if pcts else 0) + min(20, 10 * max(0, len(pcts) - 1)) + (15 if any(i["rule_id"] == "CR-04" for i in items) else 0)
    return int(min(100, round(score)))


# --------------------------------------------------------------------------- U2 YoY diff
def yoy_diff(ent, latest, prior, edges_latest, edges_prior, conc_latest, conc_prior):
    """ADDED / REMOVED counterparties (non-subsidiary) and PCT_CHANGED concentration disclosures between two annual reports."""
    base = {"entity": ent["ticker"], "latest_accession_number": latest["accession"], "prior_accession_number": prior["accession"],
            "latest_filing_date": latest["date"], "prior_filing_date": prior["date"]}
    key = lambda e: (e["counterparty"].lower(), e["relationship_type"])  # noqa: E731
    cur, old = {key(e): e for e in edges_latest}, {key(e): e for e in edges_prior}
    out = []
    for k in sorted(set(cur) - set(old)):
        out.append(dict(base, change="ADDED", counterparty=cur[k]["counterparty"], relationship_type=k[1], verbatim_quote=cur[k]["verbatim_quote"]))
    for k in sorted(set(old) - set(cur)):
        out.append(dict(base, change="REMOVED", counterparty=old[k]["counterparty"], relationship_type=k[1], verbatim_quote=old[k]["verbatim_quote"]))
    ckey = lambda i: (i["kind"], i["label"].lower())  # noqa: E731
    cc = {ckey(i): i for i in conc_latest if i["percentage"] is not None}
    co = {ckey(i): i for i in conc_prior if i["percentage"] is not None}
    for k in sorted(set(cc) & set(co)):
        if cc[k]["percentage"] != co[k]["percentage"]:
            out.append(dict(base, change="PCT_CHANGED", counterparty=cc[k]["label"], relationship_type=k[0],
                            pct_prior=co[k]["percentage"], pct_latest=cc[k]["percentage"],
                            pct_delta=round(cc[k]["percentage"] - co[k]["percentage"], 2), verbatim_quote=cc[k]["verbatim_quote"]))
    return out


# --------------------------------------------------------------------------- U3 Schedule 13D/13G
ORG_HINT_RE = re.compile(r"\b(Inc|Corp|Corporation|Ltd|Limited|LLC|LP|L\.P|Group|Management|Capital|Trust|Fund|Funds|Partners|Holdings|Advisors|Advisers|Bank|Company|"
                         r"Investments?|Asset|N\.V|plc|PLC|S\.A|AG|GmbH|SE|Financial|Associates|Technologies|Systems)\b")


def parse_13dg(raw, ent, filing):
    """Equity-stake edge from a Schedule 13D/13G primary document (XML after Dec-2024, text/HTML before)."""
    txt = html_to_text(raw)
    issuer = re.search(r"<(?:\w+:)?issuerName>\s*([^<]+?)\s*</(?:\w+:)?issuerName>", raw) or re.search(r"(?i)name of issuer[:\s|]+([^\n|]{3,100})", txt)
    person = re.search(r"<(?:\w+:)?reportingPersonName>\s*([^<]+?)\s*</(?:\w+:)?reportingPersonName>", raw) or re.search(r"(?i)names? of reporting persons?[^\n]*?[:\s|]+([^\n|]{3,100})", txt)
    pct = re.search(r"<(?:\w+:)?classPercent>\s*([\d.]+)\s*</(?:\w+:)?classPercent>", raw) or re.search(r"(?i)percent of class[^0-9\n]{0,60}([\d.]+)", txt)
    if not issuer or not person:
        return None
    issuer_n, person_n = " ".join(issuer.group(1).split()), " ".join(person.group(1).split())
    if not ORG_HINT_RE.search(person_n) and not ORG_HINT_RE.search(issuer_n):
        return None
    own = {o.lower() for o in own_names_for(ent)}
    if not ORG_HINT_RE.search(person_n) and any(o and o in issuer_n.lower() for o in own):
        return None  # reporting person looks like an individual: no personal data on individuals
    holder_is_entity = any(o and (o in person_n.lower() or person_n.lower() in o) for o in own)
    cp, direction = (issuer_n, "holds") if holder_is_entity else (person_n, "held_by")
    snippet = f"Issuer: {issuer_n}; Reporting person: {person_n}" + (f"; Percent of class: {pct.group(1)}" if pct else "")
    anchor = txt.find(person_n)
    quote = " ".join(txt[max(0, anchor - 40):anchor + 200].split()) if anchor >= 0 else snippet
    return make_edge(ent, cp, "equity_stake", "EQ-01", filing, "Schedule " + ("13D" if "13D" in filing["form"] else "13G"),
                     (quote if issuer_n in quote or person_n in quote else snippet)[:QUOTE_MAX],
                     direction=direction, percent_of_class=float(pct.group(1)) if pct else None)


# --------------------------------------------------------------------------- orchestration
def build_report(fetcher, tokens, lookback_days=LOOKBACK_DEFAULT, forms=ALL_FORMS, today=None):
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=lookback_days)).isoformat()
    errors, skipped = [], []
    ents, e = resolve_entities(fetcher, tokens)
    errors += e
    state = {}

    def guarded(label, fn):
        try:
            return fn()
        except MapperError as ex:
            (skipped if str(ex) == "request_budget_exhausted" else errors).append({"step": label, "error": str(ex)})
            return None

    # phase 1: submissions + latest annual report (core), for every entity before optional phases spend budget
    for ent in ents:
        st = state[ent["ticker"]] = {"ent": ent, "edges": [], "conc": [], "conc_prior": [], "edges_prior": [], "filings": [], "yoy": [], "annual": None}
        sub = guarded(f"{ent['ticker']}:submissions", lambda: fetcher.get_json(f"https://data.sec.gov/submissions/CIK{ent['cik']}.json"))
        if not sub:
            continue
        ent["name"] = sub.get("name") or ent["name"]
        rows = recent_filings(sub)
        for r in rows:
            r["url"] = archive_url(ent["cik"], r["accession"], r["doc"])
        st["rows"] = rows
        annual = [r for r in rows if r["form"] in ANNUAL_FORMS and r["form"] in forms]
        st["annual"] = annual
        if annual:
            st["latest"] = annual[0]

    def process_annual(ent, st, filing, slot):
        raw = guarded(f"{ent['ticker']}:{filing['form']}:{filing['accession']}", lambda: fetcher.get(filing["url"]).decode("utf-8", "replace"))
        if raw is None:
            return
        text = html_to_text(raw)
        spans = split_sections(text, filing["form"])
        edges = extract_edges(text, filing["form"], filing, ent, spans)
        conc = concentration_items(text, raw, filing, spans)
        st["edges" if slot == "latest" else "edges_prior"] = edges
        st["conc" if slot == "latest" else "conc_prior"] = conc
        st["filings"].append({"accession_number": filing["accession"], "form": filing["form"], "filing_date": filing["date"], "role": slot, "source_url": filing["url"]})

    for ent in ents:
        st = state[ent["ticker"]]
        if st.get("latest"):
            process_annual(ent, st, st["latest"], "latest")
    # phase 2: Exhibit 21 (subsidiary-only) for the latest annual report
    for ent in ents:
        st = state[ent["ticker"]]
        lat = st.get("latest")
        if not lat:
            continue
        idx = guarded(f"{ent['ticker']}:filing_index", lambda: fetcher.get_json(archive_url(ent["cik"], lat["accession"], "index.json")))
        name = find_ex21_doc(idx) if idx else None
        if name:
            f21 = dict(lat, doc=name, url=archive_url(ent["cik"], lat["accession"], name))
            raw = guarded(f"{ent['ticker']}:ex21", lambda: fetcher.get(f21["url"]).decode("utf-8", "replace"))
            if raw:
                st["subs"] = parse_exhibit21(raw, ent, f21)
        elif idx is not None:
            skipped.append({"step": f"{ent['ticker']}:ex21", "error": "exhibit_21_not_found_in_index"})
    # phase 3: prior annual report -> U2 diff
    for ent in ents:
        st = state[ent["ticker"]]
        if st.get("latest") and len(st["annual"]) >= 2:
            prior = next((r for r in st["annual"][1:] if r["form"] == st["latest"]["form"]), None)
            if prior:
                process_annual(ent, st, prior, "prior")
                if st["filings"] and any(f["role"] == "prior" for f in st["filings"]):
                    st["yoy"] = yoy_diff(ent, st["latest"], prior, st["edges"], st["edges_prior"], st["conc"], st["conc_prior"])
        elif st.get("latest"):
            skipped.append({"step": f"{ent['ticker']}:yoy", "error": "prior_annual_report_not_in_submissions_window"})
    # phase 4: 8-K Item 1.01 / 6-K in the lookback window
    for ent in ents:
        st = state[ent["ticker"]]
        cands = [r for r in st.get("rows", []) if r["date"] >= cutoff and ((r["form"] == "8-K" and "1.01" in r["items"].split(",") and "8-K" in forms)
                                                                         or (r["form"] == "6-K" and "6-K" in forms))][:2]
        for filing in cands:
            raw = guarded(f"{ent['ticker']}:{filing['form']}:{filing['accession']}", lambda: fetcher.get(filing["url"]).decode("utf-8", "replace"))
            if raw is None:
                continue
            text = html_to_text(raw)
            sec = "Item 1.01 Entry into a Material Definitive Agreement" if filing["form"] == "8-K" else "6-K announcement"
            st["edges"] += extract_edges(text, filing["form"], filing, ent, [(sec, 0, len(text))])
            st["conc"] += [i for i in concentration_items(text, raw, filing, [(sec, 0, len(text))]) if i["rule_id"] != "CR-04"]
            st["filings"].append({"accession_number": filing["accession"], "form": filing["form"], "filing_date": filing["date"], "role": "agreement", "source_url": filing["url"]})
    # phase 5: U3 Schedule 13D/13G
    for ent in ents:
        st = state[ent["ticker"]]
        cands = [r for r in st.get("rows", []) if r["form"] in EQUITY_FORMS and r["date"] >= cutoff][:3]
        for filing in cands:
            xml_doc = filing["doc"].split("/")[-1] if filing["doc"].endswith(".xml") else filing["doc"]
            f2 = dict(filing, url=archive_url(ent["cik"], filing["accession"], xml_doc))
            raw = guarded(f"{ent['ticker']}:{filing['form']}:{filing['accession']}", lambda: fetcher.get(f2["url"]).decode("utf-8", "replace"))
            if raw is None:
                continue
            edge = parse_13dg(raw, ent, f2)
            if edge:
                st["edges"].append(edge)
                st["filings"].append({"accession_number": filing["accession"], "form": filing["form"], "filing_date": filing["date"], "role": "equity_filing", "source_url": f2["url"]})
            else:
                skipped.append({"step": f"{ent['ticker']}:{filing['accession']}", "error": "13dg_issuer_or_holder_not_parseable"})
    # phase 6: EFTS cross-filer confirmation for the first few named counterparties (budget permitting)
    efts = {}
    names = []
    for ent in ents:
        for ed in state[ent["ticker"]]["edges"]:
            if ed["relationship_type"] in ("supplier", "customer", "foundry_compute") and ed["counterparty"] not in names:
                names.append(ed["counterparty"])
    for n in names[:3]:
        if fetcher.budget_left <= 0:
            skipped.append({"step": "efts", "error": "request_budget_exhausted"})
            break
        res = guarded(f"efts:{n}", lambda: fetcher.get_json("https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(f'"{n}"') + "&forms=10-K"))
        if isinstance(res, dict):
            efts[n] = ((res.get("hits") or {}).get("total") or {}).get("value")

    return assemble(ents, state, efts, errors, skipped, lookback_days, forms, fetcher)


def assemble(ents, state, efts, errors, skipped, lookback_days, forms, fetcher):
    rows, conc_out, yoy_out, filings = [], {}, [], []
    for ent in ents:
        st = state[ent["ticker"]]
        rows += st["edges"] + st.get("subs", [])
        score = concentration_score([c for c in st["conc"] if c["accession_number"] == (st.get("latest") or {}).get("accession")] or st["conc"])
        conc_out[ent["ticker"]] = {"concentration_score": score, "items": st["conc"]}
        yoy_out += st["yoy"]
        filings += st["filings"]
    rows.sort(key=lambda r: (r["source_entity"]["ticker"], r["accession_number"], r["relationship_type"], r["counterparty"].lower(), r["section"]))
    for i, r in enumerate(rows, 1):
        r["row_id"] = f"R{i:04d}"
        r["retrieved_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nodes = {}
    for ent in ents:
        nodes[ent["cik"]] = {"id": ent["cik"], "label": ent["name"], "cik": ent["cik"], "ticker": ent["ticker"], "kind": "filer"}
    edges = []
    for r in rows:
        nid = "cp:" + r["counterparty"].lower()
        nodes.setdefault(nid, {"id": nid, "label": r["counterparty"], "kind": "counterparty"})
        if r["counterparty"] in efts:
            nodes[nid]["efts_10k_hits"] = efts[r["counterparty"]]
        src, dst = r["source_entity"]["cik"], nid
        if r.get("direction") == "held_by":
            src, dst = nid, r["source_entity"]["cik"]
        edges.append({"source": src, "target": dst, "type": r["relationship_type"], "rule_id": r["rule_id"], "accession_number": r["accession_number"],
                      "quote": r["verbatim_quote"], "source_url": r["source_url"]})
    payload = {
        "schema": SCHEMA, "tool": TOOL, "version": VERSION,
        "parameters": {"lookback_days": lookback_days, "forms": list(forms)},
        "entities": [{"ticker": e["ticker"], "cik": e["cik"], "name": e["name"]} for e in ents],
        "filings_read": sorted(filings, key=lambda f: (f["accession_number"], f["role"])),
        "rows": rows,
        "summary": {"edges_total": len(rows),
                    "core_edges_total": sum(1 for r in rows if r["relationship_type"] not in ("subsidiary", "equity_stake")),
                    "by_type": {t: sum(1 for r in rows if r["relationship_type"] == t) for t in REL_TYPES},
                    "max_concentration_score": max([c["concentration_score"] for c in conc_out.values()] or [0]),
                    "concentration_score_max": 100,
                    "yoy_changes": len(yoy_out)},
        "concentration": conc_out,
        "yoy_diff": yoy_out,
        "graph": {"nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": edges},
        "errors": errors, "skipped": sorted(skipped, key=lambda s: (s["step"], s["error"])),
        "scope_note": SCOPE_NOTE,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_stats": {"requests_made": fetcher.requests, "cache_hits": fetcher.cache_hits, "max_requests": fetcher.max_requests},
    }
    payload["audit_sha256"] = audit_hash(payload)
    return payload


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in NON_CORE_KEYS}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


def audit_hash(payload):
    """SHA-256 over the canonical payload minus run metadata (timestamps, request counters)."""
    return hashlib.sha256(json.dumps(_strip(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- renderers
def md_cell(s):
    return str(s).replace("|", "/").replace("\n", " ")


def render_markdown(rep):
    L = [f"# SEC EDGAR Supply Chain & Counterparty Map (v{VERSION})", "",
         f"Entities: {', '.join(e['ticker'] for e in rep['entities']) or '(none)'} · lookback {rep['parameters']['lookback_days']}d · "
         f"{rep['summary']['edges_total']} cited edges · audit_sha256 `{rep['audit_sha256']}`", "",
         "## Relationship edges", "", "| Row | Source | Counterparty | Type | Rule | Accession | Form | Filed | Section | Quote |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        L.append(f"| {r['row_id']} | {r['source_entity']['ticker']} | {md_cell(r['counterparty'])} | {r['relationship_type']} | {r['rule_id']} | "
                 f"[{r['accession_number']}]({r['source_url']}) | {r['form']} | {r['filing_date']} | {md_cell(r['section'])} | \"{md_cell(r['verbatim_quote'])}\" |")
    L += ["", "## Disclosed concentration (CR-01..04)", ""]
    for t, c in rep["concentration"].items():
        L.append(f"- **{t}** concentration_score {c['concentration_score']}/100")
        for i in c["items"]:
            pct = f"{i['percentage']}%" if i["percentage"] is not None else "sole/single source"
            L.append(f"  - {i['rule_id']} {i['severity']} {i['kind']} {pct} ({i['accession_number']}, {md_cell(i['section'])}): \"{md_cell(i['verbatim_quote'])}\"")
    L += ["", "## Year-over-year counterparty diff (annual reports)", ""]
    L += [f"- {d['entity']} {d['change']} {md_cell(d['counterparty'])} ({d['relationship_type']}) {d['prior_accession_number']} -> {d['latest_accession_number']}"
          + (f" {d['pct_prior']}% -> {d['pct_latest']}%" if d["change"] == "PCT_CHANGED" else "") for d in rep["yoy_diff"]] or ["- none"]
    if rep["errors"] or rep["skipped"]:
        L += ["", "## Errors / skipped", ""] + [f"- {x['step'] if 'step' in x else x['input']}: {x['error']}" for x in rep["errors"] + rep["skipped"]]
    L += ["", f"> {rep['scope_note']}", ""]
    return "\n".join(L)


TYPE_COLORS = {"supplier": "#38BDF8", "customer": "#10B981", "foundry_compute": "#A78BFA", "strategic_partner": "#F59E0B",
               "subsidiary": "#64748B", "equity_stake": "#F472B6"}


def safe_href(url):
    return html.escape(url, quote=True) if str(url).startswith("https://www.sec.gov/Archives/") else "#"


def render_html(rep):
    """Self-contained dark-mode page: SVG graph (type filters) + cited evidence table.  Every dynamic string is html.escape-d."""
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    nodes = rep["graph"]["nodes"]
    filers = [n for n in nodes if n["kind"] == "filer"]
    cps = [n for n in nodes if n["kind"] != "filer"][:120]
    import math
    W, H, cx, cy = 1100, 760, 550, 380
    pos = {}
    for i, n in enumerate(filers):
        a = 2 * math.pi * i / max(1, len(filers))
        r = 0 if len(filers) == 1 else 90
        pos[n["id"]] = (cx + r * math.cos(a), cy + r * math.sin(a))
    for i, n in enumerate(cps):
        a = 2 * math.pi * i / max(1, len(cps))
        pos[n["id"]] = (cx + 320 * math.cos(a), cy + 300 * math.sin(a))
    svg = [f'<svg id="g" viewBox="0 0 {W} {H}" role="img" aria-label="relationship graph">']
    for e in rep["graph"]["edges"]:
        if e["source"] in pos and e["target"] in pos:
            (x1, y1), (x2, y2) = pos[e["source"]], pos[e["target"]]
            svg.append(f'<line class="edge t-{esc(e["type"])}" data-type="{esc(e["type"])}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                       f'stroke="{TYPE_COLORS.get(e["type"], "#94A3B8")}"><title>{esc(e["type"])} · {esc(e["accession_number"])} · {esc(e["quote"])}</title></line>')
    for n in filers + cps:
        x, y = pos[n["id"]]
        filer = n["kind"] == "filer"
        label = n.get("ticker") or n["label"]
        svg.append(f'<g class="node"><circle cx="{x:.1f}" cy="{y:.1f}" r="{18 if filer else 6}" fill="{"#E2E8F0" if filer else "#334155"}" stroke="#0B0F17"/>'
                   f'<text x="{x:.1f}" y="{y + (4 if filer else -10):.1f}" text-anchor="middle" fill="{"#0B0F17" if filer else "#94A3B8"}" font-size="{11 if filer else 9}">{esc(label[:28])}</text>'
                   f'<title>{esc(n["label"])}</title></g>')
    svg.append("</svg>")
    legend = "".join(f'<label><input type="checkbox" checked data-t="{esc(t)}"><span style="color:{c}">&#9632;</span> {esc(t)}</label>' for t, c in TYPE_COLORS.items())
    trs = []
    for r in rep["rows"]:
        trs.append(f'<tr data-type="{esc(r["relationship_type"])}"><td>{esc(r["row_id"])}</td><td>{esc(r["source_entity"]["ticker"])}</td><td>{esc(r["counterparty"])}</td>'
                   f'<td><span class="pill" style="border-color:{TYPE_COLORS.get(r["relationship_type"], "#94A3B8")}">{esc(r["relationship_type"])}</span></td><td>{esc(r["rule_id"])}</td>'
                   f'<td><a href="{safe_href(r["source_url"])}" rel="noopener noreferrer" target="_blank">{esc(r["accession_number"])}</a></td><td>{esc(r["form"])}</td>'
                   f'<td>{esc(r["filing_date"])}</td><td>{esc(r["section"])}</td><td class="q">&ldquo;{esc(r["verbatim_quote"])}&rdquo;</td></tr>')
    conc = "".join(f'<li><b>{esc(t)}</b>: score {esc(c["concentration_score"])}/100 · ' + "; ".join(
        f'{esc(i["rule_id"])} {esc(i["severity"])} {esc(i["kind"])} {esc(i["percentage"]) + "%" if i["percentage"] is not None else "sole/single source"}' for i in c["items"][:6]) + "</li>"
        for t, c in rep["concentration"].items())
    yoy = "".join(f'<li>{esc(d["entity"])} <b>{esc(d["change"])}</b> {esc(d["counterparty"])} ({esc(d["relationship_type"])}) '
                  f'{esc(d["prior_accession_number"])} &rarr; {esc(d["latest_accession_number"])}</li>' for d in rep["yoy_diff"]) or "<li>none</li>"
    css = ("body{background:#0B0F17;color:#E2E8F0;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px}h1{font-size:22px}h2{font-size:16px;color:#94A3B8;margin-top:28px}"
           "svg{width:100%;max-width:1100px;background:#0F1522;border:1px solid #1E293B;border-radius:12px}.edge{stroke-width:1.2;opacity:.75}.edge.off,tr.off{display:none}"
           "label{margin-right:14px;cursor:pointer}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #1E293B;padding:6px 8px;text-align:left;vertical-align:top}"
           "th{color:#94A3B8}.pill{border:1px solid;border-radius:10px;padding:1px 8px}.q{color:#94A3B8;max-width:480px}a{color:#38BDF8}small{color:#64748B}")
    js = ("document.querySelectorAll('input[data-t]').forEach(function(c){c.addEventListener('change',function(){var t=c.getAttribute('data-t');"
          "document.querySelectorAll('[data-type]').forEach(function(e){if(e.getAttribute('data-type')===t){e.classList.toggle('off',!c.checked);}});});});")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
            f'<title>SEC Supply Chain Map</title><style>{css}</style></head><body>'
            f'<h1>SEC EDGAR Supply Chain &amp; Counterparty Map</h1><small>v{esc(VERSION)} · {esc(rep["summary"]["edges_total"])} cited edges · audit_sha256 {esc(rep["audit_sha256"])}</small>'
            f'<h2>Graph</h2><div>{legend}</div>{"".join(svg)}<h2>Disclosed concentration (CR-01..04)</h2><ul>{conc}</ul>'
            f'<h2>Year-over-year diff</h2><ul>{yoy}</ul><h2>Cited evidence</h2><table><thead><tr><th>Row</th><th>Filer</th><th>Counterparty</th><th>Type</th><th>Rule</th>'
            f'<th>Accession</th><th>Form</th><th>Filed</th><th>Section</th><th>Verbatim quote</th></tr></thead><tbody>{"".join(trs)}</tbody></table>'
            f'<h2>Scope</h2><p><small>{esc(rep["scope_note"])}</small></p><script>{js}</script></body></html>')


def write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_receipt(rep, png_path):
    if not os.path.isfile(os.path.join(RECEIPT_ENGINE_DIR, "render_receipt.py")):
        print("receipt engine not found at " + RECEIPT_ENGINE_DIR + "; skipping --receipt-out", file=sys.stderr)
        return False
    sys.path.insert(0, RECEIPT_ENGINE_DIR)
    try:
        import render_receipt as rr
        receipt, text = rr.from_sec_supply_chain_json(rep)
        rr.render_receipt_png(receipt, png_path)
    except Exception as e:  # noqa: BLE001
        print(f"receipt render failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    print(f"receipt: {png_path}\npost: {text}", file=sys.stderr)
    return True


# --------------------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(2)


def build_parser():
    p = _Parser(prog="sec_supply_chain_mapper.py", description="Read-only SEC EDGAR supply chain & counterparty mapper (v1.1)")
    p.add_argument("--version", action="version", version=f"sec_supply_chain_mapper {VERSION}")
    p.add_argument("--tickers", metavar="CSV", help="comma-separated tickers or CIKs (max 10)")
    p.add_argument("--preset", choices=sorted(PRESETS))
    p.add_argument("--stdin-json", action="store_true", help='read {"watchlist":[...],"preset":"...","lookback_days":730,"forms":[...]}')
    p.add_argument("--lookback-days", type=int, default=None, help=f"window for 8-K/6-K/13D/13G (default {LOOKBACK_DEFAULT})")
    p.add_argument("--json", action="store_true", help="emit the full deterministic JSON report")
    p.add_argument("--markdown", action="store_true", help="emit the Markdown report")
    p.add_argument("--html-out", metavar="PATH", help="write the self-contained dark-mode HTML graph")
    p.add_argument("--receipt-out", metavar="PNG", help="render a 1200x675 receipt card")
    return p


def resolve_request(a):
    req = {}
    if a.stdin_json:
        try:
            req = json.loads(sys.stdin.read(65536) or "{}")
        except ValueError:
            raise MapperError("malformed_request_json") from None
        if not isinstance(req, dict):
            raise MapperError("request_must_be_json_object")
    tokens = []
    preset = a.preset or req.get("preset")
    if preset:
        if preset not in PRESETS:
            raise MapperError("unknown_preset")
        tokens += PRESETS[preset]
    wl = req.get("watchlist") or []
    if not isinstance(wl, list) or not all(isinstance(x, str) for x in wl):
        raise MapperError("watchlist_must_be_list_of_strings")
    tokens += wl
    if a.tickers:
        tokens += [t for t in a.tickers.split(",") if t.strip()]
    tokens = list(dict.fromkeys(t.strip().upper() for t in tokens if t.strip()))
    if not tokens:
        raise MapperError("no_entities: pass --tickers, --preset or --stdin-json")
    if len(tokens) > MAX_ENTITIES:
        raise MapperError(f"too_many_entities: max {MAX_ENTITIES}")
    if any(not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t) for t in tokens):
        raise MapperError("invalid_ticker_or_cik")
    lookback = a.lookback_days if a.lookback_days is not None else req.get("lookback_days", LOOKBACK_DEFAULT)
    if not isinstance(lookback, int) or isinstance(lookback, bool) or not 1 <= lookback <= 3650:
        raise MapperError("lookback_days_must_be_1_to_3650")
    forms = req.get("forms") or list(ALL_FORMS)
    if not isinstance(forms, list) or not set(forms) <= set(ALL_FORMS):
        raise MapperError("forms_must_be_subset_of_10-K,20-F,8-K,6-K")
    return tokens, lookback, tuple(forms)


def main(argv=None, fetcher=None):
    a = build_parser().parse_args(argv)
    try:
        tokens, lookback, forms = resolve_request(a)
    except MapperError as e:
        if a.stdin_json:
            print(json.dumps({"state": "USAGE_ERROR", "detail": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    rep = build_report(fetcher or Fetcher(), tokens, lookback, forms)
    if a.html_out:
        write_text(a.html_out, render_html(rep))
    if a.receipt_out:
        write_receipt(rep, a.receipt_out)
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False))
    elif a.markdown:
        print(render_markdown(rep))
    elif a.stdin_json:
        run_dir = os.path.join(RUNS_ROOT, rep["audit_sha256"][:12])
        paths = {"table_path": os.path.join(run_dir, "table.md"), "graph_json_path": os.path.join(run_dir, "graph.json"),
                 "graph_html_path": a.html_out or os.path.join(run_dir, "graph.html")}
        write_text(paths["table_path"], render_markdown(rep))
        write_text(paths["graph_json_path"], json.dumps(rep["graph"], indent=2, sort_keys=True))
        if not a.html_out:
            write_text(paths["graph_html_path"], render_html(rep))
        print(json.dumps(dict(state="OK", run_dir=run_dir, error_count=len(rep["errors"]), **paths)))
    else:
        print(render_markdown(rep))
    return 0 if rep["entities"] else 1


if __name__ == "__main__":
    sys.exit(main())
