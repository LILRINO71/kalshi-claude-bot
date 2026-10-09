"""Claude as the brain, through the Claude Code CLI on your Claude subscription (no API key).

Same lean-call recipe as kalshibot.claude_cli (no tools, MCP servers or settings, empty working
directory, exact model IDs), but generic: any prompt, any JSON schema. Every call is logged to
data/usage.jsonl, so the dashboard's Usage tab counts day-trader calls too, and successful
answers are cached by (model, effort, system, schema, prompt), so re-running a backtest is free.
"""
import hashlib
import json
import subprocess
import tempfile
import threading
import time

from kalshibot.claude_cli import LEAN_FLAGS, MODEL_IDS, clean_env, find_claude

from . import store

_cache_lock = threading.Lock()
_cache = None


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


def ask(prompt, schema, *, system, model, effort, source, use_cache=True, timeout=600):
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
