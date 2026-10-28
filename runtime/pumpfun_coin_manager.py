#!/usr/bin/env python3
"""Pump.fun Coin & Community Manager / Launch Auditor (pumpfun-coin-manager).

Strictly read-only: public HTTP GETs plus one JSON-RPC read (getAccountInfo). No wallet, no keys,
no signing, nothing is posted anywhere. Callouts, replies and bio text are emitted as copy-paste drafts.

    pumpfun_coin_manager.py MINT [MINT ...] [--expected-website URL ...] [--expected-discord URL]
                            [--expected-x HANDLE] [--state PATH --delta] [--draft-replies]
                            [--json] [--fail-on LEVEL] [--timeout SEC]

Exit codes: 0 clean | 1 MEDIUM | 2 HIGH | 3 CRITICAL | 10 bad input | 11 data sources degraded.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import bot_common as bc  # noqa: E402
from bot_common import Finding  # noqa: E402

TOOL = "pumpfun_coin_manager v1.0"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
PUMP_API = "https://frontend-api-v3.pump.fun"
SWAP_API = "https://swap-api.pump.fun"
CAIP_SOL = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
IPFS_GATEWAYS = ["https://pump.mypinata.cloud/ipfs/", "https://4everland.io/ipfs/", "https://gateway.pinata.cloud/ipfs/",
                 "https://dweb.link/ipfs/"]
PUMP_HEADERS = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/"}
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_INITIAL_SUPPLY = 1_000_000_000

LARGE_HOLDER_PCT = 1.0
HOLDER_MOVE_PCT = 50.0
CREATOR_SELL_PTS = 0.5
LIQ_DROP_PCT = 30.0
LIQ_FLOOR_USD = 2000.0
DEX_CONFIRM_RUNS = 2

# The 16 delta finding keys (rows of the delta table; two rows carry a pair of codes).
DELTA_KEYS = [
    "AUTH_REINSTATED", "METADATA_URI_CHANGED", "SUPPLY_INCREASED", "BANNED/COIN_HIDDEN",
    "CALLOUT_NEW", "CALLOUT_REPLY_NEW", "CALLOUT_TAB_EMPTY", "CALLOUT_ANCHOR_POSTED",
    "FEE_SHARING_CHANGED", "CREATOR_PROFILE_CHANGED", "LARGE_HOLDER_NEW", "CREATOR_SELL",
    "LIQUIDITY_DROP", "PAIR_DORMANT/PAIR_ACTIVE", "LINK_GAP_CLOSED", "BOT_FOLLOWERS_UP",
]

SCOPE_NOTE = (
    "SCOPE_NOTE: Read-only audit and community-thread delta monitor across public Solana RPC, IPFS gateways, "
    "Pump.fun v3 read endpoints, DexScreener, GeckoTerminal, and RugCheck. Does not hold private keys, sign "
    "transactions, or auto-post comments/Callouts; every Callout, bio update and holder reply is a "
    "copy-pasteable draft for a human to review and post. Never recommends wash-trading to wake dormant "
    "aggregator pairs, never DMs or tags underwater holders unprompted, and never makes price or return "
    "claims. Findings are heuristics with stated evidence, not verdicts."
)

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ---------------------------------------------------------------- small helpers
def b58decode(s):
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            raise ValueError("bad base58 char")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def valid_mint(s):
    try:
        return 32 <= len(s) <= 44 and len(b58decode(s)) == 32
    except ValueError:
        return False


def sha_short(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def domain_of(url):
    u = (url or "").strip()
    if "://" not in u:
        u = "https://" + u
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def handle_of(x):
    x = (x or "").strip()
    if "/" in x:
        x = x.rstrip("/").rsplit("/", 1)[-1]
    return x.lstrip("@").split("?")[0].lower()


def first(d, *names, default=None):
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return default


def cid_from_uri(uri):
    if "/ipfs/" in (uri or ""):
        return uri.split("/ipfs/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return ""


def verify_cid_digest(cid, data):
    """True/False when the CID is a sha2-256 CIDv1 over a single raw block; None when not checkable here."""
    if not cid or not cid.startswith("b") or not data:
        return None
    try:
        body = cid[1:].upper()
        body += "=" * (-len(body) % 8)
        b = base64.b32decode(body)
    except Exception:
        return None
    if len(b) < 36 or b[0] != 1 or b[1] != 0x55 or b[2] != 0x12 or b[3] != 0x20:
        return None
    return hashlib.sha256(data).digest() == b[4:36]


def classify_text(text):
    t = (text or "").lower()
    if any(k in t for k in ["http://", "https://", "t.me/", "airdrop", "claim", "free", "dm me", "send sol"]):
        return "SPAM_PROMO"
    if any(k in t for k in ["rug", "scam", "dead", "dump", "honeypot", "bundle", "dev sold"]):
        return "FUD_CONCERN"
    if "?" in t or any(k in t for k in ["wen", "what", "how", "where", "discord", "website", "roadmap", "utility"]):
        return "QUESTION"
    return "BULLISH_THESIS" if t.strip() else "COMMUNITY_SENTIMENT"


def is_botlike(username):
    return bool(re.match(r"^pumppush\d*", (username or "").lower()))


# ---------------------------------------------------------------- collection
class Collector:
    """All network access lives here. `http(url, **kw)` and `rpc(method, params)` are injectable for tests."""

    def __init__(self, timeout=20, http=None, rpc=None):
        self.timeout = timeout
        self._http = http or bc.http_json
        self._rpc = rpc
        self.sources = {}  # name -> HTTP status (-1 transport failure)
        self.endpoints = set()
        self.fallbacks = []

    def get(self, name, url, pump=False, retries=3, optional=False):
        self.endpoints.add(urllib.parse.urlparse(url).netloc)
        res = self._http(url, headers=PUMP_HEADERS if pump else None, timeout=self.timeout, retries=retries)
        status, data = res.status, res.data
        if not (optional and status == 404):
            self.sources[name] = status
        return status, data, res

    def rpc(self, method, params):
        self.endpoints.add(urllib.parse.urlparse(SOLANA_RPC).netloc)
        if self._rpc:
            out = self._rpc(method, params)
        else:
            res = bc.http_json(SOLANA_RPC, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                               timeout=self.timeout)
            doc = res.data if isinstance(res.data, dict) else {}
            out = doc.get("result") if res.ok and not doc.get("error") else None
        self.sources["solana_rpc"] = 200 if out is not None else -1
        return out

    def ipfs(self, uri):
        """Fetch metadata JSON via >=2 hash-aware gateways. Returns (json, [(gateway, status, digest_ok)])."""
        cid = cid_from_uri(uri)
        if not cid:
            return {}, []
        attempts, doc = [], {}
        for gw in IPFS_GATEWAYS:
            self.endpoints.add(urllib.parse.urlparse(gw).netloc)
            res = self._http(gw + cid, timeout=min(self.timeout, 12), retries=1)
            ok = res.status == 200 and isinstance(res.data, dict) and "_raw" not in res.data
            attempts.append((urllib.parse.urlparse(gw).netloc, res.status, verify_cid_digest(cid, res.raw) if ok else None))
            if ok and not doc:
                doc = res.data
                if len(attempts) > 1:
                    self.fallbacks.append("ipfs: first gateway failed, used " + attempts[-1][0])
            if sum(1 for a in attempts if a[1] == 200) >= 2:
                break
        self.sources["ipfs"] = 200 if doc else (attempts[-1][1] if attempts else -1)
        return doc, attempts


def collect(mint, col):
    raw = {"mint": mint}
    acct = col.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    raw["account"] = (acct or {}).get("value")
    info = (((raw["account"] or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    exts = {e.get("extension"): e.get("state") for e in info.get("extensions", []) if isinstance(e, dict)}
    meta_uri = (exts.get("tokenMetadata") or {}).get("uri") or ""

    _, coin, _ = col.get("pump_coin", f"{PUMP_API}/coins-v3/{mint}", pump=True)
    raw["coin"] = coin if isinstance(coin, dict) else {}
    meta_uri = meta_uri or raw["coin"].get("metadata_uri") or ""
    raw["ipfs_json"], raw["ipfs_attempts"] = col.ipfs(meta_uri)

    creator = raw["coin"].get("creator")
    raw["creator_addr"] = creator

    def g(name, url, default=None, **kw):
        s, d, _ = col.get(name, url, pump=kw.pop("pump", True), **kw)
        return d if s == 200 else (default if default is not None else {})

    raw["creator_ov"] = g("pump_creator", f"{PUMP_API}/users/{creator}/overview") if creator else {}
    raw["holder_stats"] = g("pump_holder_stats", f"{PUMP_API}/coins/holder-stats/{mint}")
    raw["top_holders"] = g("pump_top_holders", f"{PUMP_API}/coins/top-holders-v2/{mint}")
    raw["positions"] = g("pump_mint_positions", f"{PUMP_API}/mint-positions/{mint}")
    raw["callouts"] = g("pump_callouts", f"{PUMP_API}/callout/coin/{mint}")
    raw["coin_fees"] = g("pump_coin_fees", f"{PUMP_API}/fees/coin/{CAIP_SOL}/{mint}", optional=True)
    raw["unswept"] = g("pump_unswept", f"{PUMP_API}/fees/creator/{creator}/unswept", optional=True) if creator else {}
    raw["followers"] = g("pump_followers", f"{PUMP_API}/following/v3/followers/{creator}", default=[]) if creator else []
    raw["replies"] = {}
    for c in (raw["callouts"].get("callouts") or [])[:10]:
        cid = c.get("calloutId")
        if cid:
            raw["replies"][cid] = g("pump_replies", f"{PUMP_API}/callout/{cid}/replies")
    raw["ds_orders"] = g("dexscreener_orders", f"https://api.dexscreener.com/orders/v1/solana/{mint}", pump=False)
    raw["ds_pairs"] = g("dexscreener_pairs", f"https://api.dexscreener.com/latest/dex/tokens/{mint}", pump=False)
    raw["gt_info"] = g("geckoterminal", f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/info", pump=False)
    rc = g("rugcheck", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", pump=False)
    raw["rugcheck"] = rc
    full = g("rugcheck_full", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", pump=False, optional=True)
    raw["known_accounts"] = full.get("knownAccounts") or {}
    raw["insiders"] = full.get("graphInsidersDetected")
    raw["sources"] = dict(col.sources)
    raw["endpoints"] = sorted(col.endpoints)
    raw["fallbacks"] = list(col.fallbacks)
    return raw


# ---------------------------------------------------------------- snapshot
def build_snapshot(raw, prev=None):
    """Normalise raw API payloads into the state schema (onchain/coin/creator/fees/holders/callouts/surfaces)."""
    acct = raw.get("account") or {}
    info = ((acct.get("data") or {}).get("parsed") or {}).get("info") or {}
    exts = {e.get("extension"): e.get("state") for e in info.get("extensions", []) if isinstance(e, dict)}
    meta = exts.get("tokenMetadata") or {}
    coin = raw.get("coin") or {}
    decimals = info.get("decimals")
    supply_raw = int(info.get("supply") or 0)
    program = acct.get("owner")
    is22 = program == TOKEN_2022
    uri = meta.get("uri") or coin.get("metadata_uri") or ""
    cid = cid_from_uri(uri)
    attempts = raw.get("ipfs_attempts") or []

    ov = raw.get("creator_ov") or {}
    prof = ov.get("profile") or {}
    bio = prof.get("bio") or ""
    followers = raw.get("followers") if isinstance(raw.get("followers"), list) else []
    bots = sorted(f.get("username") for f in followers if is_botlike(f.get("username")))

    sharing = (raw.get("coin_fees") or {}).get("sharing") or {}
    events = (raw.get("coin_fees") or {}).get("events") or []
    dists = [e for e in events if e.get("kind") == "distribution"]
    dist_sol = sum(int(((e.get("distributed") or {}).get("amount") or {}).get("raw", 0)) for e in dists) / 1e9
    holders_doc = sorted((s.get("address"), s.get("shareBps")) for s in sharing.get("shareholders") or [])

    hs = raw.get("holder_stats") or {}
    supply_ui = supply_raw / (10 ** decimals) if decimals is not None else 0
    top = {h["address"]: h.get("amount", 0) for h in (raw.get("top_holders") or {}).get("topHolders") or [] if h.get("address")}
    positions = (raw.get("positions") or {}).get("positions") or []
    creator_addr = raw.get("creator_addr")
    creator_amt = next((p.get("amountHeld") for p in positions if p.get("walletAddress") == creator_addr), top.get(creator_addr))
    social = {}
    for p in positions:
        social[p.get("userId") or p.get("walletAddress")] = {
            "user": p.get("userName"), "wallet": p.get("walletAddress"), "amount": p.get("amountHeld", 0),
            "cost_usd": p.get("costBasisUsd", 0), "pnl_pct": p.get("pnlPercentage", 0),
            "callout_id": (p.get("callout") or {}).get("calloutId") if isinstance(p.get("callout"), dict) else p.get("callout"),
        }

    pos_by_user = {p.get("userId"): p for p in positions}
    items, ids, creator_ids = {}, [], []
    for c in (raw.get("callouts") or {}).get("callouts") or []:
        cid_c = c.get("calloutId")
        if not cid_c:
            continue
        ids.append(cid_c)
        pos = pos_by_user.get(c.get("userId")) or {}
        is_creator = c.get("userId") == prof.get("userId") or c.get("kind") == "own-mint-callout"
        if is_creator:
            creator_ids.append(cid_c)
        items[cid_c] = {"user_id": c.get("userId"), "user": pos.get("userName"), "cls": classify_text(c.get("thesis")),
                        "thesis": (c.get("thesis") or "")[:280], "pnl_pct": pos.get("pnlPercentage"),
                        "is_creator": is_creator}
    replies_by, reply_items = {}, {}
    for cid_c, doc in (raw.get("replies") or {}).items():
        rs = (doc or {}).get("replies") or []
        replies_by[cid_c] = []
        for r in rs:
            rid = first(r, "replyId", "id")
            if not rid:
                continue
            text = first(r, "text", "body", "content", default="")
            replies_by[cid_c].append(rid)
            reply_items[rid] = {"callout": cid_c, "user_id": r.get("userId"), "cls": classify_text(text), "text": text[:280]}

    gt = ((raw.get("gt_info") or {}).get("data") or {}).get("attributes") or {}
    ds_orders = (raw.get("ds_orders") or {}).get("orders") or []
    ds_pairs = (raw.get("ds_pairs") or {}).get("pairs") or []
    rc = raw.get("rugcheck") or {}
    active_now = bool(ds_pairs)
    ps = (prev or {}).get("surfaces") or {}
    confirmed, cand, runs = confirm_pair_state(ps, active_now)
    ath_top10 = (gt.get("holders") or {}).get("distribution_percentage", {}).get("top_10")

    return {
        "mint": raw["mint"],
        "onchain": {
            "program": program, "decimals": decimals, "supply_raw": supply_raw, "supply_ui": supply_ui,
            "token_2022": is22, "mint_auth": info.get("mintAuthority"), "freeze_auth": info.get("freezeAuthority"),
            "meta_auth": meta.get("updateAuthority") if is22 else None, "meta_auth_checked": is22,
            "ext_names": sorted(k for k in exts if k),
            "ext_hash": sha_short(json.dumps(exts, sort_keys=True, default=str)),
            "metadata_uri": uri, "ipfs_cid": cid,
            "gateways_ok": [a[0] for a in attempts if a[1] == 200],
            "digest_ok": [a[2] for a in attempts if a[1] == 200 and a[2] is not None],
            "account_found": bool(acct),
        },
        "ipfs_json": raw.get("ipfs_json") or {},
        "coin": {
            "name": coin.get("name"), "symbol": coin.get("symbol"), "created_ms": coin.get("created_timestamp"),
            "complete": coin.get("complete"), "pool": coin.get("pump_swap_pool") or coin.get("pool_address"),
            "bonding_curve": coin.get("bonding_curve"), "is_banned": bool(coin.get("is_banned")),
            "hide_banner": bool(coin.get("hide_banner")), "banner_set": bool(coin.get("banner_uri")),
            "verified": coin.get("verified"), "reply_count": coin.get("reply_count") or 0,
            "last_reply_ts": coin.get("last_reply"), "last_trade_ts": coin.get("last_trade_timestamp"),
            "website": coin.get("website"), "twitter": coin.get("twitter"), "telegram": coin.get("telegram"),
            "discord": coin.get("discord"),
            "mcap": coin.get("usd_market_cap") or coin.get("market_cap_usd") or 0, "ath": coin.get("ath_market_cap") or 0,
            "ath_ts": coin.get("ath_market_cap_timestamp"), "liq_usd": coin.get("canonical_pool_liquidity_usd") or 0,
        },
        "creator": {
            "address": creator_addr, "user_id": prof.get("userId"), "username": prof.get("username"),
            "x_username": prof.get("x_username"), "header_image_url": prof.get("header_image_url"),
            "bio": bio, "bio_sha": sha_short(bio), "bio_links": sorted(extract_domains(bio)),
            "followers_total": len(followers) or prof.get("followers") or 0,
            "followers_botlike": bots, "followers_organic": max(0, len(followers) - len(bots)),
            "coins_created": ov.get("createdCoinsCount"),
        },
        "fees": {
            "admin": sharing.get("admin"), "admin_revoked": sharing.get("adminRevoked"),
            "shareholders_hash": sha_short(json.dumps(holders_doc, default=str)) if sharing else None,
            "total_fee_sol": hs.get("totalFeesSol"), "distributed_sol": dist_sol, "claim_count": len(dists),
            "unswept_n": len((raw.get("unswept") or {}).get("coins") or []),
        },
        "holders": {
            "total": hs.get("totalHolders"), "top10_pct": hs.get("top10HoldersPercent"),
            "dev_pct": hs.get("devHoldingsPercent"), "snipers_pct": hs.get("snipersHoldingsPercent"),
            "bundlers_pct": hs.get("bundlersHoldingsPercent"), "top": top, "creator_amount": creator_amt,
            "social": social, "supply_ui": supply_ui,
            "known": {a: v.get("name") for a, v in (raw.get("known_accounts") or {}).items()},
            "insiders": raw.get("insiders"),
            "infra": sorted(set((raw.get("known_accounts") or {})) | {a for a in (coin.get("pump_swap_pool"), coin.get("pool_address"), coin.get("bonding_curve"),
                                         coin.get("associated_bonding_curve")) if a}),
        },
        "callouts": {"ids": sorted(ids), "creator_ids": sorted(creator_ids), "count": len(ids), "items": items,
                     "replies_by_callout": {k: sorted(v) for k, v in replies_by.items()}, "reply_items": reply_items},
        "surfaces": {
            "dex_pairs_active": confirmed, "dex_active_now": active_now, "dex_candidate": cand, "dex_candidate_runs": runs,
            "dex_orders": sorted("%s:%s" % (o.get("type"), o.get("status")) for o in ds_orders),
            "dex_order_paid_ms": next((o.get("paymentTimestamp") for o in ds_orders if o.get("type") == "tokenProfile"), None),
            "gt_score": gt.get("gt_score"), "gt_websites": gt.get("websites") or [], "gt_discord": gt.get("discord_url"),
            "gt_telegram": gt.get("telegram_handle"), "gt_twitter": gt.get("twitter_handle"),
            "gt_verified": gt.get("gt_verified"), "gt_top10": float(ath_top10) if ath_top10 else None,
            "rc_norm": rc.get("score_normalised"), "rc_risks": sorted(r.get("name") for r in rc.get("risks") or []),
            "lp_locked_pct": rc.get("lpLockedPct"),
        },
        "sources": raw.get("sources") or {},
        "endpoints": raw.get("endpoints") or [],
        "fallbacks": raw.get("fallbacks") or [],
    }


def confirm_pair_state(prev_surfaces, active_now):
    """A DexScreener active/dormant flip is only confirmed after DEX_CONFIRM_RUNS consecutive runs."""
    if "dex_pairs_active" not in prev_surfaces:
        return active_now, active_now, DEX_CONFIRM_RUNS
    confirmed = prev_surfaces["dex_pairs_active"]
    if active_now == confirmed:
        return confirmed, confirmed, DEX_CONFIRM_RUNS
    runs = prev_surfaces.get("dex_candidate_runs", 0) + 1 if prev_surfaces.get("dex_candidate") == active_now \
        and prev_surfaces.get("dex_candidate") != confirmed else 1
    if runs >= DEX_CONFIRM_RUNS:
        return active_now, active_now, DEX_CONFIRM_RUNS
    return confirmed, active_now, runs


def extract_domains(text):
    found = set()
    for m in re.finditer(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/[^\s]*)?", (text or "").lower()):
        d = m.group(1)
        found.add(d[4:] if d.startswith("www.") else d)
    return found


def degraded_sources(snap):
    return sorted(n for n, s in (snap.get("sources") or {}).items() if not (200 <= s < 300))


# ---------------------------------------------------------------- audit findings
def expected_set(args_like):
    return {
        "websites": [domain_of(w) for w in (args_like.get("website") or []) if w],
        "discord": (args_like.get("discord") or "").strip(),
        "x": handle_of(args_like.get("x")),
        "x_display": (args_like.get("x") or "").rstrip("/").rsplit("/", 1)[-1].lstrip("@"),
    }


def audit_findings(snap, expected=None):
    """Point-in-time findings for one snapshot. Keys are stable so delta mode can diff them."""
    exp = expected or {"websites": [], "discord": "", "x": ""}
    oc, coin, cr, sf, cl = snap["onchain"], snap["coin"], snap["creator"], snap["surfaces"], snap["callouts"]
    ipfs = snap.get("ipfs_json") or {}
    mint = snap["mint"]
    out = []
    F = lambda sev, code, msg, key=None, **ev: out.append(Finding(sev, code, msg, ev, key))

    if not oc["account_found"]:
        F("HIGH", "MINT_NOT_FOUND", "getAccountInfo returned no account for this address; nothing on-chain was audited.")
        return out

    live_auth = {k: v for k, v in (("mint", oc["mint_auth"]), ("freeze", oc["freeze_auth"]), ("metadataUpdate", oc["meta_auth"])) if v}
    if live_auth:
        F("HIGH", "MUTABLE_AUTHORITY", "Active authority present: " + ", ".join("%s=%s" % kv for kv in sorted(live_auth.items())) + ".")
    elif oc["token_2022"]:
        F("INFO", "IMMUTABLE_METADATA", "Mint, freeze, and Token-2022 metadata update authorities are all null (renounced); on-chain/IPFS metadata JSON cannot be modified.")
    else:
        F("INFO", "METADATA_AUTHORITY_UNCHECKED", "Legacy SPL mint: mint/freeze authority are null, but the Metaplex metadata update authority is not read by this tool, so metadata mutability is not asserted.")
    for ext, sev, what in (("permanentDelegate", "HIGH", "a permanent delegate can move or burn any holder's tokens"),
                           ("transferFeeConfig", "MEDIUM", "a transfer fee is charged on every transfer"),
                           ("transferHook", "MEDIUM", "a transfer hook program runs on every transfer"),
                           ("nonTransferable", "MEDIUM", "tokens are non-transferable")):
        if ext in oc["ext_names"]:
            F(sev, "TOKEN2022_" + ext.upper(), "Token-2022 extension %s is present: %s." % (ext, what))

    # immutable IPFS metadata links (F1-F3)
    for k in ("website", "telegram", "discord"):
        if not (ipfs.get(k) or coin.get(k)):
            F("HIGH" if k == "website" else "MEDIUM", "MISSING_IMMUTABLE_" + k.upper(),
              "'%s' link was omitted from the token metadata and is null on /coins-v3/%s; must be compensated on mutable surfaces (creator bio, coin banner, pinned creator Callout, DexScreener/GeckoTerminal profile)." % (k, mint))
    if oc["gateways_ok"] and len(oc["gateways_ok"]) < 2:
        F("LOW", "IPFS_SINGLE_GATEWAY", "Metadata JSON was retrievable from only one IPFS gateway (%s); content could not be cross-checked." % oc["gateways_ok"][0])
    if oc["digest_ok"] and not all(oc["digest_ok"]):
        F("HIGH", "IPFS_DIGEST_MISMATCH", "Bytes served by an IPFS gateway do not hash to the CID %s." % oc["ipfs_cid"])
    if "ipfs.io/ipfs/" in oc["metadata_uri"]:
        F("INFO", "IPFS_IO_URI_REWRITTEN", "Metadata URI uses ipfs.io; this tool reads it through %s instead because ipfs.io has been observed to challenge non-browser clients." % (oc["gateways_ok"][0] if oc["gateways_ok"] else "alternate gateways"))

    # creator profile (F2-F4)
    if not cr["x_username"]:
        F("HIGH", "CREATOR_X_UNLINKED", "Creator profile @%s has x_username=null on Pump.fun; link X in the Pump.fun profile settings." % cr["username"])
    if not cr["header_image_url"]:
        F("MEDIUM", "CREATOR_BANNER_MISSING", "Creator profile @%s has header_image_url=null; upload a header image in profile settings." % cr["username"])
    if not coin["banner_set"]:
        F("LOW", "COIN_BANNER_MISSING", "Coin has no banner set (banner_uri empty); the creator can add one from the coin page.")
    if coin["is_banned"]:
        F("CRITICAL", "BANNED", "Pump.fun reports this coin as banned (is_banned=true).")
    if coin["hide_banner"]:
        F("MEDIUM", "COIN_HIDDEN", "Pump.fun reports hide_banner=true for this coin.")

    # expected links vs surfaces
    bio_l = cr["bio"].lower()
    missing = [w for w in exp["websites"] if w and w not in bio_l.replace("www.", "")]
    if exp["discord"] and not (domain_of(exp["discord"]) in bio_l and exp["discord"].rstrip("/").rsplit("/", 1)[-1].lower() in bio_l):
        missing.append("discord")
    if exp["x"] and exp["x"] not in bio_l and handle_of(cr["x_username"]) != exp["x"]:
        missing.append("x")
    if missing:
        F("HIGH", "CREATOR_BIO_INCOMPLETE", "Creator bio (%d chars: %r) is missing expected links: %s." % (len(cr["bio"]), cr["bio"], ", ".join(missing)))
    if exp["x"] and cr["x_username"] and handle_of(cr["x_username"]) != exp["x"]:
        F("HIGH", "X_HANDLE_MISMATCH", "Creator profile links X @%s but the expected handle is @%s." % (cr["x_username"], exp["x"]))
    if exp["x"]:
        for label, val in (("coin twitter link", coin.get("twitter")), ("GeckoTerminal twitter", sf["gt_twitter"]), ("token metadata twitter", ipfs.get("twitter"))):
            if val and handle_of(val) != exp["x"]:
                F("MEDIUM", "X_HANDLE_MISMATCH", "%s is @%s; expected @%s." % (label, handle_of(val), exp["x"]), key="X_HANDLE_MISMATCH:" + label.split()[0].lower())

    # callouts (F5-F6)
    if cl["count"] == 0:
        legacy = coin["reply_count"]
        F("HIGH", "CALLOUT_TAB_EMPTY", "0 live Callouts on GET /callout/coin/%s%s. The creator can post an anchor Callout from the coin page; ready-to-post drafts follow with --draft-replies." % (
            mint, " (legacy reply_count=%d, last %s; the legacy /replies/{mint} thread is retired)" % (legacy, bc.ts_str(coin["last_reply_ts"])) if legacy else ""))
    unlinked = [h for h in snap["holders"]["social"].values() if not h["callout_id"] and h["wallet"] != cr["address"]]
    if snap["holders"]["social"] and not cl["count"]:
        F("MEDIUM", "SOCIAL_HOLDERS_NO_CALLOUT", "%d Pump.fun social-profile holders are active and none has a Callout (%d besides the creator)." % (len(snap["holders"]["social"]), len(unlinked)))
    if snap["fees"]["unswept_n"]:
        F("LOW", "UNSWEPT_CREATOR_FEES", "%d coin(s) have unswept creator fees." % snap["fees"]["unswept_n"])

    # cross-surface parity (F7-F9)
    if not sf["dex_pairs_active"] and sf["dex_orders"]:
        F("MEDIUM", "DEXSCREENER_PAIR_DORMANT", "DexScreener order(s) %s exist (tokenProfile paid %s), but /latest/dex/tokens/%s returns no pairs; DexScreener indexes pairs only with swap activity. Do not wash-trade to wake it." % (", ".join(sf["dex_orders"]), bc.ts_str(sf["dex_order_paid_ms"]), mint))
    elif not sf["dex_pairs_active"]:
        F("LOW", "DEXSCREENER_NO_PAIRS", "DexScreener returns no pairs for this mint.")
    gt_gaps = [n for n, v in (("websites", sf["gt_websites"]), ("discord", sf["gt_discord"])) if not v]
    if gt_gaps:
        F("MEDIUM", "GECKOTERMINAL_LINKS_MISSING", "GeckoTerminal token info has no %s (gt_score=%s)." % (" and no ".join(gt_gaps), "%.1f" % sf["gt_score"] if sf["gt_score"] is not None else "n/a"))
    elif exp["websites"] and not any(domain_of(w) in exp["websites"] for w in sf["gt_websites"]):
        F("MEDIUM", "GECKOTERMINAL_WEBSITE_MISMATCH", "GeckoTerminal websites %s do not include the expected %s." % (sf["gt_websites"], exp["websites"]))
    if sf["gt_top10"] is not None and snap["holders"]["top10_pct"] is not None and sf["gt_top10"] - snap["holders"]["top10_pct"] > 25:
        F("INFO", "TOP10_SURFACE_CONTRADICTION", "GeckoTerminal shows top-10 holders %.2f%% while Pump.fun shows %.2f%% (Pump.fun excludes the AMM pool and lockers); expect holders to quote the wrong figure." % (sf["gt_top10"], snap["holders"]["top10_pct"]))
    if sf["rc_risks"]:
        F("MEDIUM" if "Copycat token" in sf["rc_risks"] else "LOW", "RUGCHECK_RISKS", "RugCheck normalised score %s with risks: %s." % (sf["rc_norm"], ", ".join(sf["rc_risks"])))

    if cr["followers_botlike"]:
        F("LOW", "BOT_FOLLOWERS", "%d of %d creator followers match the pumppush* bot-name pattern (%d organic)." % (len(cr["followers_botlike"]), cr["followers_total"], cr["followers_organic"]))
    return out


# ---------------------------------------------------------------- delta engine (16 finding keys)
def delta_events(old, new):
    """One-shot events from comparing two snapshots. Returns Findings keyed so repeat events don't collide."""
    ev = []
    E = lambda sev, code, msg, key=None, **evd: ev.append(Finding(sev, code, msg, evd, key or code))
    o, n = old["onchain"], new["onchain"]
    for fld, label in (("mint_auth", "mint"), ("freeze_auth", "freeze"), ("meta_auth", "metadata update")):
        if not o.get(fld) and n.get(fld):
            E("CRITICAL", "AUTH_REINSTATED", "%s authority went from null to %s." % (label, n[fld]), "AUTH_REINSTATED:" + fld)
    if (o["ipfs_cid"], o["metadata_uri"]) != (n["ipfs_cid"], n["metadata_uri"]):
        E("CRITICAL", "METADATA_URI_CHANGED", "Metadata URI changed from %s to %s." % (o["metadata_uri"] or "none", n["metadata_uri"] or "none"))
    if n["supply_raw"] > o["supply_raw"]:
        E("CRITICAL", "SUPPLY_INCREASED", "Supply increased from %s to %s raw units (burn-downs are expected; increases are not)." % (o["supply_raw"], n["supply_raw"]))
    oc, nc = old["coin"], new["coin"]
    if not oc["is_banned"] and nc["is_banned"]:
        E("CRITICAL", "BANNED", "Coin flipped to is_banned=true; inspect Pump.fun moderation status.")
    if oc["hide_banner"] != nc["hide_banner"]:
        E("CRITICAL", "COIN_HIDDEN", "hide_banner flipped from %s to %s." % (oc["hide_banner"], nc["hide_banner"]))

    ocl, ncl = old["callouts"], new["callouts"]
    for cid in ncl["ids"]:
        if cid not in ocl["ids"]:
            it = ncl["items"].get(cid, {})
            hot = it.get("cls") == "QUESTION" or (it.get("pnl_pct") is not None and it["pnl_pct"] < 0)
            E("HIGH" if hot and not it.get("is_creator") else "MEDIUM", "CALLOUT_NEW",
              "New Callout %s by %s classified %s: %r" % (cid[:8], it.get("user") or it.get("user_id"), it.get("cls"), it.get("thesis", "")[:80]),
              "CALLOUT_NEW:" + cid, callout_id=cid)
    old_replies = {r for v in ocl["replies_by_callout"].values() for r in v}
    for cid, rids in sorted(ncl["replies_by_callout"].items()):
        for rid in rids:
            if rid not in old_replies:
                it = ncl["reply_items"].get(rid, {})
                E("MEDIUM", "CALLOUT_REPLY_NEW", "New reply %s on Callout %s classified %s: %r" % (rid[:8], cid[:8], it.get("cls"), it.get("text", "")[:80]),
                  "CALLOUT_REPLY_NEW:" + rid, callout_id=cid)
    if ncl["count"] == 0 and ocl["count"] > 0:
        E("HIGH", "CALLOUT_TAB_EMPTY", "Callout tab went from %d Callouts to 0." % ocl["count"])
    if not ocl["creator_ids"] and ncl["creator_ids"]:
        E("INFO", "CALLOUT_ANCHOR_POSTED", "First creator Callout appeared (%s); CALLOUT_TAB_EMPTY is resolved." % ncl["creator_ids"][0][:8])

    of, nf = old["fees"], new["fees"]
    if (of["shareholders_hash"], of["admin"]) != (nf["shareholders_hash"], nf["admin"]):
        E("HIGH", "FEE_SHARING_CHANGED", "Fee-sharing shareholders or admin changed (admin %s -> %s)." % (of["admin"], nf["admin"]))
    ocr, ncr = old["creator"], new["creator"]
    changed = [f for f in ("username", "x_username", "bio_sha") if ocr.get(f) != ncr.get(f)]
    if changed:
        E("HIGH", "CREATOR_PROFILE_CHANGED", "Creator profile fields changed: %s. Confirm this was an authorized edit." % ", ".join(changed))

    oh, nh = old["holders"], new["holders"]
    supply = nh["supply_ui"] or 1
    infra = set(nh.get("infra") or [])
    for addr, amt in sorted(nh["top"].items()):
        if addr in infra or addr == ncr["address"]:
            continue
        pct, prev_amt = amt / supply * 100, oh["top"].get(addr)
        if prev_amt is None and pct >= LARGE_HOLDER_PCT:
            E("HIGH", "LARGE_HOLDER_NEW", "Wallet %s.. now holds %.2f%% of supply." % (addr[:8], pct), "LARGE_HOLDER_NEW:" + addr, wallet=addr)
        elif prev_amt and abs(amt - prev_amt) / prev_amt * 100 >= HOLDER_MOVE_PCT:
            E("HIGH", "LARGE_HOLDER_NEW", "Top wallet %s.. balance moved %+.0f%% (%.2f%% of supply)." % (addr[:8], (amt - prev_amt) / prev_amt * 100, pct), "LARGE_HOLDER_NEW:" + addr + ":move", wallet=addr)
    dev_drop = (oh["dev_pct"] or 0) - (nh["dev_pct"] or 0)
    ca_o, ca_n = oh.get("creator_amount"), nh.get("creator_amount")
    if dev_drop >= CREATOR_SELL_PTS or (ca_o and ca_n is not None and ca_n < ca_o):
        E("HIGH", "CREATOR_SELL", "Creator holdings fell (dev %.2f%% -> %.2f%%; balance %s -> %s). Prepare a factual distribution/locker explanation before the community asks." % (oh["dev_pct"] or 0, nh["dev_pct"] or 0, ca_o, ca_n))

    lo, ln = oc["liq_usd"] or 0, nc["liq_usd"] or 0
    if lo and (ln <= lo * (1 - LIQ_DROP_PCT / 100) or (ln < LIQ_FLOOR_USD <= lo)):
        E("MEDIUM", "LIQUIDITY_DROP", "Pool liquidity moved from $%s to $%s." % ("{:,.0f}".format(lo), "{:,.0f}".format(ln)))
    os_, ns = old["surfaces"], new["surfaces"]
    if os_["dex_pairs_active"] and not ns["dex_pairs_active"]:
        E("MEDIUM", "PAIR_DORMANT", "DexScreener pairs went inactive (confirmed over %d runs)." % DEX_CONFIRM_RUNS)
    if not os_["dex_pairs_active"] and ns["dex_pairs_active"]:
        E("INFO", "PAIR_ACTIVE", "DexScreener pairs became active (confirmed over %d runs)." % DEX_CONFIRM_RUNS)

    closed = []
    if set(ncr["bio_links"]) - set(ocr["bio_links"]):
        closed.append("bio links +" + ",".join(sorted(set(ncr["bio_links"]) - set(ocr["bio_links"]))))
    if not ocr["x_username"] and ncr["x_username"]:
        closed.append("x_username")
    if not oc["banner_set"] and nc["banner_set"]:
        closed.append("coin banner")
    if not ocr["header_image_url"] and ncr["header_image_url"]:
        closed.append("creator header")
    if not os_["gt_websites"] and ns["gt_websites"]:
        closed.append("gt_websites")
    if not os_["gt_discord"] and ns["gt_discord"]:
        closed.append("gt_discord")
    if closed:
        E("INFO", "LINK_GAP_CLOSED", "Gap closed: " + "; ".join(closed) + ".")
    new_bots = sorted(set(ncr["followers_botlike"]) - set(ocr["followers_botlike"]))
    if new_bots:
        E("LOW", "BOT_FOLLOWERS_UP", "%d new pumppush*-pattern follower(s): %s. Excluded from organic count; log only." % (len(new_bots), ", ".join(new_bots[:5])))
    return ev


