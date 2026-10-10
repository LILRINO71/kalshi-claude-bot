"""Claude as the brain, through the Claude Code CLI on your Claude subscription (no API key).

Calls are lean: no tools, MCP servers or settings, an empty working directory and a custom system
prompt, so each one carries about 1.3k tokens of overhead instead of ~72k. Models are pinned to exact
IDs and an answer from any other model is refused. Every call is logged to data/daytrader/usage.jsonl
(the dashboard's Usage tab), and successful answers are cached by (model, effort, system, schema,
prompt), so re-running a backtest is free.
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

from . import store

# Without these the CLI loads MCP servers, settings and its tool prompt: ~72k tokens a call instead of ~1.3k.
LEAN_FLAGS = ["--tools", "", "--no-session-persistence", "--strict-mcp-config",
              "--setting-sources", "", "--disable-slash-commands"]

# Exact IDs: an inherited environment can remap the short aliases (we once caught "sonnet" running as Haiku).
MODEL_IDS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
}

_cache_lock = threading.Lock()
_cache = None


def clean_env():
    """The parent environment minus Claude Code session variables that can change the model or behavior."""
    keep = {"CLAUDE_CONFIG_DIR", "CLAUDE_BIN"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not (k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_"))}


def find_claude():
    """The Claude Code CLI: $CLAUDE_BIN, PATH, the desktop app's copy, or the Microsoft Store package folder."""
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if path:
        return path
    patterns = [
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*", "LocalCache",
                     "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.expanduser("~/.local/bin/claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
    ]
    found = [p for pat in patterns for p in glob.glob(pat)]
    if not found:
        return None

    def version_key(p):
        return [int(x) if x.isdigit() else 0 for x in Path(p).parent.name.split(".")]
    return max(found, key=version_key)


class LLMError(RuntimeError):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # error | limit | auth | missing | model_mismatch | timeout | parse

    @property
    def fatal(self):
        """Errors that will repeat on every call, so a run should stop rather than retry."""
        return self.kind in ("limit", "auth", "missing", "model_mismatch")


def _key(model, effort, system, schema, prompt):
    raw = json.dumps([MODEL_IDS.get(model, model), effort, system, schema, prompt], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_cache():
    global _cache
    if _cache is None:
        _cache = {r["key"]: r for r in store.read_jsonl(store.LLM_CACHE) if "key" in r}
    return _cache


def _parse(env):
    """The structured answer from a CLI JSON envelope, or raise LLMError('parse')."""
    out = env.get("structured_output")
    if isinstance(out, dict):
        return out
    text = str(env.get("result") or "")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise LLMError(f"No JSON in answer: {text[:200]}", "parse")
    try:
        out = json.loads(text[a:b + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Unreadable JSON in answer ({e}): {text[:200]}", "parse")
    if not isinstance(out, dict):
        raise LLMError(f"Answer is not a JSON object: {text[:200]}", "parse")
    return out


def ask(prompt, schema, *, system, model, effort, source, use_cache=True, timeout=900):
    """Return (answer_dict, meta) where meta has latency_s, cached, tokens."""
    key = _key(model, effort, system, schema, prompt)
    if use_cache:
        with _cache_lock:
            hit = _load_cache().get(key)
        if hit:
            return hit["answer"], {"latency_s": hit.get("latency_s", 0), "cached": True, "tokens": 0}

    claude = find_claude()
    if not claude:
        raise LLMError("Claude Code CLI not found. Install it or set CLAUDE_BIN.", "missing")
    wanted = MODEL_IDS.get(model, model)
    cmd = [claude, "-p", "--model", wanted, "--effort", effort, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--system-prompt", system, *LEAN_FLAGS]
    t0 = time.time()
    record = {"time": t0, "model": model, "effort": effort, "source": source, "ok": False}
    try:
        try:
            # An empty temp dir keeps any project CLAUDE.md out of the decision.
            with tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=clean_env(),
                                      encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise LLMError(f"Claude CLI timed out after {timeout}s", "timeout")
        except OSError as e:
            raise LLMError(f"Could not run the Claude CLI at {claude}: {e}", "missing")
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if "Not logged in" in out or "Not logged in" in err:
            raise LLMError("Claude CLI is not logged in. Run `claude` in a terminal and use /login.", "auth")
        try:
            env = json.loads(out)
        except json.JSONDecodeError:
            raise LLMError(f"Unreadable CLI output: {(out or err)[:300]}")
        usage = env.get("usage") or {}
        per_model = env.get("modelUsage") or {}
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
            raise LLMError(msg[:300], "limit" if "limit" in msg.lower() else "error")
        if main and main != wanted:
            raise LLMError(f"Asked for {wanted} but {main} answered", "model_mismatch")
        answer = _parse(env)
        record["ok"] = True
    except LLMError as e:
        record["error"], record["error_kind"] = str(e), e.kind
        raise
    finally:
        record["latency_s"] = round(time.time() - t0, 2)
        store.append_jsonl(store.USAGE_FILE, record)

    tokens = sum(record.get(k, 0) for k in ("input_tokens", "cache_creation_tokens", "cache_read_tokens",
                                            "output_tokens"))
    row = {"key": key, "model": model, "effort": effort, "source": source, "answer": answer,
           "latency_s": record["latency_s"], "time": time.time()}
    with _cache_lock:
        _load_cache()[key] = row
        store.append_jsonl(store.LLM_CACHE, row)
    return answer, {"latency_s": record["latency_s"], "cached": False, "tokens": tokens}
