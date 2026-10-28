#!/usr/bin/env python3
"""Social Hub dispatcher: Gate 2 (HMAC verification) then send. Dry-run unless --live.

    dispatcher.py --config config.json [--id ID] [--live] [--allow-x-api]

Gate 2 re-checks every approved item immediately before sending: status, signature over the exact current text,
expiry, and a fresh Gate 1 lint. Any mismatch (text edited after approval, expired, tampered) refuses the item.
X: default mode "intent" prints a free 1-click Web Intent URL; the paid API is used only when the config says
mode "api" AND --allow-x-api is passed. If a send times out after the request left, the item becomes
unknown_outcome and is never retried automatically (check the channel, then requeue by hand).
Exit codes: 0 ok | 2 at least one item refused or failed | 10 usage or config error.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import social_hub as sh  # noqa: E402

TIMEOUT = 20


class UnknownOutcome(Exception):
    pass


class Refused(Exception):
    pass


def _request(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise UnknownOutcome("HTTP %d" % e.code)
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        raise Refused("HTTP %d %s" % (e.code, str(body)[:120]))
    except (socket.timeout, TimeoutError):
        raise UnknownOutcome("timeout after request was sent")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise UnknownOutcome("timeout after request was sent")
        raise Refused("network error before send: %s" % e.reason)


def post_json(url, payload, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": "social-hub-publisher/1.0"}
    h.update(headers or {})
    return _request("POST", url, json.dumps(payload).encode("utf-8"), h)


# ---------------------------------------------------------------- adapters
def split_text(text, limit):
    """Split on paragraph/line/space boundaries so every chunk fits `limit`."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                chunks.append(cur.rstrip("\n"))
                cur = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def send_discord(webhook_url, text):
    ids = []
    for chunk in split_text(text, sh.LIMITS["discord"]):
        _, body = post_json(webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true", {"content": chunk, "allowed_mentions": {"parse": []}})
        ids.append(str(body.get("id", "")))
    return ",".join(ids)


def send_telegram(bot_token, chat_id, text):
    _, body = post_json("https://api.telegram.org/bot%s/sendMessage" % bot_token,
                        {"chat_id": chat_id, "text": text, "disable_web_page_preview": False})
    if not body.get("ok"):
        raise Refused("telegram: %s" % str(body)[:120])
    return str(body["result"]["message_id"])


def bsky_facets(text):
    """Link facets use UTF-8 byte offsets, per the AT Protocol."""
    facets = []
    for url in sh.find_urls(text):
        start = text.find(url)
        b0 = len(text[:start].encode("utf-8"))
        facets.append({"index": {"byteStart": b0, "byteEnd": b0 + len(url.encode("utf-8"))},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})
    return facets


def send_bluesky(service, handle, app_password, text):
    _, sess = post_json(service + "/xrpc/com.atproto.server.createSession", {"identifier": handle, "password": app_password})
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": sh.iso(sh.now()), "facets": bsky_facets(text)}
    _, out = post_json(service + "/xrpc/com.atproto.repo.createRecord",
                       {"repo": sess["did"], "collection": "app.bsky.feed.post", "record": record},
                       {"Authorization": "Bearer " + sess["accessJwt"]})
    return out.get("uri", "")


def _pct(s):
    return urllib.parse.quote(str(s), safe="~")


def oauth1_header(method, url, ck, cs, at, ts, nonce=None, stamp=None):
    p = {"oauth_consumer_key": ck, "oauth_nonce": nonce or secrets.token_hex(16), "oauth_signature_method": "HMAC-SHA1",
         "oauth_timestamp": stamp or str(int(time.time())), "oauth_token": at, "oauth_version": "1.0"}
    base = "&".join([method.upper(), _pct(url), _pct("&".join("%s=%s" % (_pct(k), _pct(v)) for k, v in sorted(p.items())))])
    key = "%s&%s" % (_pct(cs), _pct(ts))
    p["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join('%s="%s"' % (_pct(k), _pct(v)) for k, v in sorted(p.items()))


def send_x_api(text, creds, reply_to=None):
    """Paid path (2026 pay-per-use: $0.015 plain, $0.20 with a link). JSON body, so it is not in the OAuth1 base string."""
    url = "https://api.x.com/2/tweets"
    payload = {"text": text}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    _, body = post_json(url, payload, {"Authorization": oauth1_header("POST", url, *creds)})
    return str((body.get("data") or {}).get("id", ""))


# ---------------------------------------------------------------- Gate 2
def gate2_check(item, cfg, secret, others=(), at=None):
    """Return [] if the item may be sent, else a list of refusal reasons."""
    why = []
    if item.get("status") != "approved":
        why.append("status is %r, not approved" % item.get("status"))
    if not (item.get("sig") and item.get("content_sha256") and item.get("not_after")):
        return why + ["no approval signature"]
    if not secret:
        return why + ["no HMAC secret available"]
    if sh.sha256_text(item["text"]) != item["content_sha256"]:
        why.append("text changed after approval")
    expect = sh.sign(item["id"], item["channel"], item.get("target"), item["content_sha256"], item["not_after"], secret)
    if not hmac.compare_digest(expect, item["sig"]):
        why.append("HMAC signature mismatch")
    if sh.iso(at or sh.now()) > item["not_after"]:
        why.append("approval expired at " + item["not_after"])
    if sh.worst(sh.lint_item(item, cfg, others)) == "ERROR":
        why.append("Gate 1 lint now fails")
    return why


# ---------------------------------------------------------------- send
def live_send(item, cfg, allow_x_api):
    ch, c = item["channel"], cfg["channels"][item["channel"]]
    if ch == "discord":
        url = env_required(c["webhooks_env"][item["target"] or sorted(c["webhooks_env"])[0]])
        return send_discord(url, item["text"])
    if ch == "telegram":
        return send_telegram(env_required(c["bot_token_env"]), env_required(c["chat_id_env"]), item["text"])
    if ch == "bluesky":
        return send_bluesky(c.get("service", "https://bsky.social"), env_required(c["handle_env"]), env_required(c["app_password_env"]), item["text"])
    if ch == "x":
        if c.get("mode", "intent") != "api" or not allow_x_api:
            return "intent:" + sh.build_x_intent(item["text"], item.get("reply_to"))
        e = c["credentials_env"]
        return send_x_api(item["text"], [env_required(e[k]) for k in ("consumer_key", "consumer_secret", "access_token", "token_secret")], item.get("reply_to"))
    raise Refused("unsupported channel")


def env_required(name):
    v = sh.env(name)
    if not v:
        raise Refused("environment variable %s is not set" % name)
    return v


def dispatch(cfg, only_id=None, live=False, allow_x_api=False, sender=live_send):
    q = sh.Queue(cfg)
    doc = q.load()
    secret = sh.resolve_secret(cfg)
    results = []
    for item in doc["items"]:
        if only_id and item["id"] != only_id:
            continue
        if item["status"] not in ("approved",) and not only_id:
            continue
        why = gate2_check(item, cfg, secret, doc["items"])
        if why:
            results.append((item["id"], "REFUSED", "; ".join(why)))
            continue
        if item["channel"] == "x" and cfg["channels"]["x"].get("mode", "intent") == "api" and allow_x_api:
            note = " [API cost $%.3f]" % sh.x_cost_usd(item["text"], cfg)
        else:
            note = ""
        if not live:
            results.append((item["id"], "DRY-RUN", "would send to %s%s" % (item["channel"], note)))
            continue
        try:
            ref = sender(item, cfg, allow_x_api)
            item["status"], item["published_ref"], item["published_at"] = "published", ref, sh.iso(sh.now())
            results.append((item["id"], "SENT", ref + note))
        except UnknownOutcome as e:
            item["status"], item["error"] = "unknown_outcome", str(e)
            results.append((item["id"], "UNKNOWN", "%s - verify on the channel; not retried" % e))
        except Refused as e:
            item["status"], item["error"] = "failed", str(e)
            results.append((item["id"], "FAILED", str(e)))
        q.save(doc)
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--id")
    p.add_argument("--live", action="store_true", help="actually send (default is a dry run)")
    p.add_argument("--allow-x-api", action="store_true", help="permit the paid X API when channels.x.mode is 'api'")
    args = p.parse_args(argv)
    try:
        cfg = sh.load_config(args.config)
    except (OSError, ValueError) as e:
        print("error: bad config: %s" % e, file=sys.stderr)
        return 10
    res = dispatch(cfg, args.id, args.live, args.allow_x_api)
    for r in res:
        print("%-16s %-8s %s" % r)
    if not res:
        print("nothing approved to send")
    return 2 if any(r[1] in ("REFUSED", "FAILED", "UNKNOWN") for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