# ---------------------------------------------------------------- copy-paste drafts
def make_drafts(snap, expected=None):
    """Return [(label, text)] of drafts. Facts only: no price/return claims, no @-tags of holders."""
    exp = expected or {"websites": [], "discord": "", "x": "", "x_display": ""}
    oc, coin, cr, cl = snap["onchain"], snap["coin"], snap["creator"], snap["callouts"]
    name = "%s (%s)" % (coin["name"] or "this coin", coin["symbol"] or "?")
    auth_bits = [n for n, v in (("mint", oc["mint_auth"]), ("freeze", oc["freeze_auth"]), ("metadata update", oc["meta_auth"])) if v]
    auth_line = ("Mint and freeze authorities are revoked" + (" and the Token-2022 metadata update authority is revoked" if oc["token_2022"] else "") + ".") \
        if not auth_bits and (oc["mint_auth"] is None and oc["freeze_auth"] is None) else "Active authorities: " + ", ".join(auth_bits) + "."
    links = []
    for w in exp["websites"]:
        links.append("Site: https://" + w)
    if exp["discord"]:
        links.append("Discord: " + exp["discord"])
    if exp["x"]:
        links.append("X: https://x.com/" + (exp.get("x_display") or exp["x"]))
    link_block = "\n".join(links)
    drafts = []

    bio_parts = ["%s creator." % (coin["name"] or "Coin")] + [l for l in links]
    bio = " ".join(bio_parts)
    drafts.append(("BIO (paste into Pump.fun profile bio; %d chars)" % len(bio), bio))

    if cl["count"] == 0:
        c1 = ("%s - creator anchor.\n%s\nSupply: %s tokens. %s\n%s" % (name, "Start here for official links:", "{:,.0f}".format(snap["onchain"]["supply_ui"]), auth_line, link_block)).strip()
        drafts.append(("C1 CREATOR ANCHOR CALLOUT", c1))
        c2 = ("On-chain transparency for %s:\n- %s\n- Token program: %s\n- Metadata CID: %s (retrievable from %d independent IPFS gateway(s))\n"
              "Verify these yourself on any Solana explorer; none of this is financial advice." % (
                  name, auth_line, "Token-2022" if oc["token_2022"] else "SPL Token", oc["ipfs_cid"] or "n/a", len(oc["gateways_ok"])))
        drafts.append(("C2 ON-CHAIN TRANSPARENCY CALLOUT", c2))

    for cid, it in sorted(cl["items"].items()):
        if it.get("is_creator"):
            continue
        drafts.append(("REPLY to Callout %s [%s]" % (cid[:8], it["cls"]), reply_for(it["cls"], auth_line, link_block)))
    for rid, it in sorted(cl["reply_items"].items()):
        drafts.append(("REPLY to reply %s on Callout %s [%s]" % (rid[:8], it["callout"][:8], it["cls"]), reply_for(it["cls"], auth_line, link_block)))
    return drafts


