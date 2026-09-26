"""Run decisions through the Claude Code CLI on the user's Claude subscription (no API key).

Every call is logged to data/usage.jsonl. Decisions are cached by (model, effort, prompt),
so re-running a backtest costs nothing.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import storage

SYSTEM_PROMPT = (
    "You are the decision engine of a paper-trading bot for Kalshi 15-minute crypto markets. "
    "Reason carefully about the numbers given, then return only the structured decision."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["UP", "DOWN", "SKIP"]},
        "probability_up": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["direction", "probability_up", "reason"],
}
# Without these the CLI loads MCP servers, settings and its tool prompt: ~72k tokens a call instead of ~1.3k.
LEAN_FLAGS = ["--tools", "", "--no-session-persistence", "--strict-mcp-config",
              "--setting-sources", "", "--disable-slash-commands"]

# Exact IDs: an inherited environment can remap the short aliases (we caught "sonnet" running as Haiku).
MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
}

_cache_lock = threading.Lock()
_cache = None


def clean_env():
    """Parent environment minus Claude Code session variables that can change model or behavior."""
    keep = {"CLAUDE_CONFIG_DIR", "CLAUDE_BIN"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not (k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_"))}


class ClaudeError(RuntimeError):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # error | limit | auth | missing | model_mismatch


def find_claude():
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if path:
        return path
    patterns = [
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "claude.exe"),
        # Microsoft Store installs keep their AppData inside the package folder.
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache",
                     "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
    ]
    found = [p for pat in patterns for p in glob.glob(pat)]
    if not found:
        return None

    def version_key(p):
        return [int(x) if x.isdigit() else 0 for x in Path(p).parent.name.split(".")]
    return max(found, key=version_key)


def _key(model, effort, prompt):
    return hashlib.sha256(f"{MODEL_IDS.get(model, model)}|{effort}|{SYSTEM_PROMPT}|{prompt}".encode()).hexdigest()[:24]


def _load_cache():
    global _cache
    if _cache is None:
        _cache = {r["key"]: r for r in storage.read_jsonl(storage.DECISION_CACHE_FILE) if "key" in r}
    return _cache


def cached_decision(model, effort, prompt):
    with _cache_lock:
        return _load_cache().get(_key(model, effort, prompt))


def decide(prompt, model, effort, source, use_cache=True):
    """Return {'direction', 'probability_up', 'reason', 'latency_s', 'cached'}."""
    key = _key(model, effort, prompt)
    if use_cache:
        with _cache_lock:
            hit = _load_cache().get(key)
        if hit:
            return {**hit["decision"], "latency_s": hit.get("latency_s", 0), "cached": True}

    claude = find_claude()
    if not claude:
        raise ClaudeError("Claude Code CLI not found. Install it or set CLAUDE_BIN.", "missing")

    wanted = MODEL_IDS.get(model, model)
    cmd = [claude, "-p", "--model", wanted, "--effort", effort, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM_PROMPT, *LEAN_FLAGS]
    t0 = time.time()
    record = {"time": time.time(), "model": model, "effort": effort, "source": source, "ok": False}
    try:
        # An empty temp dir keeps any project CLAUDE.md out of the decision.
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=clean_env(),
                                  encoding="utf-8", errors="replace", timeout=300, cwd=tmp)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if "Not logged in" in out or "Not logged in" in err:
            raise ClaudeError("Claude CLI is not logged in. Run `claude` in a terminal and use /login.", "auth")
        try:
            env = json.loads(out)
        except json.JSONDecodeError:
            raise ClaudeError(f"Unreadable CLI output: {(out or err)[:300]}")

        usage = env.get("usage") or {}
        per_model = env.get("modelUsage") or {}
        # The model that wrote the answer is the one with the most output tokens.
        main = max(per_model, key=lambda k: per_model[k].get("outputTokens", 0)) if per_model else None
        record.update({
            "model_id": main,
            "models_used": sorted(per_model),
            "input_tokens": usage.get("input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
            "api_equiv_usd": env.get("total_cost_usd") or 0,
        })
        if env.get("is_error"):
            msg = str(env.get("result") or env.get("terminal_reason") or "unknown error")
            kind = "limit" if "limit" in msg.lower() else "error"
            raise ClaudeError(msg[:300], kind)
        if main and main != wanted:
            # Never let a silent model swap into the results.
            raise ClaudeError(f"Asked for {wanted} but {main} answered", "model_mismatch")

        decision = env.get("structured_output")
        if not isinstance(decision, dict):
            text = env.get("result") or ""
            a, b = text.find("{"), text.rfind("}")
            decision = json.loads(text[a:b + 1]) if a >= 0 else None
        if not isinstance(decision, dict) or decision.get("direction") not in ("UP", "DOWN", "SKIP"):
            raise ClaudeError(f"Invalid decision: {str(env.get('result'))[:200]}")
        decision = {
            "direction": decision["direction"],
            "probability_up": min(1.0, max(0.0, float(decision.get("probability_up", 0.5)))),
            "reason": str(decision.get("reason", ""))[:1000],
        }
        record["ok"] = True
    except subprocess.TimeoutExpired:
        record["error"] = "timeout"
        raise ClaudeError("Claude CLI timed out after 300s")
    except ClaudeError as e:
        record["error"], record["error_kind"] = str(e), e.kind
        raise
    finally:
        record["latency_s"] = round(time.time() - t0, 2)
        storage.append_jsonl(storage.USAGE_FILE, record)

    row = {"key": key, "model": model, "effort": effort, "decision": decision,
           "latency_s": record["latency_s"], "time": time.time()}
    with _cache_lock:
        _load_cache()[key] = row
        storage.append_jsonl(storage.DECISION_CACHE_FILE, row)
    return {**decision, "latency_s": record["latency_s"], "cached": False}
