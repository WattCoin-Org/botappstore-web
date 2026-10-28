#!/usr/bin/env python3
"""Social Hub Publisher: config-driven queue, Gate 1 linter, and HMAC approval for X, Discord, Bluesky and Telegram.

    social_hub.py --config config.json add --channel x --text "..." [--target announcements] [--id ID]
    social_hub.py --config config.json lint [--id ID]        Gate 1: static checks, no network
    social_hub.py --config config.json approve --id ID       re-lints, then signs (HMAC-SHA256) the exact text
    social_hub.py --config config.json list
    social_hub.py --config config.json intent --id ID        free 1-click X Web Intent URL

Nothing is sent from here. Sending is dispatcher.py, which re-verifies the signature (Gate 2).
Exit codes: 0 ok | 1 lint warnings only | 2 lint errors / refused | 10 usage or config error.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
X_URL_WEIGHT = 23
LIMITS = {"x": 280, "bluesky": 300, "discord": 2000, "telegram": 4096}
X_PPU_PLAIN_USD = 0.015
X_PPU_LINK_USD = 0.20
X_PRICING_NOTE = ("2026 X API pay-per-use: $0.015 per plain post, $0.20 per post containing a URL. "
                  "Web Intents are free and 1-click: you press Post yourself.")
STATUSES = ("draft", "approved", "published", "failed", "unknown_outcome")


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- config
def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg.get("channels"), dict):
        raise ValueError("config needs a 'channels' object")
    cfg.setdefault("state_dir", "./state")
    cfg.setdefault("hmac_secret_env", "SOCIAL_HUB_HMAC_SECRET")
    cfg.setdefault("approval_ttl_days", 7)
    cfg.setdefault("lint", {})
    base = os.path.dirname(os.path.abspath(path))
    if not os.path.isabs(cfg["state_dir"]):
        cfg["state_dir"] = os.path.normpath(os.path.join(base, cfg["state_dir"]))
    return cfg


def env(name):
    return os.environ.get(name or "", "").strip()


# ---------------------------------------------------------------- text measures
def normalize(text):
    return unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").strip()


def find_urls(text):
    return [u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")]


def x_weighted_len(text):
    """X counts every URL as 23 chars and CJK/emoji code points as 2."""
    plain = URL_RE.sub("", text)
    n = X_URL_WEIGHT * len(find_urls(text))
    for ch in plain:
        n += 2 if (unicodedata.east_asian_width(ch) in "WF" or ord(ch) >= 0x1F300) else 1
    return n


def measure(channel, text):
    if channel == "x":
        return x_weighted_len(text)
    return len(text)


def x_cost_usd(text, cfg=None):
    x = ((cfg or {}).get("channels") or {}).get("x") or {}
    plain = float(x.get("cost_plain_usd", X_PPU_PLAIN_USD))
    link = float(x.get("cost_link_usd", X_PPU_LINK_USD))
    return link if find_urls(text) else plain


def build_x_intent(text, reply_to=None):
    q = {"text": text}
    if reply_to:
        q["in_reply_to"] = str(reply_to)
    return "https://x.com/intent/post?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def sha256_text(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- HMAC (Gate 2 input)
def resolve_secret(cfg, create=False):
    """Secret comes from the configured env var, else a 0600 file in state_dir (created on first approve)."""
    val = env(cfg.get("hmac_secret_env"))
    if val:
        return val
    path = os.path.join(cfg["state_dir"], "hmac_secret")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        if not create:
            return ""
    os.makedirs(cfg["state_dir"], exist_ok=True)
    secret = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret)
    return secret


def sign(item_id, channel, target, content_sha, not_after, secret):
    msg = "\n".join((item_id, channel, target or "", content_sha, not_after)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- Gate 1 linter
def lint_item(item, cfg, others=()):
    """Static checks. Returns [(severity, code, message)] with severity ERROR or WARN."""
    out = []
    ch, text = item["channel"], normalize(item["text"])
    lcfg = cfg.get("lint") or {}
    chan_cfg = cfg["channels"].get(ch)
    if chan_cfg is None or not chan_cfg.get("enabled", True):
        out.append(("ERROR", "CHANNEL_DISABLED", "channel %r is not enabled in config" % ch))
        return out
    if ch not in LIMITS:
        out.append(("ERROR", "UNKNOWN_CHANNEL", "channel %r is not supported" % ch))
        return out
    if not text:
        out.append(("ERROR", "EMPTY", "text is empty"))
        return out
    n = measure(ch, text)
    if n > LIMITS[ch]:
        out.append(("ERROR", "TOO_LONG", "%s text is %d, limit %d" % (ch, n, LIMITS[ch])))
    elif n > LIMITS[ch] * 0.95:
        out.append(("WARN", "NEAR_LIMIT", "%s text is %d of %d" % (ch, n, LIMITS[ch])))
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        out.append(("ERROR", "CONTROL_CHARS", "text contains control characters"))
    if ch == "discord" and item.get("target") and item["target"] not in (chan_cfg.get("webhooks_env") or {}):
        out.append(("ERROR", "UNKNOWN_TARGET", "discord target %r has no webhook in config" % item["target"]))
    allowed = [d.lower() for d in lcfg.get("allowed_domains", [])]
    for url in find_urls(text):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
            out.append(("ERROR", "DOMAIN_NOT_ALLOWED", "link host %r is not in lint.allowed_domains" % host))
    low = text.lower()
    for term in lcfg.get("banned_terms", []):
        if re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", low):
            out.append(("ERROR", "BANNED_TERM", "banned term %r" % term))
    if len(re.findall(r"(?<!\w)#\w+", text)) > lcfg.get("max_hashtags", 3):
        out.append(("WARN", "HASHTAGS", "more than %d hashtags" % lcfg.get("max_hashtags", 3)))
    if len(re.findall(r"(?<![\w/])@\w+", text)) > lcfg.get("max_mentions", 3):
        out.append(("WARN", "MENTIONS", "more than %d @mentions" % lcfg.get("max_mentions", 3)))
    if len(re.findall(r"[A-Z]{4,}", text)) > 3:
        out.append(("WARN", "SHOUTING", "several ALL-CAPS words"))
    if "!" * 3 in text:
        out.append(("WARN", "PUNCTUATION", "repeated exclamation marks"))
    h = sha256_text(text)
    for o in others:
        if o["id"] != item["id"] and o["channel"] == ch and sha256_text(o["text"]) == h and o.get("status") != "failed":
            out.append(("WARN", "DUPLICATE", "same text already queued as %s (platforms flag duplicate posts)" % o["id"]))
    if ch == "x":
        out.append(("INFO", "X_COST", "API cost if posted via API: $%.3f (%s); Web Intent is free" % (x_cost_usd(text, cfg), "has link" if find_urls(text) else "plain")))
    return out


def worst(findings):
    sev = {f[0] for f in findings}
    return "ERROR" if "ERROR" in sev else "WARN" if "WARN" in sev else "OK"


# ---------------------------------------------------------------- queue
class Queue:
    def __init__(self, cfg):
        self.path = os.path.join(cfg["state_dir"], "queue.json")

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            return doc if isinstance(doc.get("items"), list) else {"items": []}
        except (OSError, ValueError):
            return {"items": []}

    def save(self, doc):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".queue-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, doc, item_id):
        return next((i for i in doc["items"] if i["id"] == item_id), None)


def add_item(cfg, channel, text, target=None, item_id=None, reply_to=None):
    q = Queue(cfg)
    doc = q.load()
    item_id = item_id or "%s-%s" % (channel, secrets.token_hex(3))
    if q.get(doc, item_id):
        raise ValueError("id %r already exists" % item_id)
    item = {"id": item_id, "channel": channel, "target": target, "reply_to": reply_to, "text": normalize(text),
            "status": "draft", "created": iso(now()), "content_sha256": None, "not_after": None, "sig": None}
    doc["items"].append(item)
    q.save(doc)
    return item


def approve_item(cfg, item_id):
    """Gate 1 must pass with no ERROR; then the exact text is HMAC-signed with an expiry."""
    q = Queue(cfg)
    doc = q.load()
    item = q.get(doc, item_id)
    if not item:
        raise KeyError(item_id)
    findings = lint_item(item, cfg, doc["items"])
    if worst(findings) == "ERROR":
        return item, findings, False
    secret = resolve_secret(cfg, create=True)
    item["content_sha256"] = sha256_text(item["text"])
    item["not_after"] = iso(now() + timedelta(days=int(cfg["approval_ttl_days"])))
    item["sig"] = sign(item["id"], item["channel"], item.get("target"), item["content_sha256"], item["not_after"], secret)
    item["status"] = "approved"
    q.save(doc)
    return item, findings, True


# ---------------------------------------------------------------- CLI
def fmt(findings):
    return "\n".join("    [%-5s] %s: %s" % f for f in findings)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--channel", required=True, choices=sorted(LIMITS))
    a.add_argument("--text", required=True)
    a.add_argument("--target")
    a.add_argument("--reply-to")
    a.add_argument("--id")
    for name in ("lint", "approve", "intent"):
        s = sub.add_parser(name)
        s.add_argument("--id", required=(name != "lint"))
    sub.add_parser("list")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print("error: bad config: %s" % e, file=sys.stderr)
        return 10
    q = Queue(cfg)
    if args.cmd == "add":
        try:
            item = add_item(cfg, args.channel, args.text, args.target, args.id, args.reply_to)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 10
        print("added %s (%s, %d/%d)" % (item["id"], item["channel"], measure(item["channel"], item["text"]), LIMITS[item["channel"]]))
        return 0
    doc = q.load()
    if args.cmd == "list":
        for i in doc["items"]:
            print("%-16s %-9s %-9s %s" % (i["id"], i["channel"], i["status"], i["text"].replace("\n", " ")[:60]))
        return 0
    if args.cmd == "lint":
        rc = 0
        for i in [x for x in doc["items"] if not args.id or x["id"] == args.id]:
            f = lint_item(i, cfg, doc["items"])
            print("%s [%s]\n%s" % (i["id"], worst(f), fmt(f)))
            rc = max(rc, {"OK": 0, "WARN": 1, "ERROR": 2}[worst(f)])
        return rc
    item = q.get(doc, args.id)
    if not item:
        print("error: no such item %s" % args.id, file=sys.stderr)
        return 10
    if args.cmd == "approve":
        item, f, ok = approve_item(cfg, args.id)
        print("%s %s\n%s" % (item["id"], "APPROVED until " + item["not_after"] if ok else "REFUSED (Gate 1 errors)", fmt(f)))
        return 0 if ok else 2
    if args.cmd == "intent":
        if item["channel"] != "x":
            print("error: intents exist for channel x only", file=sys.stderr)
            return 10
        print(build_x_intent(item["text"], item.get("reply_to")))
        print("# free, 1-click: open the URL, review, press Post. " + X_PRICING_NOTE)
        return 0
    return 10


if __name__ == "__main__":
    sys.exit(main())