def reply_for(cls, auth_line, link_block):
    if cls == "SPAM_PROMO":
        return "(no reply drafted - looks like promo/spam; do not engage, use Pump.fun's report tool)"
    if cls == "FUD_CONCERN":
        return "Fair question - here are the facts that can be verified on-chain: %s Holder and liquidity figures are public on the coin page; happy to point to specifics.\n%s" % (auth_line, link_block)
    if cls == "QUESTION":
        return "Thanks for asking. Official links are below.\n%s\n%s" % (link_block, auth_line)
    return "Thanks for the note.\n%s" % link_block if link_block else "Thanks for the note."


# ---------------------------------------------------------------- rendering
def usd(x):
    return "${:,.2f}".format(x or 0)


def render_overview(snap):
    oc, coin, cr, ho, fe, cl, sf = (snap[k] for k in ("onchain", "coin", "creator", "holders", "fees", "callouts", "surfaces"))
    L = []
    L.append("target     solana:%s (%s / %s)" % (snap["mint"], coin["name"], coin["symbol"]))
    burned = ""
    if snap["mint"].endswith("pump") and oc["supply_ui"]:
        burned = " (burned=%s vs 1,000,000,000 initial)" % "{:,.6f}".format(PUMP_INITIAL_SUPPLY - oc["supply_ui"])
    L.append("program    %s | supply=%s%s" % (oc["program"], "{:,.6f}".format(oc["supply_ui"]), burned))
    L.append("authority  mint=%s freeze=%s metadataUpdate=%s" % (oc["mint_auth"], oc["freeze_auth"], oc["meta_auth"] if oc["meta_auth_checked"] else "unchecked(legacy SPL)"))
    ij = snap.get("ipfs_json") or {}
    L.append("ipfs       cid=%s gateways_ok=%s digest_verified=%s keys=%s" % (oc["ipfs_cid"] or "none", oc["gateways_ok"], oc["digest_ok"] or "n/a", sorted(ij.keys())))
    L.append("pump.coin  created=%s complete=%s pool=%s" % (bc.ts_str(coin["created_ms"]), coin["complete"], coin["pool"]))
    L.append("pump.mkt   mcap=%s ath=%s (%s) liq=%s" % (usd(coin["mcap"]), usd(coin["ath"]), bc.ts_str(coin["ath_ts"]), usd(coin["liq_usd"])))
    L.append("holders    total=%s top10=%s%% dev=%s%% snipers=%s%% bundlers=%s%%" % (ho["total"], ho["top10_pct"], ho["dev_pct"], ho["snipers_pct"], ho["bundlers_pct"]))
    L.append("creator    %s username=@%s x=%s header=%s followers=%s (organic=%s botlike=%d) coinsCreated=%s" % (
        cr["address"], cr["username"], cr["x_username"], cr["header_image_url"], cr["followers_total"], cr["followers_organic"], len(cr["followers_botlike"]), cr["coins_created"]))
    L.append("creatorBio %s" % json.dumps(cr["bio"], ensure_ascii=False))
    L.append("fees       totalFeesSol=%s distributedToCreator=%.6f SOL (%d claims) unswept=%d admin_revoked=%s" % (fe["total_fee_sol"], fe["distributed_sol"], fe["claim_count"], fe["unswept_n"], fe["admin_revoked"]))
    L.append("community  legacy_reply_count=%s last_legacy_reply=%s | live_callouts=%d social_holders=%d" % (coin["reply_count"], bc.ts_str(coin["last_reply_ts"]), cl["count"], len(ho["social"])))
    for h in sorted(ho["social"].values(), key=lambda x: -x["amount"]):
        L.append("  holder_profile @%-16s wallet=%s.. held=%14s cost=$%7.2f pnl=%6.1f%% callout=%s" % (h["user"], (h["wallet"] or "")[:8], "{:,.2f}".format(h["amount"]), h["cost_usd"], h["pnl_pct"], h["callout_id"]))
    for cid, it in sorted(cl["items"].items()):
        L.append("  callout %s.. author=%s [%s] replies=%d %r" % (cid[:8], it.get("user") or (it.get("user_id") or "?")[:8], it["cls"], len(cl["replies_by_callout"].get(cid, [])), it["thesis"][:80]))
    L.append("surfaces   dexscreener_orders=%s (active_pairs=%s) | geckoterminal_score=%s websites=%s discord=%s twitter=%s | rugcheck_norm_score=%s risks=%s lp_locked=%s" % (
        sf["dex_orders"], sf["dex_pairs_active"], "%.1f" % sf["gt_score"] if sf["gt_score"] is not None else "n/a", sf["gt_websites"], sf["gt_discord"], sf["gt_twitter"], sf["rc_norm"], sf["rc_risks"], sf["lp_locked_pct"]))
    if ho.get("known") is not None:
        L.append("rugcheck   knownAccounts=%s graphInsidersDetected=%s" % (sorted(set(ho["known"].values())), ho.get("insiders")))
    return "\n".join(L)


