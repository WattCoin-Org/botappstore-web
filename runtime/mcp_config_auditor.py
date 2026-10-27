#!/usr/bin/env python3
"""MCP Server & Tool-Permission Security Auditor (strictly read-only, stdlib + bot_common).

Statically audits Model Context Protocol client configuration files (Claude Desktop, Cursor, VS Code, Windsurf,
Cline, Zed, Claude Code `.mcp.json`) plus optional `tools/list` schema snapshots, across five buckets:
  1. SEC_*            hardcoded secrets in env / args / headers / URLs / inputs (always redacted in output)
  2. CMD_* FS_* DOCKER_* DB_*  shell wrappers, remote pipe-exec, dangerous flags, filesystem / Docker host exposure
  3. SUP_*            unpinned npx/uvx/pipx packages, floating tags, unpinned git sources, typosquatted packages
  4. NET_*            cleartext remote HTTP, unauthenticated remote endpoints, metadata SSRF, insecure bridges
  5. TOOL_*           tool-description poisoning, invisible Unicode, exfiltration params, cross-server shadowing,
                      destructive tools without (or with lying) annotations
It prints a copy-paste hardened config per file and never launches servers, installs packages, touches the
network, or modifies any config file.
Exit codes: 0 clean, 1 MEDIUM (or LOW/INFO with --fail-on low/info), 2 HIGH, 3 CRITICAL, 10 bad input,
11 degraded (unreadable / malformed input, or nothing to audit).

Offline bundle (JSON, --offline): {"now": "2026-10-27T00:00:00Z" (optional),
"configs": {path: <config object> | "<raw JSON/JSONC text>" | null (unreadable)},
"tools": <tools snapshot: {"tools": [...]}, {"servers": {name: {"tools": [...]}}}, {name: [...]} or [...]>,
"tools_files": {path: <tools snapshot> | null}}.
"""
import argparse
import bisect
import copy
import datetime
import hashlib
import ipaddress
import json
import math
import os
import re
import shlex
import sys
import tempfile
import types
import urllib.parse

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

TOOL = "mcp-config-auditor"
VERSION = "1.0.0"
STATE_KEY = "mcp-config-auditor"
SCOPE_NOTE = ("SCOPE_NOTE: Static, read-only audit of local MCP client configuration files (Claude Desktop, Cursor, "
              "VS Code, Windsurf, Cline, Zed, Claude Code .mcp.json) and of tools/list schema snapshots the user "
              "supplies. No MCP server is launched, no package is downloaded or installed, no network request is "
              "made, and no config file is modified; hardened snippets are templates to review before use, and "
              "VERSION / COMMIT_SHA placeholders must be replaced with versions you have reviewed. Secrets are "
              "detected by known token prefixes, key names and entropy and are always redacted; detection is "
              "heuristic, so the absence of a finding is not proof that a config is safe. Typosquat matching covers "
              "a curated catalog of popular MCP packages only. Tool-poisoning checks only see the descriptions in "
              "the snapshot provided; servers can change them after install, so use --state PATH --delta to catch "
              "rug-pulls. Not a penetration test, malware analysis, or compliance attestation. Audit only "
              "configurations you own or are authorised to review.")

MAX_FILE_BYTES = 5_000_000
MAX_DISCOVERED = 500
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build"}
DISCOVER_NAMES = {"mcp.json", ".mcp.json", "claude_desktop_config.json", "cline_mcp_settings.json",
                  "windsurf_mcp_config.json", "mcp_servers.json", "mcp_config.json"}
SERVER_KEYS = {"command", "args", "url", "serverUrl", "httpUrl", "type", "transport", "env", "headers"}
SEV_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")

# ---------------------------------------------------------------- secret patterns
_TOKEN_DEFS = [
    ("PRIVATE_KEY_PEM", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|[\s\S]*)", False),
    ("SLACK_WEBHOOK", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", False),
    ("DISCORD_WEBHOOK", r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", False),
    ("ANTHROPIC_KEY", r"sk-ant-[A-Za-z0-9_-]{16,}", True),
    ("OPENAI_KEY", r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,}", True),
    ("STRIPE_KEY", r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}", True),
    ("GITHUB_TOKEN", r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", True),
    ("GITLAB_TOKEN", r"glpat-[A-Za-z0-9_-]{16,}", True),
    ("SLACK_TOKEN", r"xox[abposr]-[A-Za-z0-9-]{10,}", True),
    ("AWS_ACCESS_KEY", r"(?:AKIA|ASIA)[0-9A-Z]{16}", True),
    ("GOOGLE_API_KEY", r"AIza[0-9A-Za-z_-]{35}", True),
    ("WATTCOIN_KEY", r"wc[kl]_[A-Za-z0-9_-]{12,}", True),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*)?", True),
    ("NPM_TOKEN", r"npm_[A-Za-z0-9]{30,}", True),
    ("PYPI_TOKEN", r"pypi-[A-Za-z0-9_-]{16,}", True),
    ("HF_TOKEN", r"hf_[A-Za-z0-9]{30,}", True),
]
TOKEN_RX = [(lbl, re.compile((r"(?<![A-Za-z0-9])" if bounded else "") + rx)) for lbl, rx, bounded in _TOKEN_DEFS]
BASE58_KEY_RX = re.compile(r"[1-9A-HJ-NP-Za-km-z]{86,90}")
HEX_KEY_RX = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
KEYISH_HINT_RX = re.compile(r"(?i)key|secret|private|seed|wallet|signer|mnemonic")
VAR_REF_RX = re.compile(r"\$\{[^}]*\}|\$env:[A-Za-z_]\w*|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%|\{\{[^}]*\}\}")
PLACEHOLDER_RX = re.compile(r"(?i)^(?:<[^<>]{1,80}>|\[[A-Z0-9_ -]{3,60}\]|your[\w .-]*|[\w.-]*[_-]here|x{4,}|\*{3,}|\.{3,}|"
                            r"changeme|change[_-]me|replace[_-]?me|placeholder|todo|tbd|redacted|dummy|none|null|example)$")
SCHEME_PREFIX_RX = re.compile(r"(?i)^\s*(?:bearer|basic|token|apikey)\s+")
SECRET_NAME_RX = re.compile(r"(?i)(?:^|[_.-])(?:api[_-]?key|apikey|token|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
                            r"password|passwd|pwd|pass|private[_-]?key|privkey|secret[_-]?key|client[_-]?secret|mnemonic|"
                            r"seed[_-]?phrase|credentials?|session[_-]?key|signing[_-]?key|access[_-]?key)$|^bearer|secret|private[_-]?key")
NONSECRET_SUFFIX_RX = re.compile(r"(?i)[_.-](?:id|name|path|file|dir|region|arn|uri|endpoint|host|ttl|timeout|limit|enabled|mode|"
                                 r"type|version|length|count|size)$")
SECRET_FLAG_RX = re.compile(r"(?i)^--?((?:[a-z0-9]+[-_])*(?:api[-_]?key|apikey|token|access[-_]token|auth[-_]token|secret|"
                            r"client[-_]secret|password|passwd|pass|private[-_]key|access[-_]key|bearer|key))(?:=(.*))?$", re.S)
SECRET_HEADER_RX = re.compile(r"(?i)^(?:authorization|proxy-authorization|cookie)$|api[-_]?key|token|secret|password|auth")
URL_SECRET_PARAMS = {"api_key", "apikey", "api-key", "key", "token", "access_token", "auth", "auth_token", "secret",
                     "password", "client_secret", "sig", "signature", "x-api-key"}
ENV_ASSIGN_RX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)

# ---------------------------------------------------------------- command patterns
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"}
REMOTE_EXEC_RX = re.compile(r"(?i)\b(?:curl|wget|invoke-webrequest|iwr|irm|invoke-restmethod|invoke-expression|iex|"
                            r"downloadstring|frombase64string)\b|\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b|\|\s*(?:python[0-9.]*|node|perl|ruby)\b|"
                            r"\beval\b|base64\s+(?:-d|--decode|-D)\b")
METACHAR_RX = re.compile(r"[|;&<>`]|\$\(|\$\{?[A-Za-z_]")
DANGEROUS_FLAGS = {
    "--dangerously-skip-permissions": "CRITICAL", "--allow-all": "CRITICAL", "--yolo": "CRITICAL",
    "--unsafe": "HIGH", "--no-sandbox": "HIGH", "--disable-sandbox": "HIGH", "--enable-all-tools": "HIGH",
    "--allow-commands": "HIGH", "--allow-run": "HIGH", "--allow-dangerous": "HIGH", "--trust-all": "HIGH",
    "--disable-security": "HIGH", "--allow-write": "HIGH", "--allow-all-commands": "HIGH", "--unrestricted": "HIGH",
}
SENSITIVE_SEGS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".password-store", ".netrc", ".npmrc", ".pypirc",
                  ".git-credentials", "Keychains", ".wattcoin_env", "docker.sock", "gcloud", ".gcloud", ".vault-token",
                  "id_rsa", "id_ed25519"}
SENSITIVE_ROOTS = {"etc", "proc", "sys", "boot", "dev"}
DOCKER_VALUE_FLAGS = {"-e", "--env", "-v", "--volume", "--mount", "-p", "--publish", "--name", "-w", "--workdir", "-u", "--user",
                      "--entrypoint", "-l", "--label", "--platform", "--network", "--net", "--pid", "--ipc", "--cap-add",
                      "--cap-drop", "--security-opt", "--env-file", "--add-host", "--dns", "-m", "--memory", "--cpus",
                      "--hostname", "-h", "--restart", "--pull", "--log-driver", "--tmpfs", "--device", "--uts", "--userns",
                      "--runtime", "--gpus", "--shm-size", "--ulimit", "--group-add"}
DB_HINT_RX = re.compile(r"(?i)postgres|\bpg\b|mysql|mariadb|sqlite|mongo")
DB_READONLY_RX = re.compile(r"(?i)--?read-?only|readonly|read_only|access-mode[= ]restricted|mode=ro\b|default_transaction_read_only|"
                            r"\bro_user\b|reader|--allow-write=false")
PROD_HOST_RX = re.compile(r"(?i)prod|production|primary|master|rds\.amazonaws\.com|\.database\.azure\.com|cloudsql")
# The official reference Postgres server only exposes a read-only query tool (wrapped in a READ ONLY transaction).
DB_READONLY_PACKAGES = {"@modelcontextprotocol/server-postgres"}

