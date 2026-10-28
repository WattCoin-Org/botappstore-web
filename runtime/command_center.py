#!/usr/bin/env python3
"""Token Launch Command Center: audit a Pump.fun coin, then turn the verified facts into gated announcement drafts.

    command_center.py init  --dir DIR [--site URL ...] [--discord URL] [--x HANDLE]
    command_center.py audit MINT --config DIR/config.json [--queue] [--draft-replies] [--delta] [--fail-on LEVEL]
    command_center.py status --config DIR/config.json

`init` writes config.json from crypto_launch_preset.json. `audit` runs pumpfun_coin_manager (read-only), prints its
report, and with --queue adds fact-only announcement drafts for X / Discord / Telegram / Bluesky to the Social Hub
queue (Gate 1 lint runs on each). Approve and send with social_hub.py / dispatcher.py; nothing is posted from here.
Exit codes follow pumpfun_coin_manager: 0 clean | 1 MEDIUM | 2 HIGH | 3 CRITICAL | 10 bad input | 11 degraded.
"""
import argparse
import copy
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bot_common as bc  # noqa: E402
import pumpfun_coin_manager as pcm  # noqa: E402
import social_hub as sh  # noqa: E402

PRESET = os.path.join(HERE, "crypto_launch_preset.json")
DISCLAIMER = "Facts only, verify on-chain. Not financial advice."


def init_config(directory, sites=(), discord="", x=""):
    with open(PRESET, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    hosts = {pcm.domain_of(u) for u in list(sites) + ([discord] if discord else []) if u}
    cfg["lint"]["allowed_domains"] = sorted(set(cfg["lint"]["allowed_domains"]) | {h for h in hosts if h})
    cfg["audit"] = {"expected_websites": list(sites), "expected_discord": discord, "expected_x": x.lstrip("@")}
    if x:
        cfg["channels"]["x"]["handle"] = "@" + x.lstrip("@")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return path


def link_lines(exp):
    links = ["https://" + w for w in exp["websites"]]
    if exp["discord"]:
        links.append(exp["discord"])
    if exp["x"]:
        links.append("https://x.com/" + (exp.get("x_display") or exp["x"]))
    return links


def build_posts(snap, exp):
    """Fact-only drafts from a verified snapshot: authority state, supply, mint, official links. Returns [(channel, target, text)]."""
    oc, coin = snap["onchain"], snap["coin"]
    name = "%s ($%s)" % (coin["name"] or "This coin", coin["symbol"] or "?")
    live = [n for n, v in (("mint", oc["mint_auth"]), ("freeze", oc["freeze_auth"])) if v]
    auth = "Mint and freeze authorities: revoked." if not live else "Active authorities: " + ", ".join(live) + "."
    links = link_lines(exp)
    mint = snap["mint"]
    short = "%s official links.\n%s\nMint: %s\n%s" % (name, "\n".join(links), mint, auth)
    long = ("%s - official links and on-chain facts\n%s\nMint: %s\nSupply: %s tokens\n%s\n%s" % (
        name, "\n".join(links), mint, "{:,.0f}".format(oc["supply_ui"]), auth, DISCLAIMER))
    posts = [("x", None, short + "\n" + "Not financial advice."), ("discord", "announcements", long),
             ("telegram", None, long), ("bluesky", None, short)]
    return posts


def queue_posts(cfg, posts, prefix):
    added, skipped = [], []
    q = sh.Queue(cfg)
    for ch, target, text in posts:
        item_id = "%s-%s" % (prefix, ch)
        if q.get(q.load(), item_id):
            skipped.append(item_id)
            continue
        sh.add_item(cfg, ch, text, target=target, item_id=item_id)
        added.append(item_id)
    doc = q.load()
    report = []
    for item_id in added:
        it = q.get(doc, item_id)
        enabled = cfg["channels"].get(it["channel"], {}).get("enabled", True)
        f = sh.lint_item(it, cfg, doc["items"]) if enabled else [("WARN", "CHANNEL_DISABLED", "skipped lint; channel disabled")]
        report.append((item_id, sh.worst(f), f))
    return added, skipped, report


def expected_from_cfg(cfg):
    a = cfg.get("audit") or {}
    return pcm.expected_set({"website": a.get("expected_websites") or [], "discord": a.get("expected_discord") or "", "x": a.get("expected_x") or ""})


def cmd_audit(args, cfg, out=print):
    if not pcm.valid_mint(args.mint):
        print("error: not a valid Solana mint address: %s" % args.mint, file=sys.stderr)
        return bc.EXIT_USAGE
    a = cfg.get("audit") or {}
    ns = argparse.Namespace(timeout=args.timeout, state=os.path.join(cfg["state_dir"], "audit_state.json") if args.delta else None,
                            delta=args.delta, draft_replies=args.draft_replies, expected_website=a.get("expected_websites") or [],
                            expected_discord=a.get("expected_discord") or "", expected_x=a.get("expected_x") or "")
    res = pcm.run_mint(args.mint, ns)
    snap = res["snapshot"]
    out(bc.snapshot_header(pcm.TOOL, snap["endpoints"], snap["fallbacks"]))
    out(pcm.render_overview(snap))
    if res["degraded"]:
        out("DEGRADED  sources unavailable: %s - findings are incomplete, not clean." % ", ".join(pcm.degraded_sources(snap)))
    if res["delta"] is not None:
        out(pcm.render_delta(res["delta"], res["events"], res["old_ts"]))
    out("findings\n" + bc.render_text(res["findings"]))
    if res["drafts"]:
        out(pcm.render_drafts(res["drafts"]))
    if args.queue:
        if not snap["onchain"]["account_found"]:
            out("queue      skipped: mint account not found, nothing verified to announce")
        else:
            added, skipped, report = queue_posts(cfg, build_posts(snap, expected_from_cfg(cfg)), "launch-" + (snap["coin"]["symbol"] or args.mint[:6]).lower())
            out("queue      added=%s skipped(existing)=%s" % (added, skipped))
            for item_id, verdict, f in report:
                out("  %-22s Gate1=%s" % (item_id, verdict))
                for line in f:
                    out("      [%-5s] %s: %s" % line)
    out("\n" + pcm.SCOPE_NOTE)
    code = bc.exit_code(pcm.actionable(res), degraded=res["degraded"], fail_on=args.fail_on)
    return code


def cmd_status(cfg, out=print):
    doc = sh.Queue(cfg).load()
    for i in doc["items"]:
        out("%-26s %-9s %-10s %s" % (i["id"], i["channel"], i["status"], i["text"].replace("\n", " ")[:50]))
    if not doc["items"]:
        out("queue is empty")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--dir", required=True)
    i.add_argument("--site", action="append", default=[])
    i.add_argument("--discord", default="")
    i.add_argument("--x", default="")
    a = sub.add_parser("audit")
    a.add_argument("mint")
    a.add_argument("--config", required=True)
    a.add_argument("--queue", action="store_true")
    a.add_argument("--draft-replies", action="store_true")
    a.add_argument("--delta", action="store_true")
    a.add_argument("--fail-on", default="medium", choices=["medium", "high", "critical"])
    a.add_argument("--timeout", type=int, default=20)
    s = sub.add_parser("status")
    s.add_argument("--config", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        print(init_config(args.dir, args.site, args.discord, args.x))
        return 0
    try:
        cfg = sh.load_config(args.config)
    except (OSError, ValueError) as e:
        print("error: bad config: %s" % e, file=sys.stderr)
        return bc.EXIT_USAGE
    return cmd_status(cfg) if args.cmd == "status" else cmd_audit(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