def render_delta(delta, events, old_ts):
    L = ["delta      vs previous snapshot %s: EVENTS=%d NEW=%d CHANGED=%d RESOLVED=%d" % (old_ts or "none (baseline run)", len(events), len(delta["NEW"]), len(delta["CHANGED"]), len(delta["RESOLVED"]))]
    for tag, items in (("EVENT", events), ("NEW", delta["NEW"]), ("CHANGED", delta["CHANGED"]), ("RESOLVED", delta["RESOLVED"])):
        for f in bc.sort_findings(items):
            L.append("  %-8s [%-8s] %s: %s" % (tag, f.sev, f.code, f.msg))
    return "\n".join(L)


def render_drafts(drafts):
    L = ["", "drafts (copy-paste; review before posting - nothing is posted by this tool)"]
    for label, text in drafts:
        L.append("--- %s ---" % label)
        L.extend(text.splitlines() or [""])
    return "\n".join(L)


# ---------------------------------------------------------------- orchestration
def run_mint(mint, args_ns, col_factory=Collector):
    col = col_factory(timeout=args_ns.timeout)
    raw = collect(mint, col)
    store = bc.StateStore(args_ns.state) if args_ns.state else None
    prev_doc = store.get(mint) if store else None
    prev_snap = prev_doc["snapshot"] if prev_doc else None
    snap = build_snapshot(raw, prev=prev_snap)
    exp = expected_set({"website": args_ns.expected_website, "discord": args_ns.expected_discord, "x": args_ns.expected_x})
    findings = audit_findings(snap, exp)
    degraded = bool(degraded_sources(snap))
    result = {"mint": mint, "snapshot": snap, "findings": findings, "degraded": degraded, "events": [], "delta": None, "old_ts": None}
    if args_ns.delta and prev_doc:
        result["events"] = delta_events(prev_snap, snap)
        result["delta"] = bc.diff_findings(prev_doc["findings"], findings)
        result["old_ts"] = prev_doc.get("ts")
    elif args_ns.delta:
        result["delta"] = bc.diff_findings(None, findings)
    if store and not (degraded and prev_doc):  # never overwrite a good baseline with a degraded snapshot
        store.put(mint, {"ts": bc.utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "snapshot": snap,
                         "findings": [f.to_dict() for f in findings]})
    result["drafts"] = make_drafts(snap, exp) if args_ns.draft_replies else []
    return result