# ---------------------------------------------------------------- supply-chain catalog
OFFICIAL_PACKAGES = [
    "@modelcontextprotocol/server-filesystem", "@modelcontextprotocol/server-github", "@modelcontextprotocol/server-gitlab",
    "@modelcontextprotocol/server-postgres", "@modelcontextprotocol/server-sqlite", "@modelcontextprotocol/server-brave-search",
    "@modelcontextprotocol/server-fetch", "@modelcontextprotocol/server-puppeteer", "@modelcontextprotocol/server-memory",
    "@modelcontextprotocol/server-slack", "@modelcontextprotocol/server-gdrive", "@modelcontextprotocol/server-everything",
    "@modelcontextprotocol/server-sequential-thinking", "@modelcontextprotocol/server-google-maps",
    "@modelcontextprotocol/server-redis", "@modelcontextprotocol/server-aws-kb-retrieval", "@modelcontextprotocol/server-everart",
    "@modelcontextprotocol/inspector", "@playwright/mcp", "@stripe/mcp", "@cloudflare/mcp-server-cloudflare",
    "mcp-server-git", "mcp-server-fetch", "mcp-server-sqlite", "mcp-server-time", "mcp-server-sentry",
]
KNOWN_GOOD_PACKAGES = set(OFFICIAL_PACKAGES) | {
    "mcp-remote", "supergateway", "mcp-proxy", "@upstash/context7-mcp", "@notionhq/notion-mcp-server",
    "@supabase/mcp-server-supabase", "@sentry/mcp-server", "@browsermcp/mcp", "firecrawl-mcp", "exa-mcp-server", "tavily-mcp",
    "@heroku/mcp-server", "@azure/mcp", "@executeautomation/playwright-mcp-server", "@modelcontextprotocol/sdk",
}
OFFICIAL_BARE = {p.split("/", 1)[1]: p for p in OFFICIAL_PACKAGES if p.startswith("@modelcontextprotocol/")}
FLOATING_TAGS = {"latest", "next", "main", "master", "head", "canary", "beta", "alpha", "dev", "nightly", "edge", "rc"}
EXACT_VER_RX = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
SHA_PIN_RX = re.compile(r"(?:[#@/]|commit=)[0-9a-f]{40}(?:$|[^0-9a-f])")
BRIDGE_PACKAGES = {"mcp-remote", "supergateway", "mcp-proxy"}

# ---------------------------------------------------------------- network
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata", "100.100.100.200", "0.0.0.0", "fd00:ec2::254",
                  "metadata.azure.com"}
ENDPOINT_FLAGS = {"--url", "--sse", "--endpoint", "--server-url", "--server", "--streamablehttp", "--remote"}
PUBLIC_ENDPOINT_HOSTS = {"gitmcp.io", "mcp.deepwiki.com", "docs.mcp.cloudflare.com", "mcp.context7.com"}