def actionable(res):
    """Findings that drive the exit code: everything in a normal run; events + NEW/CHANGED in delta mode."""
    if res["delta"] is None:
        return res["findings"]
    return res["events"] + res["delta"]["NEW"] + res["delta"]["CHANGED"]


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, epilog=SCOPE_NOTE, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mints", nargs="+", metavar="MINT")
    p.add_argument("--expected-website", action="append", default=[], metavar="URL", help="repeatable")
    p.add_argument("--expected-discord", default="", metavar="URL")
    p.add_argument("--expected-x", default="", metavar="HANDLE")
    p.add_argument("--state", metavar="PATH", help="JSON state file (written atomically)")
    p.add_argument("--delta", action="store_true", help="report EVENT/NEW/CHANGED/RESOLVED vs the state file")
    p.add_argument("--draft-replies", action="store_true", help="emit copy-paste Callout/reply/bio drafts")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fail-on", default="medium", choices=["medium", "high", "critical"])
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--no-color", action="store_true", help="accepted for compatibility; output is never colored")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.delta and not args.state:
        print("error: --delta requires --state", file=sys.stderr)
        return bc.EXIT_USAGE
    bad = [m for m in args.mints if not valid_mint(m)]
    if bad:
        print("error: not a valid Solana mint address: %s" % ", ".join(bad), file=sys.stderr)
        return bc.EXIT_USAGE
    worst, degraded_any, outputs = 0, False, []
    for mint in dict.fromkeys(args.mints):
        res = run_mint(mint, args)
        degraded_any = degraded_any or res["degraded"]
        code = bc.exit_code(actionable(res), degraded=res["degraded"], fail_on=args.fail_on)
        worst = max(worst, code if code != bc.EXIT_DEGRADED else 0)
        if args.json:
            outputs.append({"mint": mint, "snapshot": res["snapshot"], "degraded_sources": degraded_sources(res["snapshot"]),
                            "findings": [f.to_dict() for f in bc.sort_findings(res["findings"])],
                            "events": [f.to_dict() for f in res["events"]],
                            "delta": {k: [f.to_dict() for f in v] for k, v in res["delta"].items()} if res["delta"] else None,
                            "drafts": [{"label": l, "text": t} for l, t in res["drafts"]], "scope_note": SCOPE_NOTE})
            continue
        snap = res["snapshot"]
        parts = [bc.snapshot_header(TOOL, snap["endpoints"], snap["fallbacks"]), render_overview(snap)]
        deg = degraded_sources(snap)
        if deg:
            parts.append("DEGRADED  sources unavailable: %s - findings below are incomplete, not clean." % ", ".join("%s(%s)" % (n, snap["sources"][n]) for n in deg))
        if res["delta"] is not None:
            parts.append(render_delta(res["delta"], res["events"], res["old_ts"]))
        parts.append("findings")
        parts.append(bc.render_text(res["findings"]))
        if res["drafts"]:
            parts.append(render_drafts(res["drafts"]))
        parts.append("\n" + SCOPE_NOTE)
        outputs.append("\n".join(parts))
    if args.json:
        print(json.dumps(outputs if len(outputs) > 1 else outputs[0], indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print("\n\n".join(outputs))
    return worst if worst else (bc.EXIT_DEGRADED if degraded_any else 0)


if __name__ == "__main__":
    sys.exit(main())