# ---------------------------------------------------------------- tool poisoning
INJECTION_RULES = [
    ("ignore-instructions", r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|other|system)\b"
                            r"[^.\n]{0,20}\b(?:instructions?|prompts?|rules|directions)\b"),
    ("ignore-all-prior", r"\bignore all prior\b"),
    ("conceal-from-user", r"\b(?:do not|don't|never|must not)\s+(?:tell|inform|mention|reveal|show|notify|alert)\b[^.\n]{0,30}\buser\b|"
                          r"\bhide (?:this|it|these)?[^.\n]{0,20}from the user\b|\bwithout (?:telling|informing|notifying|alerting|asking) the user\b"),
    ("pre-call-read", r"\bbefore (?:calling|using|invoking|running|executing) (?:this|any|the)\b[^.\n]{0,40}\b(?:read|open|cat|fetch|load|include)\b"),
    ("ssh-key-path", r"~/\.ssh|\.ssh/|\bid_(?:rsa|ed25519|ecdsa|dsa)\b"),
    ("dotenv-path", r"(?<![\w])\.env\b|\.wattcoin_env"),
    ("mcp-config-path", r"\.cursor/mcp\.json|claude_desktop_config\.json|\.vscode/mcp\.json|\bmcp\.json\b"),
    ("credential-path", r"\.aws/credentials|/etc/(?:passwd|shadow)|\.config/gcloud|\.kube/config|\.git-credentials"),
    ("fake-system-tag", r"<\s*/?\s*(?:important|system|instructions?|hidden|admin|override)\s*>|\[\s*/?\s*(?:system|inst|admin)\s*\]|"
                        r"<\|im_start\|>|<\|system\|>"),
    ("role-override", r"\byou are now\b|\bnew (?:system )?instructions?\s*:"),
    ("exfiltration", r"\b(?:pass|send|include|put|append|attach|copy|embed|add|provide|insert)\b[^.\n]{0,60}\b(?:contents?|secrets?|"
                     r"credentials?|api[ _-]?keys?|private[ _-]?keys?|passwords?|tokens?|conversation|chat history|system prompt|ssh)\b"
                     r"[^.\n]{0,60}\b(?:in|into|as|via|inside|to)\b[^.\n]{0,40}\b(?:param(?:eter)?|argument|field|note|notes|"
                     r"telemetry|sidenote|metadata|comment)\b"),
]
INJECTION_RX = [(lbl, re.compile(rx, re.I)) for lbl, rx in INJECTION_RULES]
INVISIBLE_RX = re.compile("[\u00ad\u200b\u200c\u200d\u2060-\u2064\u202a-\u202e\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
SUSPICIOUS_PARAMS = {"conversation_history", "chat_history", "system_prompt", "system_message", "env_vars",
                     "environment_variables", "ssh_key", "telemetry_dump", "all_previous_messages", "previous_messages",
                     "full_conversation", "passthrough", "sidenote", "context_dump"}
DESTRUCTIVE_NAME_RX = re.compile(r"(?i)(?:^|[_\-.])(?:exec|execute|run_shell|shell|run_command|run_cmd|execute_command|bash|"
                                 r"powershell|terminal|eval|delete|delete_all|delete_file|remove|rm|rmdir|drop|drop_table|truncate|"
                                 r"raw_sql|execute_sql|write_query|write|write_file|edit_file|overwrite|destroy|purge|wipe|kill)(?:$|[_\-.])")
DESTRUCTIVE_DESC_RX = re.compile(r"(?i)\b(?:execut(?:e|es|ing) (?:arbitrary |any )?(?:shell |system )?(?:commands?|code|scripts?|sql)|"
                                 r"runs? (?:arbitrary |any )?(?:shell|bash|terminal) commands?|delet(?:e|es) (?:all|files?|records|rows|tables?)|"
                                 r"drops? (?:a |the )?tables?|overwrit(?:e|es)|permanently (?:remove|delete)s?)\b")
WRITE_PARAMS = {"command", "cmd", "script", "shell", "code", "sql", "statement", "commands"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def shannon(s):
    if not s:
        return 0.0
    n, counts = len(s), {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def levenshtein(a, b, cap=3):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def env_var_name(*parts):
    s = re.sub(r"[^A-Z0-9]+", "_", "_".join(p for p in parts if p).upper()).strip("_")
    return s or "SECRET"


def cmd_base(cmd):
    b = os.path.basename(str(cmd or "").replace("\\", "/")).lower()
    return re.sub(r"\.(exe|cmd|bat|ps1)$", "", b)


def sha16(obj):
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def visible(s, limit=100):
    """Printable, single-line, truncated rendering; invisible/bidi characters become <U+XXXX>."""
    s = INVISIBLE_RX.sub(lambda m: "<U+%04X>" % ord(m.group(0)), str(s))
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."


def find_token(value):
    for label, rx in TOKEN_RX:
        m = rx.search(value)
        if m:
            return label, m.group(0)
    return None


def literal_part(value):
    s = VAR_REF_RX.sub("", value)
    return SCHEME_PREFIX_RX.sub("", s).strip()


def is_var_ref(value):
    return bool(VAR_REF_RX.search(value)) and not literal_part(value)


def is_placeholder(lit):
    return bool(PLACEHOLDER_RX.match(lit.strip()))


def is_secret_name(name):
    return bool(name) and bool(SECRET_NAME_RX.search(name)) and not NONSECRET_SUFFIX_RX.search(name)


def looks_nonsecret(lit):
    low = lit.lower()
    if low in ("true", "false", "yes", "no", "on", "off", "localhost") or re.fullmatch(r"[\d.:-]+", lit):
        return True
    if lit.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", lit):
        return True
    return "://" in lit and not urllib.parse.urlsplit(lit).password


def entropy_candidate(lit):
    if len(lit) < 20 or re.search(r"\s", lit) or "://" in lit or lit.startswith(("/", "~", ".", "$", "-", "@")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", lit) or is_placeholder(lit):
        return False
    return shannon(lit) >= 4.0


def url_credentials(value):
    """Literal password in userinfo or secret-named query parameter -> (what, raw) or None."""
    try:
        parts = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return None
    if parts.password and not is_var_ref(parts.password) and not is_placeholder(parts.password):
        return "userinfo password", parts.password
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
        if k.lower() in URL_SECRET_PARAMS and v and not is_var_ref(v) and not is_placeholder(v) and len(v) >= 6:
            return "query parameter '%s'" % k, v
    return None


def classify_path(p):
    """-> (kind, label) with kind ROOT | HOME | SENSITIVE, or None."""
    norm = str(p).strip().strip("'\"").replace("\\", "/")
    norm = re.sub(r"^(?:\$\{?HOME\}?|\$\{env:HOME\}|\$\{userHome\}|%USERPROFILE%|\$env:USERPROFILE)", "~", norm, flags=re.I)
    if len(norm) > 1:
        norm = norm.rstrip("/") or "/"
    if norm in ("/", "/*") or re.fullmatch(r"[A-Za-z]:/?", norm):
        return "ROOT", "filesystem root"
    if norm in ("~", "~/"):
        return "HOME", "home directory root"
    if re.fullmatch(r"/(?:Users|home)(?:/[^/]+)?|/root|/var/root|[A-Za-z]:/Users(?:/[^/]+)?", norm, re.I):
        return "HOME", "home directory root"
    segs = [s for s in norm.split("/") if s]
    for s in segs:
        if s in SENSITIVE_SEGS or s.startswith(".env"):
            return "SENSITIVE", s
    if norm.startswith("/") and segs and segs[0] in SENSITIVE_ROOTS:
        return "SENSITIVE", "/" + segs[0]
    if segs and segs[-1] == ".config":
        return "SENSITIVE", ".config (cloud CLI credentials)"
    if norm.startswith("/var/lib/docker"):
        return "SENSITIVE", "/var/lib/docker"
    return None


def is_hostpath(src):
    return bool(src) and (src[0] in "/~.$%" or bool(re.match(r"^[A-Za-z]:[\\/]", src)))


# ---------------------------------------------------------------- JSONC + line locator
def strip_jsonc(text):
    """Blank out // and /* */ comments and trailing commas outside strings; newlines are kept so line numbers hold.

    Returns (clean_text, sorted list of JSONC extensions found)."""
    found = set()
    if text.startswith("\ufeff"):
        text = text[1:]
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and text[i + 1:i + 2] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            found.add("// comments")
            i = j
        elif c == "/" and text[i + 1:i + 2] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            found.add("/* */ comments")
            i = j
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    res, in_str, i = list(s), False, 0
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ",":
            k = i + 1
            while k < len(s) and s[k] in " \t\r\n":
                k += 1
            if k < len(s) and s[k] in "}]":
                res[i] = " "
                found.add("trailing commas")
        i += 1
    return "".join(res), sorted(found)


class Locator:
    def __init__(self, text):
        self.text = text
        self.nl = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_at(self, pos):
        return bisect.bisect_left(self.nl, pos) + 1 if pos >= 0 else None

    def key_pos(self, key, start=0):
        for enc in (json.dumps(key, ensure_ascii=False), json.dumps(key)):
            m = re.compile(re.escape(enc) + r"\s*:").search(self.text, max(start, 0))
            if m:
                return m.start()
        return -1

    def value_line(self, value, start=0):
        for enc in (json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]):
            i = self.text.find(enc, max(start, 0)) if enc else -1
            if i >= 0:
                return self.line_at(i)
        return None


# ---------------------------------------------------------------- redaction
class Redactor:
    """Registry of every secret seen this run + known token patterns; applied to all output and state."""

    def __init__(self):
        self.known = {}

    def add(self, raw, label="SECRET"):
        if isinstance(raw, str) and len(raw) >= 4:
            self.known.setdefault(raw, label)

    def text(self, s):
        if not isinstance(s, str):
            return s
        for raw in sorted(self.known, key=len, reverse=True):
            if raw in s:
                s = s.replace(raw, "[REDACTED:%s]" % self.known[raw])
        for label, rx in TOKEN_RX:
            s = rx.sub("[REDACTED:%s]" % label, s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        return o


# ---------------------------------------------------------------- per-server audit
class ServerAudit:
    def __init__(self, fa, name, raw, loc, pos, R):
        self.fa, self.file, self.name, self.raw, self.loc, self.pos, self.R = fa, fa["path"], name, raw, loc, pos, R
        self.line = loc.line_at(pos) if pos >= 0 else None
        self.findings, self.secret_map, self.edits, self.notes, self.packages = [], {}, {}, [], []
        self.removed, self.url_fix, self.auth_present, self.pkg_positions, self.env_add = False, None, False, set(), {}
        cmd, args, env = raw.get("command"), raw.get("args"), raw.get("env")
        if isinstance(cmd, dict):  # older Zed shape: "command": {"path", "args", "env"}
            args, env, cmd = cmd.get("args"), cmd.get("env"), cmd.get("path")
        if isinstance(args, str):
            args = _split(args)
        self.args = [str(a) for a in args if isinstance(a, (str, int, float))] if isinstance(args, list) else []
        self.command = str(cmd) if isinstance(cmd, (str, int, float)) else ""
        if self.command and not self.args and re.search(r"\s", self.command.strip()) and not os.path.exists(self.command):
            toks = _split(self.command)
            self.command, self.args = toks[0], toks[1:]
        self.env = {str(k): ("" if v is None else str(v)) for k, v in env.items()} if isinstance(env, dict) else {}
        self.url = next((raw[k] for k in ("url", "serverUrl", "httpUrl", "endpoint") if isinstance(raw.get(k), str)), "")
        hdr = raw.get("headers")
        if not isinstance(hdr, dict) and isinstance(raw.get("requestInit"), dict):
            hdr = raw["requestInit"].get("headers")
        self.headers = {str(k): str(v) for k, v in hdr.items()} if isinstance(hdr, dict) else {}
        t = str(raw.get("type") or raw.get("transport") or "").lower()
        if not t:
            t = "stdio" if self.command else ("sse" if self.url.rstrip("/").endswith("/sse") else "http") if self.url else "unknown"
        self.transport = t
        self.disabled = raw.get("disabled") is True or raw.get("enabled") is False
        self.argv, self.idx_map = [self.command] + self.args, [-1] + list(range(len(self.args)))

    # findings
    def add(self, sev, code, msg, detail="", needle=None, **ev):
        line = (self.loc.value_line(needle, self.pos) if needle else None) or self.line
        key = "%s#%s:%s%s" % (self.file, self.name, code, ":" + detail if detail else "")
        ev.update({"file": self.file, "server": self.name, "line": line})
        for i, f in enumerate(self.findings):
            if f.key == key:
                if bc.SEV[sev] > bc.SEV[f.sev]:
                    self.findings[i] = bc.Finding(sev, code, msg, ev, key=key)
                return
        self.findings.append(bc.Finding(sev, code, msg, ev, key=key))

    def secret(self, raw, label, var):
        self.R.add(raw, label)
        self.secret_map.setdefault(raw, var)

    def edit(self, argv_index, new):
        if self.idx_map is None or argv_index >= len(self.idx_map) or self.idx_map[argv_index] < 0:
            self.notes.append("manual fix needed inside the shell command string (cannot rewrite it safely)")
            return
        self.edits[self.idx_map[argv_index]] = new

    def placeholder(self, var):
        style = "${env:%s}" if self.fa["shape"] == "vscode" or ".cursor" in self.file.replace("\\", "/") else "${%s}"
        return style % var


def _split(s):
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def check_secret(sa, kind, field, value, hint="", detail=None, var=None):
    """Bucket 1 for one value. kind: env | arg | header | input | url. Returns True when a finding was raised."""
    if not isinstance(value, str) or not value.strip():
        return False
    detail = detail or field
    var = var or env_var_name(hint or field)
    lit = literal_part(value)
    hit = find_token(value)
    if not hit and kind in ("env", "arg", "header", "input"):
        if BASE58_KEY_RX.fullmatch(lit):
            hit = ("BASE58_PRIVATE_KEY", lit)
        elif HEX_KEY_RX.fullmatch(lit) and KEYISH_HINT_RX.search(hint or ""):
            hit = ("HEX_PRIVATE_KEY", lit)
    if hit:
        label, raw = hit
        sa.secret(raw, label, var)
        sa.add("CRITICAL", "SEC_HARDCODED_TOKEN", "%s contains a hardcoded %s credential ([REDACTED:%s]); rotate it and load it "
               "from the environment as %s" % (field, label.replace("_", " ").lower(), label, sa.placeholder(var)),
               detail, needle=raw, label=label, field=field)
        return True
    if "://" in value:
        uc = url_credentials(value)
        if uc:
            what, raw = uc
            sa.secret(raw, "SECRET", var)
            sa.add("HIGH", "SEC_URL_CREDENTIALS", "%s embeds a literal credential in the URL %s ([REDACTED:SECRET]); use %s instead"
                   % (field, what, sa.placeholder(var)), detail, needle=raw, field=field)
            return True
    if kind in ("env", "arg", "header", "input") and (is_secret_name(hint) or (kind == "header" and SECRET_HEADER_RX.search(hint or ""))):
        if not lit or is_var_ref(value):
            return False
        if is_placeholder(lit):
            sa.add("INFO", "SEC_PLACEHOLDER", "%s is an unfilled placeholder; set it via %s so the server can authenticate"
                   % (field, sa.placeholder(var)), detail, field=field)
            return False
        if len(lit) >= 8 and not looks_nonsecret(lit):
            code = {"env": "SEC_LITERAL_SECRET_ENV", "arg": "SEC_LITERAL_SECRET_ARG", "header": "SEC_LITERAL_SECRET_HEADER",
                    "input": "SEC_LITERAL_SECRET_INPUT"}[kind]
            sa.secret(lit, "SECRET", var)
            sa.add("HIGH", code, "%s holds a literal secret value ([REDACTED:SECRET], %d chars); replace it with %s"
                   % (field, len(lit), sa.placeholder(var)), detail, needle=lit, field=field)
            return True
    if kind in ("env", "arg") and entropy_candidate(lit):
        sa.secret(lit, "SECRET", var)
        sa.add("MEDIUM", "SEC_HIGH_ENTROPY_LITERAL", "%s is a high-entropy literal (%.2f bits/char, %d chars) that looks like a "
               "credential ([REDACTED:SECRET]); if it is one, move it to %s" % (field, shannon(lit), len(lit), sa.placeholder(var)),
               detail, needle=lit, field=field)
        return True
    return False


def audit_secrets(sa):
    for k in sorted(sa.env):
        check_secret(sa, "env", "env `%s`" % k, sa.env[k], hint=k, detail="env.%s" % k, var=env_var_name(k))
    for h in sorted(sa.headers):
        v = sa.headers[h]
        if SECRET_HEADER_RX.search(h):
            sa.auth_present = True
        check_secret(sa, "header", "header `%s`" % h, v, hint=h, detail="headers.%s" % h,
                     var=env_var_name(sa.name, "TOKEN" if h.lower() in ("authorization", "proxy-authorization") else h))
    if sa.url:
        check_secret(sa, "url", "url", sa.url, detail="url", var=env_var_name(sa.name, "API_KEY"))
    argv, consumed = sa.argv, set()
    for i in range(1, len(argv)):
        if i in consumed:
            continue
        a = argv[i]
        m = SECRET_FLAG_RX.match(a)
        if m and a.startswith("-"):
            flag, val = m.group(1), m.group(2)
            if val is None and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                val = argv[i + 1]
                consumed.add(i + 1)
            if val is not None:
                check_secret(sa, "arg", "arg `--%s`" % flag, val, hint=flag, detail="args.%s" % flag, var=env_var_name(sa.name, flag))
            continue
        if a in ("-H", "--header", "--headers") or a.startswith("--header="):
            hv = a.split("=", 1)[1] if a.startswith("--header=") else (argv[i + 1] if i + 1 < len(argv) else "")
            if not a.startswith("--header="):
                consumed.add(i + 1)
            name, _, val = hv.partition(":")
            if SECRET_HEADER_RX.search(name.strip()):
                sa.auth_present = True
            check_secret(sa, "header", "arg header `%s`" % name.strip(), val.strip(), hint=name.strip(),
                         detail="args.header.%s" % name.strip(), var=env_var_name(sa.name, "TOKEN"))
            continue
        em = ENV_ASSIGN_RX.match(a)
        if em:
            hit = check_secret(sa, "env", "inline env `%s`" % em.group(1), em.group(2), hint=em.group(1),
                               detail="args.env.%s" % em.group(1), var=env_var_name(em.group(1)))
            if hit and argv[i - 1] in ("-e", "--env") and sa.idx_map is not None:
                sa.edit(i, em.group(1))  # docker passthrough: -e NAME reads it from the server's env block
                sa.env_add[em.group(1)] = sa.placeholder(env_var_name(em.group(1)))
            continue
        if not a.startswith("-") or "=" in a:
            val = a.split("=", 1)[1] if a.startswith("-") else a
            if i not in sa.pkg_positions:
                check_secret(sa, "arg", "args[%d]" % (i - 1), val, detail="args[%d]" % (i - 1), var=env_var_name(sa.name, "SECRET"))


def display_cmd(sa):
    out, argv, mask_next = [], sa.argv, False
    for i, a in enumerate(argv):
        if mask_next:
            out.append("[REDACTED]" if not is_var_ref(a) else a)
            mask_next = False
            continue
        m = SECRET_FLAG_RX.match(a) if a.startswith("-") else None
        em = ENV_ASSIGN_RX.match(a)
        if m and m.group(2) is not None and not is_var_ref(m.group(2)):
            a = a.split("=", 1)[0] + "=[REDACTED]"
        elif m and i > 0:
            mask_next = True
        elif em and is_secret_name(em.group(1)) and em.group(2) and not is_var_ref(em.group(2)):
            a = em.group(1) + "=[REDACTED]"
        elif i > 0 and argv[i - 1] in ("-H", "--header") and ":" in a:
            n, _, v = a.partition(":")
            if SECRET_HEADER_RX.search(n) and not is_var_ref(v.strip()):
                a = n + ": [REDACTED]"
        out.append(sa.R.text(a))
    s = " ".join(x if x and not re.search(r"\s", x) else json.dumps(x) for x in out if x is not None)
    return visible(s, 200)


# ---------------------------------------------------------------- bucket 2: commands, filesystem, docker, db
def unwrap_shell(sa):
    base = cmd_base(sa.command)
    if base not in SHELLS:
        return
    args = sa.args
    for i, a in enumerate(args):
        low = a.lower()
        if base in ("cmd",):
            hit = low in ("/c", "/k", "/s")
        elif base in ("powershell", "pwsh"):
            hit = low in ("-c", "-command", "-encodedcommand", "-enc", "-e", "-ec")
        else:
            hit = bool(re.fullmatch(r"-[a-z]*c[a-z]*", a))
        if not hit:
            continue
        if base in ("powershell", "pwsh") and low in ("-encodedcommand", "-enc", "-e", "-ec"):
            sa.add("CRITICAL", "CMD_REMOTE_PIPE_EXEC", "%s runs a base64-encoded command; the payload is hidden from review and can "
                   "fetch and execute anything" % base, "encoded", needle=a)
            sa.removed = True
            return
        separate = base in ("cmd", "powershell", "pwsh")
        script = " ".join(args[i + 1:]) if separate else (args[i + 1] if i + 1 < len(args) else "")
        if not script:
            return
        snippet = visible(sa.R.text(script), 90)
        if REMOTE_EXEC_RX.search(script):
            sa.add("CRITICAL", "CMD_REMOTE_PIPE_EXEC", "`%s %s` downloads and/or evaluates code at launch (`%s`); every agent start "
                   "runs whatever the remote host serves" % (base, a, snippet), "script", needle=args[i + 1])
            sa.removed = True
            sa.argv, sa.idx_map = [sa.command] + args, None
            return
        if METACHAR_RX.search(script):
            sa.add("HIGH", "CMD_SHELL_WRAPPER", "`%s %s` executes an inline shell pipeline/interpolation (`%s`); launch the server "
                   "binary directly with an explicit args list" % (base, a, snippet), "script", needle=args[i + 1])
            sa.argv, sa.idx_map = _split(script) or [sa.command], None
            return
        inner = _split(script)
        if not inner:
            return
        if base != "cmd":
            sa.add("LOW", "CMD_SHELL_WRAPPER", "server is launched through `%s %s` (`%s`); call the command directly to avoid "
                   "shell parsing surprises" % (base, a, snippet), "script", needle=args[i + 1])
        if separate:
            sa.argv, sa.idx_map = list(args[i + 1:]), list(range(i + 1, len(args)))
        else:
            sa.argv, sa.idx_map = inner, None
        return


def audit_flags(sa):
    argv = sa.argv
    for i in range(1, len(argv)):
        a = argv[i]
        name, _, val = a.partition("=")
        low = name.lower()
        sev = DANGEROUS_FLAGS.get(low)
        if low == "--allow-write" and val.strip() in ("/", "~", "C:\\", "C:/"):
            sev = "CRITICAL"
        if low == "-a" and cmd_base(argv[0]) == "deno":
            sev, name = "CRITICAL", "-A (deno --allow-all)"
        if sev:
            sa.add(sev, "CMD_DANGEROUS_FLAG", "dangerous permission flag `%s` disables a safety boundary for every tool call this "
                   "server makes" % visible(a, 60), "flag:" + low, needle=a)
            sa.edit(i, None)
            if low == "--allow-commands" and not val and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                sa.edit(i + 1, None)
    for key in ("alwaysAllow", "autoApprove", "autoapprove"):
        lst = sa.raw.get(key)
        if isinstance(lst, list) and lst:
            names = [str(x) for x in lst]
            risky = [n for n in names if n in ("*", "all") or DESTRUCTIVE_NAME_RX.search(n)]
            if risky:
                sa.add("HIGH", "CMD_AUTO_APPROVE", "`%s` auto-approves destructive/wildcard tools %s without a human confirmation"
                       % (key, ", ".join("`%s`" % visible(n, 40) for n in sorted(risky))), key, needle=key)
            else:
                sa.add("INFO", "CMD_AUTO_APPROVE", "`%s` auto-approves %d tool(s): %s" % (key, len(names), ", ".join(sorted(names))[:200]),
                       key, needle=key)
    if sa.raw.get("trust") is True:
        sa.add("HIGH", "CMD_AUTO_APPROVE", "`trust: true` bypasses every tool-call confirmation for this server", "trust", needle="trust")


def audit_filesystem(sa, pkg_name, pkg_index):
    if pkg_index is None:
        return
    seen = set()
    for j in range(pkg_index + 1, len(sa.argv)):
        p = sa.argv[j]
        if p.startswith("-"):
            continue
        cls = classify_path(p)
        if not cls:
            seen.add(p)
            continue
        kind, label = cls
        code = "FS_SENSITIVE_MOUNT" if kind == "SENSITIVE" else "FS_ROOT_EXPOSED"
        sa.add("CRITICAL", code, "filesystem server allows `%s` (%s): every file under it — SSH keys, cloud credentials, other MCP "
               "configs — is readable/writable by the model and any prompt-injected tool call" % (visible(p, 80), label),
               "path:" + p, needle=p, path=p)
        sa.edit(j, None if "/workspace/project" in seen else "/workspace/project")
        seen.add("/workspace/project")


def _parse_mount(spec):
    """-v value -> (src, dst, opts list)."""
    drive = ""
    if re.match(r"^[A-Za-z]:[\\/]", spec):
        drive, spec = spec[:2], spec[2:]
    parts = spec.split(":")
    src = drive + parts[0]
    return src, parts[1] if len(parts) > 1 else "", parts[2].split(",") if len(parts) > 2 else []


def audit_docker(sa, start):
    argv = sa.argv
    if start >= len(argv) or argv[start] not in ("run", "create"):
        return
    i = start + 1
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if not a.startswith("-"):
            sa.pkg_positions.add(i)
            audit_image(sa, i, a)
            return
        has_val = name in DOCKER_VALUE_FLAGS and not eq
        if has_val:
            val = argv[i + 1] if i + 1 < len(argv) else ""
        if name == "--privileged" and val.lower() not in ("false",):
            sa.add("CRITICAL", "DOCKER_PRIVILEGED", "container runs `--privileged`: full device access and trivial escape to the host",
                   "privileged", needle=a)
            sa.edit(i, None)
        elif name in ("--net", "--network", "--pid", "--ipc", "--uts", "--userns") and val == "host":
            sa.add("HIGH", "DOCKER_PRIVILEGED", "container shares the host %s namespace (`%s host`)" % (name.lstrip("-"), name),
                   "ns:" + name.lstrip("-"), needle=a)
            sa.edit(i, None)
            if has_val:
                sa.edit(i + 1, None)
        elif name == "--cap-add" and val.upper() in ("ALL", "SYS_ADMIN", "SYS_PTRACE", "NET_ADMIN", "SYS_MODULE"):
            sa.add("HIGH", "DOCKER_PRIVILEGED", "container adds kernel capability `%s`" % val.upper(), "cap:" + val.upper(), needle=a)
            sa.edit(i, None)
            if has_val:
                sa.edit(i + 1, None)
        elif name == "--security-opt" and "unconfined" in val:
            sa.add("HIGH", "DOCKER_PRIVILEGED", "container disables a kernel sandbox (`--security-opt %s`)" % val, "secopt", needle=a)
        elif name in ("-v", "--volume", "--mount"):
            vi = i + 1 if has_val else i
            if name == "--mount":
                kv = dict(x.split("=", 1) if "=" in x else (x, "true") for x in val.split(","))
                src = kv.get("source") or kv.get("src") or ""
                ro = kv.get("readonly") in ("true", "1") or kv.get("ro") in ("true", "1") or "readonly" in kv
                bind = kv.get("type", "volume") == "bind"
                fixed = ",".join(x for x in val.split(",")) + ("" if ro else ",readonly")
            else:
                src, dst, opts = _parse_mount(val)
                ro, bind = "ro" in opts or "readonly" in opts, is_hostpath(src)
                fixed = val if ro else val + (",ro" if opts else ":ro")
            if bind and src:
                cls = classify_path(src)
                if cls:
                    kind, label = cls
                    code = "FS_SENSITIVE_MOUNT" if kind == "SENSITIVE" else "FS_ROOT_EXPOSED"
                    extra = " — root-equivalent control of the host Docker daemon" if "docker.sock" in src else ""
                    sa.add("CRITICAL", code, "Docker volume mounts host `%s` (%s) into the server container%s"
                           % (visible(src, 80), label, extra), "mount:" + src, needle=val, path=src)
                    if "docker.sock" in src:
                        sa.edit(vi, None)
                        if has_val:
                            sa.edit(i, None)
                        fixed = None
                    else:
                        fixed = fixed.replace(src, "/workspace/project", 1)
                if not ro:
                    sa.add("HIGH", "DOCKER_WRITABLE_MOUNT", "host path `%s` is bind-mounted writable; add `:ro` unless the server "
                           "must write there" % visible(src, 80), "rw:" + src, needle=val, path=src)
                if fixed is not None and fixed != val:
                    sa.edit(vi, fixed)
        i += 2 if has_val else 1


def audit_image(sa, i, image):
    if "@sha256:" in image:
        return
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
    if not tag or tag.lower() in FLOATING_TAGS:
        sa.add("MEDIUM", "SUP_UNPINNED_IMAGE", "Docker image `%s` is %s; pin a version tag plus `@sha256:` digest so a pushed "
               "image cannot silently replace the server" % (image, "untagged (implicit :latest)" if not tag else "on floating tag `%s`" % tag),
               "image:" + image.split(":")[0], needle=image)
        sa.edit(i, (image.rsplit(":", 1)[0] if tag else image) + ":VERSION@sha256:DIGEST")


def audit_db(sa):
    blob = " ".join([sa.name, sa.command] + sa.args + list(sa.env) + list(sa.env.values()))
    if not DB_HINT_RX.search(blob) or any(p["name"] in DB_READONLY_PACKAGES for p in sa.packages):
        return
    if DB_READONLY_RX.search(blob):
        return
    prod = PROD_HOST_RX.search(" ".join(sa.args + list(sa.env.values())))
    sa.add("MEDIUM", "DB_WRITE_ACCESS", "database MCP server has no read-only flag or read-only role%s; any injected tool call can "
           "INSERT/UPDATE/DROP — connect with a read-only user or enable the server's read-only mode"
           % (" and points at a production-like host" if prod else ""), "db")


# ---------------------------------------------------------------- bucket 3: supply chain
def split_npm_spec(spec):
    if spec.startswith((".", "/", "~", "file:")) or re.match(r"^[A-Za-z]:[\\/]", spec):
        return "local", spec, None
    if spec.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:", "http://", "https://")) or re.match(r"^[\w.-]+/[\w.-]+(?:#.*)?$", spec):
        return "git", spec, None
    if spec.startswith("@"):
        idx = spec.find("@", 1)
        return "npm", (spec[:idx] if idx > 0 else spec), (spec[idx + 1:] if idx > 0 else None)
    name, _, ver = spec.partition("@")
    return "npm", name, ver or None


def split_py_spec(spec):
    if spec.startswith((".", "/", "~")):
        return "local", spec, None
    if spec.startswith("git+") or "://" in spec:
        return "git", spec, None
    m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*(==|@|>=|<=|~=|>|<|!=)?\s*(.*)$", spec)
    if not m:
        return "pypi", spec, None
    name, op, ver = m.group(1), m.group(2), m.group(3)
    if op in ("==", "@"):
        return "pypi", name, ver
    return "pypi", name, (op + ver) if op else None


def version_status(ver):
    if not ver:
        return "unpinned"
    if ver.lower() in FLOATING_TAGS:
        return "floating"
    return "pinned" if EXACT_VER_RX.match(ver) else "range"


def typosquat_target(name):
    if name in KNOWN_GOOD_PACKAGES:
        return None
    low = name.lower()
    if low.startswith("@") and "/" in low:
        scope, bare = low[1:].split("/", 1)
        if bare in OFFICIAL_BARE and scope != "modelcontextprotocol":
            return OFFICIAL_BARE[bare], "scope mismatch (`@%s` instead of `@modelcontextprotocol`)" % scope
    else:
        for pre in ("", "modelcontextprotocol-", "modelcontextprotocol_", "mcp-"):
            if pre and low.startswith(pre) and low[len(pre):] in OFFICIAL_BARE:
                return OFFICIAL_BARE[low[len(pre):]], "unscoped clone of an official @modelcontextprotocol package"
        if low in OFFICIAL_BARE:
            return OFFICIAL_BARE[low], "unscoped clone of an official @modelcontextprotocol package"
    best = None
    for c in OFFICIAL_PACKAGES:
        if len(c) < 8:
            continue
        d = levenshtein(low, c, cap=2)
        if 1 <= d <= 2 and (best is None or d < best[0]):
            best = (d, c)
    if best:
        return best[1], "edit distance %d" % best[0]
    return None


def audit_package(sa, launcher, eco, spec, idx, yes=True):
    kind, name, ver = (split_npm_spec if eco == "npm" else split_py_spec)(spec)
    sa.pkg_positions = getattr(sa, "pkg_positions", set()) | {idx}
    rec = {"launcher": launcher, "spec": spec, "name": name, "version": ver, "kind": kind, "official": name in KNOWN_GOOD_PACKAGES}
    if kind == "local":
        rec["status"] = "local"
        sa.packages.append(rec)
        return rec
    if kind == "git":
        pinned = bool(SHA_PIN_RX.search(spec))
        rec["status"] = "pinned" if pinned else "unpinned"
        if not pinned:
            sa.add("HIGH", "SUP_UNPINNED_GIT", "`%s %s` installs from a git ref without an immutable commit SHA; a force-push or "
                   "branch update changes the code on the next launch" % (launcher, visible(spec, 100)), "pkg:" + spec, needle=spec)
            base = re.sub(r"[#@][^/#@]*$", "", spec) if re.search(r"[#@][^/]*$", spec) and not spec.startswith("@") else spec
            sa.edit(idx, base + ("#COMMIT_SHA" if eco == "npm" else "@COMMIT_SHA"))
        sa.packages.append(rec)
        return rec
    status = version_status(ver)
    rec["status"] = status
    sa.packages.append(rec)
    squat = typosquat_target(name)
    if squat:
        target, why = squat
        rec["typosquat_of"] = target
        sa.add("CRITICAL", "SUP_TYPOSQUAT_SUSPECT", "package `%s` looks like a typosquat of `%s` (%s); verify the publisher before "
               "it runs with your credentials" % (name, target, why), "pkg:" + name, needle=spec, suggested=target)
        sa.edit(idx, target + "@VERSION")
        sa.notes.append("`%s` replaced with `%s` — confirm this is the package you intended" % (name, target))
        return rec
    code = "SUP_UNPINNED_NPX" if eco == "npm" else "SUP_UNPINNED_UVX"
    pin = "%s@VERSION" % name
    if status == "floating":
        sa.add("HIGH", "SUP_FLOATING_TAG", "`%s %s` follows the floating tag `@%s`; any new publish (or account takeover) runs "
               "automatically on next start" % (launcher, spec, ver), "pkg:" + name, needle=spec)
        sa.edit(idx, pin)
    elif status in ("unpinned", "range"):
        how = "with no version" if status == "unpinned" else "with the range `%s`" % ver
        auto = ("`-y` silently executes whatever is published to `latest` on every agent start" if eco == "npm" else
                "it resolves and runs the newest release on every agent start") if yes else "whatever is published at install time is executed"
        sa.add("HIGH" if yes else "MEDIUM", code, "`%s %s` runs %s; %s — pin an exact reviewed version"
               % (launcher, visible(spec, 80), how, auto), "pkg:" + name, needle=spec)
        sa.edit(idx, pin)
    return rec


def audit_launcher(sa):
    argv = sa.argv
    if not argv or not argv[0]:
        return None
    base, rest = cmd_base(argv[0]), argv[1:]
    off = 1
    node = None
    if base in ("npx", "bunx"):
        node = base
    elif base in ("npm", "pnpm", "yarn", "bun") and rest and rest[0] in ("exec", "dlx", "x"):
        node, rest, off = "%s %s" % (base, rest[0]), rest[1:], 2
    if node:
        yes, spec, sidx, i = node != "npx" and node != "npm exec", None, None, 0
        while i < len(rest):
            a = rest[i]
            if a in ("-y", "--yes"):
                yes = True
            elif a in ("-p", "--package") and i + 1 < len(rest):
                spec, sidx = rest[i + 1], off + i + 1
                i += 1
            elif a.startswith("--package="):
                spec, sidx = a.split("=", 1)[1], None
            elif a == "--":
                pass
            elif not a.startswith("-"):
                if spec is None:
                    spec, sidx = a, off + i
                break
            i += 1
        if spec:
            rec = audit_package(sa, node, "npm", spec, sidx if sidx is not None else 0, yes=yes)
            return rec, (off + i if i < len(rest) else len(argv) - 1)
        return None
    py = None
    if base == "uvx":
        py = "uvx"
    elif base == "uv" and rest[:2] == ["tool", "run"]:
        py, rest, off = "uv tool run", rest[2:], 3
    elif base == "pipx" and rest[:1] == ["run"]:
        py, rest, off = "pipx run", rest[1:], 2
    if py:
        spec, sidx, i = None, None, 0
        value_flags = {"--with", "-w", "--python", "-p", "--index-url", "--extra-index-url", "--index", "--with-requirements",
                       "--with-editable", "--constraint", "--python-version"}
        while i < len(rest):
            a = rest[i]
            if a in ("--from", "--spec") and i + 1 < len(rest):
                spec, sidx = rest[i + 1], off + i + 1
                i += 2
                continue
            if a in value_flags:
                i += 2
                continue
            if not a.startswith("-"):
                if spec is None:
                    spec, sidx = a, off + i
                break
            i += 1
        if spec:
            rec = audit_package(sa, py, "py", spec, sidx)
            return rec, off + i
        return None
    if base in ("docker", "podman"):
        audit_docker(sa, 1)
    return None


# ---------------------------------------------------------------- bucket 4: network
def host_of(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme.lower(), (parts.hostname or "").lower()
    except ValueError:
        return "", ""


def is_loopback(host):
    if host in LOOPBACK_HOSTS or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_metadata(host):
    if host in METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_link_local or ip.is_unspecified
    except ValueError:
        return False


def audit_url(sa, field, url, argv_index=None, endpoint=True):
    scheme, host = host_of(url)
    if scheme not in ("http", "https", "ws", "wss") or not host:
        return
    shown = visible(sa.R.text(url), 100)
    if is_metadata(host):
        sa.add("CRITICAL", "NET_METADATA_SSRF", "%s `%s` targets a cloud-metadata / unspecified address; tool traffic there can leak "
               "instance credentials" % (field, shown), "url:" + field, needle=url)
        sa.notes.append("%s points at a metadata address — remove this server" % field)
        return
    if is_loopback(host):
        return
    if scheme in ("http", "ws"):
        sa.add("CRITICAL", "NET_CLEARTEXT_REMOTE_HTTP", "%s `%s` uses cleartext %s to non-loopback host `%s`; prompts, tool results "
               "and auth headers can be read or altered in transit" % (field, shown, scheme, host), "cleartext:" + field, needle=url)
        fixed = ("https" if scheme == "http" else "wss") + url[len(scheme):]
        if argv_index is None:
            sa.url_fix = fixed
        else:
            sa.edit(argv_index, sa.argv[argv_index].replace(url, fixed))
    if not endpoint:
        return
    if host in PUBLIC_ENDPOINT_HOSTS:
        sa.add("INFO", "NET_PUBLIC_ENDPOINT", "%s `%s` is a known public read-only endpoint (no auth expected)" % (field, shown),
               "public:" + field)
        return
    oauth = any(k in sa.raw for k in ("oauth", "auth", "authorization", "authProviderType"))
    query_auth = any(k.lower() in URL_SECRET_PARAMS for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    env_auth = any(is_secret_name(k) for k in sa.env)
    if not (sa.auth_present or oauth or query_auth or (argv_index is not None and env_auth)):
        sa.add("MEDIUM", "NET_REMOTE_NO_AUTH", "remote MCP endpoint `%s` has no Authorization header or token configured; unless the "
               "client completes OAuth for it, anyone who can reach it can drive its tools" % shown, "noauth:" + field, needle=url)


def audit_network(sa):
    if sa.url:
        audit_url(sa, "url", sa.url)
    names = {p["name"] for p in sa.packages}
    bridge = bool(names & BRIDGE_PACKAGES) or cmd_base(sa.argv[0] if sa.argv else "") in BRIDGE_PACKAGES
    if sa.removed:
        return
    for i in range(1, len(sa.argv)):
        a = sa.argv[i]
        prev = sa.argv[i - 1].split("=", 1)[0].lower()
        endpoint = bridge or prev in ENDPOINT_FLAGS or a.split("=", 1)[0].lower() in ENDPOINT_FLAGS
        for m in re.finditer(r"(?:https?|wss?)://[^\s\"']+", a):
            if i not in sa.pkg_positions:
                audit_url(sa, "args[%d]" % (i - 1), m.group(0), i, endpoint=endpoint)
        if not bridge:
            continue
        name, _, val = a.partition("=")
        nxt = sa.argv[i + 1] if i + 1 < len(sa.argv) else ""
        if name == "--allow-http":
            sa.add("HIGH", "NET_INSECURE_BRIDGE", "bridge flag `--allow-http` permits cleartext upstream connections", "bridge:allow-http", needle=a)
            sa.edit(i, None)
        elif name in ("--cors", "--allow-origin", "--cors-origin") and (val in ("", "*") and (nxt in ("*", "") or nxt.startswith("-") or val == "*")):
            sa.add("HIGH", "NET_INSECURE_BRIDGE", "bridge enables wildcard CORS (`%s`); any web page can call the local MCP bridge" % visible(a + (" " + nxt if nxt == "*" else ""), 40),
                   "bridge:cors", needle=a)
            sa.edit(i, None)
            if nxt == "*":
                sa.edit(i + 1, None)
        elif "0.0.0.0" in a or (a == "::" and i > 1):
            sa.add("HIGH", "NET_INSECURE_BRIDGE", "bridge binds `0.0.0.0` (all interfaces); other hosts on the network can drive this "
                   "MCP server — bind 127.0.0.1", "bridge:bind", needle=a)
            sa.edit(i, a.replace("0.0.0.0", "127.0.0.1"))


# ---------------------------------------------------------------- bucket 5: tools
def _schema_texts(schema, path="inputSchema"):
    if isinstance(schema, dict):
        for k in ("description", "title", "default"):
            if isinstance(schema.get(k), str):
                yield "%s.%s" % (path, k), schema[k]
        for k, v in schema.items():
            if isinstance(v, (dict, list)):
                yield from _schema_texts(v, "%s.%s" % (path, k))
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _schema_texts(v, "%s[%d]" % (path, i))


def _schema_params(schema, out=None):
    out = out if out is not None else set()
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for k, v in props.items():
                out.add(str(k))
                _schema_params(v, out)
        for k in ("items", "anyOf", "oneOf", "allOf"):
            v = schema.get(k)
            for x in (v if isinstance(v, list) else [v]):
                _schema_params(x, out)
    return out


def norm_tools(lst):
    out = []
    for t in lst if isinstance(lst, list) else []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            out.append(t)
    return out


def parse_tools_doc(doc, default_server):
    """Any supported tools/list snapshot shape -> {server: [tool, ...]} or None when unrecognised."""
    if isinstance(doc, list):
        return {default_server: norm_tools(doc)}
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("result"), dict) and isinstance(doc["result"].get("tools"), list):
        doc = doc["result"]
    if isinstance(doc.get("servers"), dict):
        res = {}
        for s, v in doc["servers"].items():
            res[str(s)] = norm_tools(v.get("tools") if isinstance(v, dict) else v)
        return res
    if isinstance(doc.get("tools"), list):
        return {str(doc.get("server") or default_server): norm_tools(doc["tools"])}
    if doc and all(isinstance(v, list) for v in doc.values()):
        return {str(s): norm_tools(v) for s, v in doc.items()}
    return None


def tool_finding(sev, code, server, tool, msg, detail="", **ev):
    key = "tools:%s:%s:%s%s" % (server, code, tool, ":" + detail if detail else "")
    ev.update({"server": server, "tool": tool})
    return bc.Finding(sev, code, msg, ev, key=key)


def audit_tool(server, tool, source, R):
    fs = []
    name = tool["name"]
    shown = visible(name, 60)
    desc = tool.get("description") if isinstance(tool.get("description"), str) else ""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    texts = [("name", name), ("description", desc)] + ([("title", tool["title"])] if isinstance(tool.get("title"), str) else [])
    texts += list(_schema_texts(schema))
    hits, fields, snippet = [], [], ""
    for field, text in texts:
        inv = sorted({"U+%04X" % ord(c) for c in INVISIBLE_RX.findall(text)})
        if inv:
            hits.append("invisible-unicode(%s)" % ",".join(inv))
            fields.append(field)
            snippet = snippet or visible(text, 90)
        first = None
        for label, rx in INJECTION_RX:
            m = rx.search(text)
            if m:
                hits.append(label)
                fields.append(field)
                first = m if first is None or m.start() < first.start() else first
        if first is not None and not snippet:
            snippet = visible(text[max(0, first.start() - 20):first.end() + 40], 110)
    if hits:
        fs.append(tool_finding("CRITICAL", "TOOL_PROMPT_INJECTION", server, name, "tool `%s` carries hidden instructions aimed at the "
                               "model [%s] in %s: \"%s\" — do not enable this server" % (shown, ", ".join(sorted(set(hits))),
                                                                                     ", ".join(sorted(set(fields))), R.text(snippet)),
                               indicators=sorted(set(hits)), fields=sorted(set(fields)), source=source))
    params = _schema_params(schema)
    sus = sorted(p for p in params if re.sub(r"[\s-]+", "_", p.lower()) in SUSPICIOUS_PARAMS)
    if sus:
        fs.append(tool_finding("HIGH", "TOOL_SUSPICIOUS_PARAM", server, name, "tool `%s` asks the model for unrelated sensitive context "
                               "via parameter(s) %s — a classic exfiltration channel" % (shown, ", ".join("`%s`" % visible(p, 40) for p in sus)),
                               params=sus, source=source))
    ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    destructive = bool(DESTRUCTIVE_NAME_RX.search(name) or DESTRUCTIVE_DESC_RX.search(desc))
    write_params = sorted(p for p in params if p.lower() in WRITE_PARAMS)
    if ann.get("readOnlyHint") is True and (destructive or write_params):
        fs.append(tool_finding("HIGH", "TOOL_READONLY_LIE", server, name, "tool `%s` declares `readOnlyHint: true` but %s; clients may "
                               "auto-approve it as harmless" % (shown, "its name/description is destructive" if destructive else
                                                                "takes shell/SQL parameter(s) %s" % ", ".join(write_params)),
                               source=source))
    elif destructive and ann.get("destructiveHint") is False:
        fs.append(tool_finding("HIGH", "TOOL_READONLY_LIE", server, name, "tool `%s` declares `destructiveHint: false` but its "
                               "name/description is destructive" % shown, source=source))
    elif destructive and "destructiveHint" not in ann and ann.get("readOnlyHint") is not True:
        fs.append(tool_finding("MEDIUM", "TOOL_DESTRUCTIVE_UNAMBIGUOUS", server, name, "destructive/exec tool `%s` has no "
                               "`destructiveHint`/`readOnlyHint` annotations; keep human confirmation on for it" % shown, source=source))
    return fs


def audit_tools(tools_by_server, sources, R):
    findings, owners = [], {}
    for server in sorted(tools_by_server):
        for tool in tools_by_server[server]:
            findings += audit_tool(server, tool, sources.get(server, ""), R)
            owners.setdefault(tool["name"], set()).add(server)
    for name in sorted(owners):
        if len(owners[name]) > 1:
            srv = sorted(owners[name])
            findings.append(bc.Finding("HIGH", "TOOL_NAME_SHADOWING", "tool name `%s` is defined by %d servers (%s); a third-party server "
                                       "can shadow or hijack calls meant for the trusted one" % (visible(name, 60), len(srv), ", ".join(srv)),
                                       {"server": "(cross-server)", "tool": name, "servers": srv},
                                       key="tools:TOOL_NAME_SHADOWING:%s" % name))
    return findings


# ---------------------------------------------------------------- config files
def extract_servers(doc, explicit):
    """-> (shape, section_path, {name: raw}) or (None, None, {})."""
    if not isinstance(doc, dict):
        return None, None, {}
    for key, shape in (("mcpServers", "mcpServers"), ("context_servers", "zed"), ("servers", "vscode")):
        if isinstance(doc.get(key), dict):
            return shape, [key], doc[key]
    mcp = doc.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        return "vscode", ["mcp", "servers"], mcp["servers"]
    if explicit and doc and all(isinstance(v, dict) and set(v) & SERVER_KEYS for v in doc.values()):
        return "flat", [], doc
    return None, None, {}


TOP_KEY = {"mcpServers": "mcpServers", "zed": "context_servers", "vscode": "servers", "flat": "mcpServers"}


def audit_config(path, text, R, explicit=True):
    fa = {"path": path, "shape": None, "jsonc": [], "servers": [], "findings": [], "error": None, "remediation": None,
          "inline_tools": {}}
    clean, ext = strip_jsonc(text)
    try:
        doc = json.loads(clean)
    except ValueError as e:
        fa["error"] = "malformed JSON (%s)" % visible(str(e), 120)
        return fa
    loc = Locator(clean)
    fa["jsonc"] = ext
    shape, section, servers = extract_servers(doc, explicit)
    fa["shape"] = shape
    file_sa = ServerAudit(fa, "(file)", {}, loc, 0, R)
    file_sa.line = 1
    if ext:
        file_sa.add("LOW", "CFG_JSONC_EXTENSIONS", "file uses JSONC extensions (%s); they were stripped for this audit, but strict JSON "
                    "parsers in other clients/tools will reject the file" % ", ".join(ext), "jsonc")
    if shape is None:
        file_sa.add("INFO", "CFG_NO_SERVERS", "no MCP server section found (mcpServers / servers / context_servers)", "noservers")
        fa["findings"] = file_sa.findings
        return fa
    pos = 0
    for k in section:
        p = loc.key_pos(k, pos)
        pos = p if p >= 0 else pos
    if isinstance(doc.get("inputs"), list):
        audit_inputs(file_sa, doc["inputs"])
    for name in servers:
        raw = servers[name]
        if not isinstance(raw, dict):
            file_sa.add("INFO", "CFG_BAD_SERVER", "server entry `%s` is not an object; skipped" % visible(name, 60), "bad:" + name)
            continue
        sa = ServerAudit(fa, str(name), raw, loc, loc.key_pos(name, pos), R)
        audit_server(sa)
        fa["servers"].append(sa)
        if isinstance(raw.get("tools"), list):
            fa["inline_tools"][str(name)] = norm_tools(raw["tools"])
    fa["findings"] = file_sa.findings
    return fa


def audit_inputs(file_sa, inputs):
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        iid = str(inp.get("id") or "")
        if inp.get("type", "promptString") == "promptString" and is_secret_name(iid.replace("-", "_")) and inp.get("password") is not True:
            file_sa.add("LOW", "SEC_INPUT_NOT_MASKED", "input `%s` looks like a secret but is not declared `\"password\": true`; it is "
                        "echoed on screen when entered" % visible(iid, 60), "input:" + iid, needle=iid)
        if isinstance(inp.get("default"), str):
            check_secret(file_sa, "input", "input `%s` default" % iid, inp["default"], hint=iid.replace("-", "_") or "secret",
                         detail="inputs.%s.default" % iid, var=env_var_name(iid))


def audit_server(sa):
    if sa.disabled:
        sa.add("INFO", "CFG_SERVER_DISABLED", "server is disabled in this config (still audited: re-enabling it restores these risks)",
               "disabled")
    unwrap_shell(sa)
    sa.pkg_positions = set()
    launch = audit_launcher(sa)
    audit_secrets(sa)
    audit_flags(sa)
    if launch:
        rec, last = launch
        is_fs = "server-filesystem" in rec["name"] or rec["name"].endswith("/filesystem") or rec["name"] == "filesystem"
        if is_fs:
            idx = next(iter(sorted(sa.pkg_positions)), None)
            audit_filesystem(sa, rec["name"], idx)
    elif "filesystem" in sa.name.lower() and sa.argv and cmd_base(sa.argv[0]) in ("node", "bun", "deno", "mcp-server-filesystem"):
        audit_filesystem(sa, sa.name, 1 if cmd_base(sa.argv[0]) != "mcp-server-filesystem" else 0)
    audit_db(sa)
    audit_network(sa)


# ---------------------------------------------------------------- remediation
def harden_server(sa):
    raw = copy.deepcopy(sa.raw)
    if isinstance(raw.get("command"), dict):
        z = raw.pop("command")
        raw.update({k: v for k, v in (("args", z.get("args")), ("env", z.get("env"))) if v is not None})
        raw["command"] = z.get("path")
    if sa.command:
        args = list(sa.args)
        new = []
        for i, a in enumerate(args):
            if i in sa.edits:
                if sa.edits[i] is not None:
                    new.append(sa.edits[i])
            else:
                new.append(a)
        raw["command"], raw["args"] = sa.command, new
    if sa.env_add:
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        raw["env"] = dict(env, **sa.env_add)
    if sa.url_fix:
        for k in ("url", "serverUrl", "httpUrl", "endpoint"):
            if raw.get(k) == sa.url:
                raw[k] = sa.url_fix
    for key in ("alwaysAllow", "autoApprove", "autoapprove"):
        if isinstance(raw.get(key), list):
            raw[key] = [x for x in raw[key] if not (str(x) in ("*", "all") or DESTRUCTIVE_NAME_RX.search(str(x)))]
    if raw.get("trust") is True:
        raw["trust"] = False
    raw.pop("tools", None)

    def sub(o):
        if isinstance(o, str):
            for secret in sorted(sa.secret_map, key=len, reverse=True):
                if secret in o:
                    o = o.replace(secret, sa.placeholder(sa.secret_map[secret]))
            return o
        if isinstance(o, list):
            return [sub(x) for x in o]
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        return o
    return sa.R.obj(sub(raw))


def build_remediation(fa):
    if fa["shape"] is None:
        return None
    top = TOP_KEY[fa["shape"]]
    servers, notes, changed = {}, [], False
    for sa in fa["servers"]:
        if sa.removed:
            notes.append("`%s` removed: it executes remote or obfuscated shell code at launch and cannot be hardened automatically; "
                         "replace it with a pinned package" % sa.name)
            changed = True
            continue
        hard = harden_server(sa)
        if hard != sa.raw:
            changed = True
        servers[sa.name] = hard
        for n in dict.fromkeys(sa.notes):
            notes.append("`%s`: %s" % (sa.name, n))
        if sa.secret_map:
            notes.append("`%s`: rotate the exposed credential(s) now — removing them from the file does not revoke them; then export "
                         "%s in the launching environment" % (sa.name, ", ".join(sorted(set(sa.secret_map.values())))))
    if any(sa.secret_map for sa in fa["servers"]) and "claude_desktop_config" in fa["path"]:
        notes.append("Claude Desktop does not expand ${VAR} in env values; launch servers via a wrapper that reads a secret manager")
    snippet = {top: servers}
    if fa["shape"] == "vscode" and isinstance(fa.get("inputs"), list):
        snippet["inputs"] = fa["inputs"]
    return {"path": fa["path"], "shape": fa["shape"], "changed": changed, "snippet": snippet, "notes": notes}


# ---------------------------------------------------------------- inputs / discovery
def discover(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn in DISCOVER_NAMES or (fn == "settings.json" and os.path.basename(dirpath) == "zed"):
                found.append(full)
                if len(found) >= MAX_DISCOVERED:
                    return found
    return found


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, "file larger than %d bytes" % MAX_FILE_BYTES
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(), None
    except (OSError, UnicodeDecodeError) as e:
        return None, "unreadable (%s)" % visible(str(e), 120)


# ---------------------------------------------------------------- state + delta
def make_state(files, tools_by_server, findings, R):
    servers = {}
    for fa in files:
        for sa in fa["servers"]:
            servers["%s#%s" % (fa["path"], sa.name)] = sha16(R.obj(sa.raw))
    tools = {s: {t["name"]: {"desc": sha16(t.get("description") or ""), "schema": sha16(t.get("inputSchema") or {}),
                             "annotations": sha16(t.get("annotations") or {})} for t in ts}
             for s, ts in tools_by_server.items()}
    fbk = {f.key: f.to_dict() for f in bc.sort_findings(findings)}
    return R.obj({"servers": servers, "tools_by_server": tools, "findings_by_key": fbk})


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
        ev.append(bc.Finding(sev, code, msg, e, key="EVENT:%s:%s" % (code, key)))

    ps, cs = prev.get("servers") or {}, cur["servers"]
    for s in sorted(set(cs) - set(ps)):
        add("LOW", "SERVER_ADDED", "server `%s` added since baseline — review it before trusting" % s, s, server=s)
    for s in sorted(set(ps) - set(cs)):
        add("INFO", "SERVER_REMOVED", "server `%s` removed since baseline" % s, s, server=s)
    for s in sorted(set(ps) & set(cs)):
        if ps[s] != cs[s]:
            add("LOW", "SERVER_CONFIG_CHANGED", "server `%s` launch config changed since baseline" % s, s, server=s)
    pt, ct = prev.get("tools_by_server") or {}, cur["tools_by_server"]
    for s in sorted(ct):
        if s not in pt:
            continue
        for t in sorted(set(ct[s]) - set(pt[s])):
            add("LOW", "TOOL_ADDED", "server `%s` gained tool `%s` since baseline" % (s, visible(t, 60)), "%s:%s" % (s, t), server=s, tool=t)
        for t in sorted(set(pt[s]) - set(ct[s])):
            add("INFO", "TOOL_REMOVED", "server `%s` no longer exposes tool `%s`" % (s, visible(t, 60)), "%s:%s" % (s, t), server=s, tool=t)
        for t in sorted(set(pt[s]) & set(ct[s])):
            a, b = pt[s][t], ct[s][t]
            if a.get("desc") != b.get("desc"):
                add("HIGH", "TOOL_DESC_CHANGED", "tool `%s` on server `%s` changed its description after baseline (possible rug-pull); "
                    "re-review it before the model sees it" % (visible(t, 60), s), "%s:%s" % (s, t), server=s, tool=t)
            if a.get("schema") != b.get("schema"):
                add("MEDIUM", "TOOL_SCHEMA_CHANGED", "tool `%s` on server `%s` changed its input schema after baseline" % (visible(t, 60), s),
                    "%s:%s" % (s, t), server=s, tool=t)
            if a.get("annotations") != b.get("annotations"):
                add("LOW", "TOOL_ANNOTATIONS_CHANGED", "tool `%s` on server `%s` changed its annotations" % (visible(t, 60), s),
                    "%s:%s" % (s, t), server=s, tool=t)
    return ev


# ---------------------------------------------------------------- exit code
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


def _md(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def _loc(f):
    ev = f.evidence
    if ev.get("line"):
        return "L%d" % ev["line"]
    return ""


def _launch(sa):
    if sa.command:
        return display_cmd(sa)
    return visible(sa.R.text(sa.url), 120) if sa.url else "-"


def _pkg_summary(sa):
    out = []
    for p in sa.packages:
        out.append("%s%s (%s%s)" % (p["name"], "@" + p["version"] if p.get("version") else "", p["status"],
                                    ", typosquat?" if p.get("typosquat_of") else ", known" if p.get("official") else ""))
    return "; ".join(out) or "-"


def _delta_lines(delta):
    if delta is None:
        return []
    if delta.get("skipped"):
        return ["  skipped: %s" % delta["skipped"]]
    if delta.get("baseline"):
        return ["  baseline run: %d finding(s) recorded as NEW; no prior state" % len(delta["new"])]
    L = []
    for label in ("events", "new", "changed", "resolved"):
        for f in delta[label]:
            who = f.evidence.get("server") if label != "events" else None
            L.append("  %-8s [%s] %s: %s%s" % ("EVENT" if label == "events" else label.upper(), f.sev, f.code,
                                                "%s: " % who if who else "", f.msg))
    return L or ["  (no changes since baseline)"]


def render_text(ctx):
    L = [ctx["header"], "inputs     %d config file(s), %d server(s), %d tools snapshot(s), %d tool(s)"
         % (len(ctx["files"]), sum(len(fa["servers"]) for fa in ctx["files"]), len(ctx["tool_sources"]), ctx["tool_count"])]
    for fa in ctx["files"]:
        L += ["", "config  %s  shape=%s  servers=%d%s" % (fa["path"], fa["shape"] or "-", len(fa["servers"]),
                                                        "  [JSONC: %s]" % ", ".join(fa["jsonc"]) if fa["jsonc"] else "")]
        if fa["error"]:
            L.append("  DEGRADED %s" % fa["error"])
        for f in bc.sort_findings(fa["findings"]):
            L.append("  [%-8s] %s %s: %s" % (f.sev, f.code, _loc(f), f.msg))
        for sa in fa["servers"]:
            L.append("  server  %s  (%s) %s  L%s  worst=%s" % (sa.name, sa.transport, _launch(sa), sa.line or "?", _worst(sa.findings)))
            L += ["    [%-8s] %s %s: %s" % (f.sev, f.code, _loc(f), f.msg) for f in bc.sort_findings(sa.findings)] or ["    (no findings)"]
    if ctx["tool_findings"] or ctx["tool_count"]:
        L += ["", "tools   %s" % ", ".join("%s=%d" % (s, len(t)) for s, t in sorted(ctx["tools_by_server"].items()))]
        L += ["  [%-8s] %s %s: %s" % (f.sev, f.code, f.evidence.get("server"), f.msg) for f in bc.sort_findings(ctx["tool_findings"])] \
            or ["  (no tool findings)"]
    c = _counts(ctx["findings"])
    L += ["", "summary   %d file(s) %d server(s)  critical=%d high=%d medium=%d low=%d info=%d  worst=%s"
          % (len(ctx["files"]), sum(len(fa["servers"]) for fa in ctx["files"]), c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"],
             c["INFO"], _worst(ctx["findings"]))]
    if ctx["degraded"]:
        L.append("DEGRADED  %s (result may be incomplete; not a clean pass)" % "; ".join(ctx["degraded"]))
    if ctx["delta"] is not None:
        L += ["", "delta"] + _delta_lines(ctx["delta"])
    rem = [r for r in ctx["remediation"] if r and r["changed"]]
    if rem:
        L += ["", "hardened config (copy-paste after review; original files were not modified)"]
        for r in rem:
            L += ["  # %s" % r["path"]] + ["  " + x for x in json.dumps(r["snippet"], indent=2, ensure_ascii=False).splitlines()]
            L += ["  - " + n for n in r["notes"]]
    L += ["", "exit_code %d (fail-on=%s)" % (ctx["code"], ctx["fail_on"]), SCOPE_NOTE]
    return "\n".join(L)


def render_markdown(ctx):
    c = _counts(ctx["findings"])
    nserv = sum(len(fa["servers"]) for fa in ctx["files"])
    L = ["# MCP Server & Tool-Permission Security Audit", "", "```", ctx["header"], "```", "",
         "**Config files:** %d | **Servers:** %d | **Tools:** %d | **Worst:** %s | Critical %d · High %d · Medium %d · Low %d · "
         "Info %d | **Exit code:** %d (fail-on=%s)" % (len(ctx["files"]), nserv, ctx["tool_count"], _worst(ctx["findings"]),
                                                       c["CRITICAL"], c["HIGH"], c["MEDIUM"], c["LOW"], c["INFO"], ctx["code"], ctx["fail_on"]), ""]
    if ctx["degraded"]:
        L += ["> **DEGRADED** %s. Result may be incomplete; not a clean pass." % _md("; ".join(ctx["degraded"])), ""]
    L += ["## Server inventory", "", "| File | Server | Transport | Launch | Packages | Worst | Crit | High | Med | Low |",
          "|---|---|---|---|---|---|---:|---:|---:|---:|"]
    for fa in ctx["files"]:
        for sa in fa["servers"]:
            k = _counts(sa.findings)
            L.append("| `%s` | `%s` | %s | `%s` | %s | %s | %d | %d | %d | %d |" % (
                _md(fa["path"]), _md(sa.name), sa.transport, _md(_launch(sa)), _md(_pkg_summary(sa)), _worst(sa.findings),
                k["CRITICAL"], k["HIGH"], k["MEDIUM"], k["LOW"]))
    for fa in ctx["files"]:
        L += ["", "## `%s`" % fa["path"], "", "Shape: **%s**%s" % (fa["shape"] or "unrecognised",
                                                               " · JSONC extensions stripped: %s" % ", ".join(fa["jsonc"]) if fa["jsonc"] else "")]
        if fa["error"]:
            L += ["", "> **DEGRADED** %s" % _md(fa["error"])]
            continue
        rows = [(f, "(file)") for f in fa["findings"]] + [(f, sa.name) for sa in fa["servers"] for f in sa.findings]
        rows.sort(key=lambda x: (-bc.SEV[x[0].sev], x[1], x[0].code, x[0].key))
        L += ["", "| Severity | Server | Code | Line | Finding |", "|---|---|---|---|---|"]
        L += ["| %s | `%s` | `%s` | %s | %s |" % (f.sev, _md(s), f.code, _loc(f) or "-", _md(f.msg)) for f, s in rows] \
            or ["| - | - | - | - | no findings |"]
    if ctx["tool_count"] or ctx["tool_findings"]:
        L += ["", "## Tool schemas", "", "Servers: %s" % ", ".join("`%s` (%d tools)" % (s, len(t)) for s, t in sorted(ctx["tools_by_server"].items())),
              "", "| Severity | Server | Tool | Code | Finding |", "|---|---|---|---|---|"]
        L += ["| %s | `%s` | `%s` | `%s` | %s |" % (f.sev, _md(f.evidence.get("server")), _md(visible(f.evidence.get("tool", ""), 60)), f.code,
                                                  _md(f.msg)) for f in bc.sort_findings(ctx["tool_findings"])] or ["| - | - | - | - | no tool findings |"]
    rem = [r for r in ctx["remediation"] if r and r["changed"]]
    if rem:
        L += ["", "## Hardened configuration (copy-paste after review)", "",
              "Secrets become `${VAR}` references, packages are pinned (`@VERSION` / `#COMMIT_SHA` placeholders), root/home/credential "
              "paths are narrowed to `/workspace/project`, Docker mounts are read-only and privileges dropped, and remote `http://` "
              "becomes `https://`. The original files were **not** modified."]
        for r in rem:
            L += ["", "### `%s`" % r["path"], "", "```json", json.dumps(r["snippet"], indent=2, ensure_ascii=False), "```"]
            if r["notes"]:
                L += [""] + ["- %s" % n for n in r["notes"]]
    if ctx["delta"] is not None:
        L += ["", "## Delta", "", "```"] + _delta_lines(ctx["delta"]) + ["```"]
    L += ["", "---", "", SCOPE_NOTE, ""]
    return "\n".join(L)


def _delta_json(delta):
    if delta is None:
        return None
    return {k: (v if k in ("baseline", "skipped") else [f.to_dict() for f in v]) for k, v in delta.items()}


def _server_json(sa):
    return {"name": sa.name, "line": sa.line, "transport": sa.transport, "launch": _launch(sa), "disabled": sa.disabled,
            "env_keys": sorted(sa.env), "header_keys": sorted(sa.headers), "packages": sa.packages, "worst": _worst(sa.findings),
            "findings": [f.to_dict() for f in bc.sort_findings(sa.findings)]}


# ---------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = _Parser(prog="mcp_config_auditor.py", description="Read-only MCP server config & tool-permission security auditor")
    p.add_argument("paths", nargs="*", metavar="PATH", help="config files or directories (directories are discovered recursively)")
    p.add_argument("--config", action="append", default=[], metavar="PATH")
    p.add_argument("--dir", action="append", default=[], metavar="DIR")
    p.add_argument("--tools-json", action="append", default=[], metavar="PATH")
    p.add_argument("--offline", metavar="PATH")
    p.add_argument("--remediate", action="store_true", help="print only the hardened config snippets (JSON)")
    p.add_argument("--state", metavar="PATH")
    p.add_argument("--delta", action="store_true")
    p.add_argument("--format", type=str.lower, choices=["text", "markdown", "md", "json"], default="text")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", metavar="PATH")
    p.add_argument("--fail-on", type=str.lower, choices=["none", "info", "low", "medium", "high", "critical"], default="medium")
    return p


def _parse_now(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def collect_inputs(args):
    """-> (configs [(path, text|None, err, explicit)], tool_docs [(path, doc|None, err)], degraded notes, fixture)."""
    configs, tool_docs, degraded, fx = [], [], [], None
    if args.offline:
        try:
            with open(args.offline, "r", encoding="utf-8") as fh:
                fx = json.load(fh)
        except (OSError, ValueError) as e:
            raise UsageError("cannot read --offline bundle: %s" % e) from None
        if not isinstance(fx, dict):
            raise UsageError("--offline bundle must be a JSON object")
        for path, body in sorted((fx.get("configs") or fx.get("files") or {}).items()):
            if body is None:
                configs.append((path, None, "unreadable (offline bundle marks it missing)", True))
            else:
                configs.append((path, body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False), None, True))
        if fx.get("tools") is not None:
            tool_docs.append(("offline:tools", fx["tools"], None))
        for path, body in sorted((fx.get("tools_files") or {}).items()):
            tool_docs.append((path, body, None if body is not None else "unreadable (offline bundle marks it missing)"))
    explicit = list(args.config)
    dirs = list(args.dir)
    for p in args.paths:
        if os.path.isdir(p):
            dirs.append(p)
        elif os.path.isfile(p):
            explicit.append(p)
        else:
            raise UsageError("no such file or directory: %s" % p)
    for p in explicit:
        if not os.path.exists(p):
            raise UsageError("no such config file: %s" % p)
        text, err = read_text(p)
        configs.append((p, text, err, True))
    for d in dirs:
        if not os.path.isdir(d):
            raise UsageError("not a directory: %s" % d)
        found = discover(d)
        if not found:
            degraded.append("--dir %s: no MCP config files discovered" % d)
        for p in found:
            if any(p == c[0] for c in configs):
                continue
            text, err = read_text(p)
            if text is not None and os.path.basename(p) == "settings.json" and "context_servers" not in text:
                continue
            configs.append((p, text, err, False))
    for p in args.tools_json:
        if not os.path.exists(p):
            raise UsageError("no such tools snapshot: %s" % p)
        text, err = read_text(p)
        doc = None
        if text is not None:
            try:
                doc = json.loads(strip_jsonc(text)[0])
            except ValueError as e:
                err = "malformed JSON (%s)" % visible(str(e), 120)
        tool_docs.append((p, doc, err))
    if not configs and not tool_docs and not degraded:
        raise UsageError("nothing to audit (pass PATH ..., --config, --dir, --tools-json or --offline)")
    return configs, tool_docs, degraded, fx


def run(argv, out=None, now=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        if args.delta and not args.state:
            raise UsageError("--delta requires --state PATH")
        configs, tool_docs, degraded, fx = collect_inputs(args)
    except UsageError as e:
        print("error: %s" % e, file=err)
        return bc.EXIT_USAGE
    except SystemExit as e:  # --help
        return int(e.code or 0)

    fmt = "json" if args.json else ("markdown" if args.format == "md" else args.format)
    now = now or (_parse_now(fx.get("now")) if fx else None) or bc.utc_now()
    R = Redactor()
    files = []
    for path, text, e, explicit in configs:
        if text is None:
            degraded.append("%s: %s" % (path, e))
            files.append({"path": path, "shape": None, "jsonc": [], "servers": [], "findings": [], "error": e, "inline_tools": {}})
            continue
        fa = audit_config(path, text, R, explicit=explicit)
        if fa["error"]:
            degraded.append("%s: %s" % (path, fa["error"]))
        else:
            try:
                fa["inputs"] = json.loads(strip_jsonc(text)[0]).get("inputs")
            except (ValueError, AttributeError):
                fa["inputs"] = None
        files.append(fa)

    tools_by_server, sources = {}, {}
    for fa in files:
        for s, ts in fa["inline_tools"].items():
            tools_by_server.setdefault(s, []).extend(ts)
            sources.setdefault(s, fa["path"])
    for path, doc, e in tool_docs:
        if e:
            degraded.append("%s: %s" % (path, e))
            continue
        default = os.path.basename(path).split(".")[0] or "tools"
        parsed = parse_tools_doc(doc, default)
        if parsed is None:
            degraded.append("%s: unrecognised tools snapshot shape" % path)
            continue
        for s, ts in parsed.items():
            have = {t["name"] for t in tools_by_server.get(s, [])}
            tools_by_server.setdefault(s, []).extend(t for t in ts if t["name"] not in have)
            sources.setdefault(s, path)
    tool_findings = audit_tools(tools_by_server, sources, R)

    findings = []
    for fa in files:
        findings += fa["findings"]
        for sa in fa["servers"]:
            findings += sa.findings
    findings += tool_findings
    if not files and not tools_by_server and not degraded:
        degraded.append("no configs or tools audited")
    for fa in files:
        fa["remediation"] = build_remediation(fa) if not fa["error"] else None

    delta, events = None, []
    if args.state:
        store = bc.StateStore(args.state)
        prev = store.get(STATE_KEY)
        cur = make_state(files, tools_by_server, findings, R)
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
    endpoints = ["offline:" + os.path.basename(args.offline)] if args.offline else ["local-files"]
    header = bc.snapshot_header(TOOL, endpoints, None, now=now)
    remediation = [fa["remediation"] for fa in files]
    ctx = {"header": header, "files": files, "tools_by_server": tools_by_server, "tool_sources": sorted(set(sources.values())),
           "tool_count": sum(len(t) for t in tools_by_server.values()), "tool_findings": tool_findings, "findings": findings,
           "degraded": degraded, "delta": delta, "remediation": remediation, "code": code, "fail_on": args.fail_on}
    if args.remediate:
        text = json.dumps(R.obj({r["path"]: r["snippet"] for r in remediation if r}), indent=2, ensure_ascii=False, sort_keys=True)
    elif fmt == "json":
        c = _counts(findings)
        payload = {"tool": TOOL, "version": VERSION, "retrieved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "files": [{"path": fa["path"], "shape": fa["shape"], "jsonc": fa["jsonc"], "error": fa["error"],
                              "file_findings": [f.to_dict() for f in bc.sort_findings(fa["findings"])],
                              "servers": [_server_json(sa) for sa in fa["servers"]], "remediation": fa["remediation"]} for fa in files],
                   "tools": {"servers": {s: sorted(t["name"] for t in ts) for s, ts in sorted(tools_by_server.items())},
                             "findings": [f.to_dict() for f in bc.sort_findings(tool_findings)]},
                   "summary": {"files": len(files), "servers": sum(len(fa["servers"]) for fa in files), "counts": c,
                               "worst": _worst(findings), "events": [f.to_dict() for f in events]},
                   "findings": [f.to_dict() for f in bc.sort_findings(findings)],
                   "delta": _delta_json(delta), "exit_code": code, "fail_on": args.fail_on, "degraded_sources": degraded,
                   "endpoints": endpoints, "scope_note": SCOPE_NOTE}
        text = json.dumps(R.obj(payload), indent=2, sort_keys=True, ensure_ascii=False)
    elif fmt == "markdown":
        text = render_markdown(ctx)
    else:
        text = render_text(ctx)
    text = R.text(text)
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
