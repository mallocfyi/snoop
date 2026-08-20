#!/usr/bin/env python3
"""snoop - explore Claude Code session transcripts in your browser.

Usage:
    python3 snoop.py                  # searchable index of every session on disk
    python3 snoop.py <guid-or-prefix> # open one session
    python3 snoop.py --deep           # index full transcripts, not just prompts
    python3 snoop.py --list           # plain terminal listing

Everything is generated as static, self-contained HTML - no server, no network.
"""

import sys
import json
import webbrowser
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_OUT_DIR = Path.home() / ".snoop"


# ---------------------------------------------------------------------------
# Locating and loading sessions
# ---------------------------------------------------------------------------

def find_session_file(guid):
    matches = sorted(CLAUDE_PROJECTS_DIR.glob(f"*/{guid}*.jsonl"))
    exact = [m for m in matches if m.stem == guid]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Multiple sessions match '{guid}':\n")
        for m in matches:
            print(f"  {m.stem}  ({m.parent.name})")
        sys.exit(1)
    return None


def session_title(events):
    title = None
    for e in events:
        if e.get("type") == "ai-title" and e.get("aiTitle"):
            title = e["aiTitle"]
    if title:
        return title
    for e in events:
        if e.get("type") == "last-prompt" and e.get("lastPrompt"):
            return e["lastPrompt"][:80]
    return "(untitled session)"


def list_recent_sessions(limit=20):
    files = sorted(
        CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    print(f"Recent sessions (newest first, showing {len(files)}):\n")
    for f in files:
        events = load_events(f)
        title = session_title(events)
        project = f.parent.name
        print(f"  {f.stem}")
        print(f"    project: {project}")
        print(f"    title:   {title}\n")
    print("Run: python3 snoop.py <guid>")


def load_events(path):
    events = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


# ---------------------------------------------------------------------------
# Transforming raw events into a render-friendly transcript
# ---------------------------------------------------------------------------

def extract_text(content):
    """Flatten a tool_result's content (str, or list of content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(block, indent=2))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, indent=2)


LOCAL_COMMAND_TAGS = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<command-message>",
)


def looks_like_local_command_artifact(text):
    """CLI slash-command scaffolding (e.g. /model) that isn't real typed prose."""
    stripped = text.lstrip()
    return any(stripped.startswith(tag) for tag in LOCAL_COMMAND_TAGS)


def build_transcript(events):
    # Pass 1: index every tool_result by the tool_use_id it answers.
    tool_results = {}
    for e in events:
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_results[block.get("tool_use_id")] = {
                        "content": extract_text(block.get("content")),
                        "isError": bool(block.get("is_error")),
                    }

    turns = []
    meta_events = []

    for e in events:
        etype = e.get("type")

        if etype == "assistant":
            msg = e.get("message") or {}
            turn_idx = len(turns)
            blocks = []
            for b_idx, b in enumerate(msg.get("content") or []):
                bt = b.get("type")
                block_id = f"b{turn_idx}-{b_idx}"
                if bt == "text":
                    if b.get("text"):
                        blocks.append({
                            "kind": "text",
                            "id": block_id,
                            "category": "agent_response",
                            "text": b.get("text", ""),
                        })
                elif bt == "thinking":
                    if b.get("thinking"):
                        blocks.append({
                            "kind": "thinking",
                            "id": block_id,
                            "category": "thinking",
                            "text": b.get("thinking", ""),
                        })
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    blocks.append({
                        "kind": "tool_call",
                        "id": block_id,
                        "category": f"tool:{name}",
                        "name": name,
                        "input": b.get("input", {}),
                        "result": tool_results.get(b.get("id")),
                    })
            if blocks:
                usage = msg.get("usage") or {}
                turns.append({
                    "role": "assistant",
                    "uuid": e.get("uuid"),
                    "timestamp": e.get("timestamp"),
                    "model": msg.get("model"),
                    "usage": usage,
                    "blocks": blocks,
                    "isMeta": False,
                })

        elif etype == "user":
            msg = e.get("message") or {}
            content = msg.get("content")
            is_meta = bool(e.get("isMeta"))
            turn_idx = len(turns)

            if isinstance(content, str):
                block_is_meta = is_meta or looks_like_local_command_artifact(content)
                turns.append({
                    "role": "user",
                    "uuid": e.get("uuid"),
                    "timestamp": e.get("timestamp"),
                    "blocks": [{
                        "kind": "text",
                        "id": f"b{turn_idx}-0",
                        "category": "meta_prompt" if block_is_meta else "user_prompt",
                        "text": content,
                    }],
                    "isMeta": block_is_meta,
                })
            elif isinstance(content, list):
                only_tool_results = bool(content) and all(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
                if not only_tool_results:
                    blocks = []
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                            btext = b.get("text", "")
                            block_is_meta = is_meta or looks_like_local_command_artifact(btext)
                            blocks.append({
                                "kind": "text",
                                "id": f"b{turn_idx}-{len(blocks)}",
                                "category": "meta_prompt" if block_is_meta else "user_prompt",
                                "text": btext,
                            })
                    if blocks:
                        turns.append({
                            "role": "user",
                            "uuid": e.get("uuid"),
                            "timestamp": e.get("timestamp"),
                            "blocks": blocks,
                            "isMeta": all(b["category"] == "meta_prompt" for b in blocks),
                        })
                # else: a pure tool_result carrier - already merged into the
                # matching tool_call block above, nothing more to render.

        else:
            meta_events.append({
                "type": etype,
                "timestamp": e.get("timestamp"),
                "detail": summarize_meta(e),
            })

    return turns, meta_events


def summarize_meta(e):
    t = e.get("type")
    if t == "mode":
        return f"mode -> {e.get('mode')}"
    if t == "permission-mode":
        return f"permission mode -> {e.get('permissionMode')}"
    if t == "file-history-snapshot":
        n = len((e.get("snapshot") or {}).get("trackedFileBackups") or {})
        return f"file history snapshot ({n} tracked file(s))"
    if t == "attachment":
        a = e.get("attachment") or {}
        at = a.get("type")
        if at == "deferred_tools_delta":
            added = a.get("addedNames") or []
            return f"deferred tools added ({len(added)}): {', '.join(added[:6])}" + (
                "..." if len(added) > 6 else ""
            )
        return f"attachment: {at}"
    if t == "ai-title":
        return f"session title set: {e.get('aiTitle')}"
    if t == "last-prompt":
        return f"last prompt: {e.get('lastPrompt', '')[:100]}"
    if t == "system":
        if e.get("subtype") == "turn_duration":
            return f"turn duration: {e.get('durationMs')}ms ({e.get('messageCount')} messages)"
        return f"system: {e.get('subtype')}"
    return json.dumps(e)[:200]


# ---------------------------------------------------------------------------
# Sidebar navigation index (one entry per block: user prompt, agent
# response, thinking, or a specific tool call)
# ---------------------------------------------------------------------------

def tool_preview(block):
    inp = block.get("input") or {}
    for key in ("description", "command", "file_path", "path", "pattern", "query", "prompt", "url", "content"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())[:70]
    if inp:
        first_key = next(iter(inp))
        return f"{first_key}: {str(inp[first_key])[:50]}"
    return ""


def preview_for_block(b):
    if b["kind"] in ("text", "thinking"):
        return " ".join(b["text"].split())[:70]
    if b["kind"] == "tool_call":
        return tool_preview(b)
    return ""


def build_nav(turns):
    nav = []
    category_counts = {}
    tool_names = set()
    for turn in turns:
        for b in turn["blocks"]:
            cat = b["category"]
            category_counts[cat] = category_counts.get(cat, 0) + 1
            if cat.startswith("tool:"):
                tool_names.add(cat[len("tool:"):])
            nav.append({
                "id": b["id"],
                "category": cat,
                "timestamp": turn["timestamp"],
                "preview": preview_for_block(b),
            })
    return nav, category_counts, sorted(tool_names)


# ---------------------------------------------------------------------------
# Cross-session inventory (for the index page)
# ---------------------------------------------------------------------------

def project_path(events, fallback_dirname):
    """Real cwd, read from the events themselves.

    The directory name encodes the cwd with '/' replaced by '-', which is
    lossy: '-home-dev-inventory-api' could decode to either '.../inventory-api'
    or '.../inventory/api'. Every event carries the actual cwd, so prefer that
    and only fall back to the ambiguous decode.
    """
    for e in events:
        cwd = e.get("cwd")
        if cwd:
            return cwd
    return fallback_dirname.replace("-", "/")


def session_stats(path, events):
    """Everything the index page needs to describe and search one session."""
    prompts = []
    tools = {}
    models = {}
    timestamps = []
    files_touched = set()
    out_tokens = 0
    n_tool_calls = 0
    n_errors = 0

    for e in events:
        if e.get("timestamp"):
            timestamps.append(e["timestamp"])
        etype = e.get("type")

        if etype == "assistant":
            msg = e.get("message") or {}
            if msg.get("model"):
                models[msg["model"]] = models.get(msg["model"], 0) + 1
            out_tokens += (msg.get("usage") or {}).get("output_tokens") or 0
            for b in msg.get("content") or []:
                if b.get("type") != "tool_use":
                    continue
                n_tool_calls += 1
                name = b.get("name") or "tool"
                tools[name] = tools.get(name, 0) + 1
                if name in ("Edit", "Write", "NotebookEdit"):
                    fp = (b.get("input") or {}).get("file_path")
                    if fp:
                        files_touched.add(fp)

        elif etype == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, str):
                if not e.get("isMeta") and not looks_like_local_command_artifact(content):
                    prompts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                        n_errors += 1

    timestamps.sort()
    start = timestamps[0] if timestamps else None
    end = timestamps[-1] if timestamps else None

    return {
        "_path": str(path),  # stripped before the payload is serialized into the page
        "guid": path.stem,
        "project": project_path(events, path.parent.name),
        "projectDir": path.parent.name,
        "title": session_title(events),
        "start": start,
        "end": end,
        "durationMs": duration_ms(start, end),
        "activeMs": active_ms(timestamps),
        "events": len(events),
        "prompts": prompts,
        "nPrompts": len(prompts),
        "nToolCalls": n_tool_calls,
        "nErrors": n_errors,
        "filesTouched": sorted(files_touched),
        "outTokens": out_tokens,
        "topTools": sorted(tools.items(), key=lambda kv: -kv[1])[:4],
        "models": sorted(models, key=lambda m: -models[m]),
        "sizeBytes": path.stat().st_size,
    }


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def duration_ms(start, end):
    """Wall-clock span from first to last event."""
    a, b = parse_ts(start or ""), parse_ts(end or "")
    if not a or not b:
        return None
    return int((b - a).total_seconds() * 1000)


IDLE_GAP_MS = 5 * 60 * 1000


def active_ms(timestamps):
    """Time actually spent working, ignoring idle stretches.

    Wall-clock span badly overstates sessions that were resumed hours or days
    later - a session touched on Monday and again on Wednesday spans 48h but
    may hold 20 minutes of work. Summing only the sub-IDLE_GAP_MS gaps between
    consecutive events approximates the real figure.
    """
    total = 0
    prev = None
    for ts in timestamps:
        t = parse_ts(ts)
        if t is None:
            continue
        if prev is not None:
            gap = (t - prev).total_seconds() * 1000
            if 0 <= gap <= IDLE_GAP_MS:
                total += gap
        prev = t
    return int(total)


def collect_sessions(deep=False):
    """Load every session on disk and summarize it for the index."""
    sessions = []
    for path in sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl")):
        try:
            events = load_events(path)
        except OSError:
            continue
        if not events:
            continue
        stats = session_stats(path, events)
        if deep:
            # Full transcript text, so the index can search assistant output
            # and tool results too - much larger, hence opt-in.
            turns, _ = build_transcript(events)
            body = []
            for turn in turns:
                for b in turn["blocks"]:
                    if b["kind"] in ("text", "thinking"):
                        body.append(b["text"])
                    elif b["kind"] == "tool_call":
                        body.append(json.dumps(b.get("input") or {}))
                        if b.get("result"):
                            body.append((b["result"].get("content") or "")[:4000])
            stats["body"] = "\n".join(body)
        sessions.append(stats)
    sessions.sort(key=lambda s: s.get("start") or "", reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Cost estimation
#
# List prices in USD per million tokens, current as of 2026-08-19. These are
# Anthropic first-party API rates; Bedrock and Vertex bill separately, and any
# negotiated discount makes the real figure lower. Treat the output as an
# estimate at list price, which is what the UI labels it.
# ---------------------------------------------------------------------------

MODEL_RATES = {
    # model id prefix:  (input $/Mtok, output $/Mtok)
    "claude-fable-5":    (10.00, 50.00),
    "claude-mythos-5":   (10.00, 50.00),
    "claude-opus-5":     (5.00, 25.00),
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-opus-4-5":   (5.00, 25.00),
    "claude-sonnet-5":   (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00, 5.00),
}

# Sonnet 5 launched with introductory pricing that runs through 2026-08-31.
# The start date isn't published, but Sonnet 5 is recent enough that every
# session in a local archive predating the end date was billed at intro rates.
SONNET_5_INTRO = (2.00, 10.00)
SONNET_5_INTRO_ENDS = "2026-08-31"

# Cache reads bill at ~0.1x the input rate; writes carry a premium that
# depends on TTL - 1.25x for the 5-minute cache, 2x for the 1-hour cache.
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = {"ephemeral_5m_input_tokens": 1.25, "ephemeral_1h_input_tokens": 2.00}


def rates_for(model, timestamp=None):
    """(input, output) $/Mtok for a model id, or None if it isn't billable.

    Claude Code writes a '<synthetic>' model on locally generated messages;
    those never hit the API and must not be priced.
    """
    if not model or model.startswith("<"):
        return None
    if model.startswith("claude-sonnet-5"):
        if timestamp and timestamp[:10] <= SONNET_5_INTRO_ENDS:
            return SONNET_5_INTRO
        return MODEL_RATES["claude-sonnet-5"]
    # Longest prefix wins, so a dated id (claude-haiku-4-5-20251001) still matches.
    best = max((p for p in MODEL_RATES if model.startswith(p)), key=len, default=None)
    return MODEL_RATES[best] if best else None


def message_cost(model, usage, timestamp=None):
    """Estimated list-price cost of one assistant message, in USD."""
    rates = rates_for(model, timestamp)
    if not rates:
        return 0.0
    inp, out = rates

    creation = usage.get("cache_creation") or {}
    write_cost = sum(
        (creation.get(field) or 0) * inp * mult
        for field, mult in CACHE_WRITE_MULT.items()
    )
    if not creation:
        # Older logs carry only the flat total; assume the cheaper 5m tier.
        write_cost = (usage.get("cache_creation_input_tokens") or 0) * inp * 1.25

    total = (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * out
        + (usage.get("cache_read_input_tokens") or 0) * inp * CACHE_READ_MULT
        + write_cost
    )
    return total / 1_000_000


# ---------------------------------------------------------------------------
# Session summary - what actually happened
# ---------------------------------------------------------------------------

MUTATING_TOOLS = ("Edit", "Write", "NotebookEdit")


def build_summary(events, turns):
    """Answer the questions people open a transcript with.

    Token and cost totals come from `events` so nothing is missed, while the
    file and command lists come from `turns` because only those carry the
    block ids the UI needs to link to.
    """
    tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    models = {}
    cost = 0.0
    timestamps = []

    for e in events:
        if e.get("timestamp"):
            timestamps.append(e["timestamp"])
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        usage = msg.get("usage") or {}
        model = msg.get("model")
        if model:
            models[model] = models.get(model, 0) + 1
        tokens["input"] += usage.get("input_tokens") or 0
        tokens["output"] += usage.get("output_tokens") or 0
        tokens["cacheRead"] += usage.get("cache_read_input_tokens") or 0
        tokens["cacheWrite"] += usage.get("cache_creation_input_tokens") or 0
        cost += message_cost(model, usage, e.get("timestamp"))

    files = {}
    commands = []
    errors = []
    tool_counts = {}
    n_prompts = 0

    for turn in turns:
        if turn["role"] == "user" and not turn.get("isMeta"):
            n_prompts += 1
        for b in turn["blocks"]:
            if b["kind"] != "tool_call":
                continue
            name = b.get("name") or "tool"
            tool_counts[name] = tool_counts.get(name, 0) + 1
            inp = b.get("input") or {}
            result = b.get("result")

            if result and result.get("isError"):
                errors.append({"id": b["id"], "tool": name,
                               "preview": (result.get("content") or "")[:120]})

            if name in MUTATING_TOOLS:
                path = inp.get("file_path")
                if path:
                    entry = files.setdefault(path, {"path": path, "count": 0, "id": b["id"]})
                    entry["count"] += 1
            elif name == "Bash":
                cmd = inp.get("command")
                if cmd:
                    commands.append({
                        "id": b["id"],
                        "cmd": " ".join(cmd.split())[:200],
                        "description": inp.get("description") or "",
                        "isError": bool(result and result.get("isError")),
                    })

    timestamps.sort()
    unpriced = sorted(m for m in models if rates_for(m) is None)

    return {
        "activeMs": active_ms(timestamps),
        "durationMs": duration_ms(timestamps[0] if timestamps else None,
                                  timestamps[-1] if timestamps else None),
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
        "nPrompts": n_prompts,
        "nToolCalls": sum(tool_counts.values()),
        "tokens": tokens,
        "costUsd": round(cost, 4),
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
        "unpricedModels": unpriced,
        "topTools": sorted(tool_counts.items(), key=lambda kv: -kv[1]),
        "files": sorted(files.values(), key=lambda f: -f["count"]),
        "commands": commands,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

THEME_CSS = r""":root {
  --bg: #f7f7f5;
  --panel: #ffffff;
  --border: #e3e1dc;
  --text: #2a2a26;
  --text-dim: #75726a;
  --accent: #b45309;
  --accent-soft: #fef3e2;
  --user-bg: #eef2ff;
  --user-border: #c7d2fe;
  --assistant-bg: #ffffff;
  --tool-bg: #f4f3ef;
  --tool-border: #ddd9cf;
  --error-bg: #fdecea;
  --error-border: #f3aaa1;
  --mark: #ffe58a;
  --mark-current: #ff9f43;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --jv-key: #7c3aed;
  --jv-string: #0f766e;
  --jv-number: #1d4ed8;
  --jv-boolean: #be185d;
  --badge-user-bg: #c7d2fe;
  --badge-user-fg: #1e2a5e;
  --badge-assistant-bg: #fde7c7;
  --badge-assistant-fg: #7a4a00;
  --badge-tool-bg: #e5e2da;
  --badge-tool-fg: #4a4638;
  --badge-meta-bg: #eeece6;
  --badge-meta-fg: #8a8678;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1b18;
    --panel: #242320;
    --border: #38362f;
    --text: #ecebe6;
    --text-dim: #9c988c;
    --accent: #f0b45e;
    --accent-soft: #3a2d16;
    --user-bg: #232a44;
    --user-border: #3a4470;
    --assistant-bg: #242320;
    --tool-bg: #2a2924;
    --tool-border: #423f36;
    --error-bg: #3a1f1c;
    --error-border: #7a3a33;
    --mark: #6b5215;
    --mark-current: #a3720c;
    --jv-key: #c4b5fd;
    --jv-string: #5eead4;
    --jv-number: #93c5fd;
    --jv-boolean: #f9a8d4;
    --badge-user-bg: #334072;
    --badge-user-fg: #c7d2fe;
    --badge-assistant-bg: #4a3419;
    --badge-assistant-fg: #f0b45e;
    --badge-tool-bg: #3a3830;
    --badge-tool-fg: #d8d4c8;
    --badge-meta-bg: #2e2c26;
    --badge-meta-fg: #8a8678;
  }
}
"""


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>snoop - __TITLE__</title>
<style>
__THEME_CSS__
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.5;
}
#layout {
  display: grid;
  grid-template-columns: 320px 6px 1fr;
  height: 100vh;
}
#sidebar {
  background: var(--panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
#resizer {
  cursor: col-resize;
  background: var(--border);
  position: relative;
}
#resizer:hover, #resizer.dragging { background: var(--accent); }
#resizer::before {
  content: '';
  position: absolute;
  top: 0; bottom: 0;
  left: -4px; right: -4px;
}
#sidebar-header {
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border);
}
#sidebar-header .brand {
  font-weight: 700;
  font-size: 15px;
  color: var(--accent);
}
#sidebar-header .session-title {
  margin-top: 4px;
  font-size: 12.5px;
  color: var(--text-dim);
}
#sidebar-header .back-link {
  display: inline-block;
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--accent);
  text-decoration: none;
}
#sidebar-header .back-link:hover { text-decoration: underline; }
#sidebar-header .meta-line {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-dim);
  font-family: var(--mono);
}
#filter-details { border-bottom: 1px solid var(--border); flex-shrink: 0; }
#filter-details summary {
  cursor: pointer;
  padding: 8px 16px;
  font-size: 12px;
  color: var(--text-dim);
  font-weight: 600;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
#filter-details summary::-webkit-details-marker { display: none; }
#filter-details summary .chev { font-size: 9px; }
#filter-body { padding: 2px 16px 10px; }
.filter-actions { display: flex; gap: 6px; margin-bottom: 8px; }
.filter-actions button {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  cursor: pointer;
}
.filter-actions button:hover { border-color: var(--accent); }
#filter-list { display: flex; flex-direction: column; gap: 4px; max-height: 260px; overflow-y: auto; }
.filter-row { display: flex; align-items: center; gap: 6px; font-size: 11.5px; cursor: pointer; }
.filter-row input { cursor: pointer; margin: 0; }
.filter-count { color: var(--text-dim); font-size: 10.5px; margin-left: auto; }
#nav-list { list-style: none; margin: 0; padding: 6px; flex: 1; overflow-y: auto; min-height: 0; }
.nav-item {
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 11.5px;
  display: flex;
  gap: 7px;
  align-items: center;
  color: var(--text-dim);
}
.nav-item:hover { background: var(--tool-bg); }
.nav-item.active { background: var(--accent-soft); color: var(--text); }
.nav-time { font-family: var(--mono); font-size: 10px; opacity: 0.65; flex-shrink: 0; width: 58px; }
.nav-preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.nav-badge {
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 1px 5px;
  border-radius: 4px;
  flex-shrink: 0;
  white-space: nowrap;
}
.badge-user { background: var(--badge-user-bg); color: var(--badge-user-fg); }
.badge-assistant { background: var(--badge-assistant-bg); color: var(--badge-assistant-fg); }
.badge-thinking { background: transparent; border: 1px dashed var(--text-dim); color: var(--text-dim); }
.badge-meta { background: var(--badge-meta-bg); color: var(--badge-meta-fg); }
.badge-tool { background: var(--badge-tool-bg); color: var(--badge-tool-fg); }
#main { overflow-y: auto; padding: 20px 28px 80px; min-height: 0; }
#toolbar {
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--bg);
  padding: 10px 0 14px;
  display: flex;
  gap: 10px;
  align-items: center;
  border-bottom: 1px solid var(--border);
  margin-bottom: 18px;
}
#search-input {
  flex: 1;
  max-width: 420px;
  padding: 7px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font-size: 13px;
}
#search-input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
#search-count { font-size: 12px; color: var(--text-dim); min-width: 60px; }
button.nav-btn, button.toggle-btn {
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: 12.5px;
}
button.nav-btn:hover, button.toggle-btn:hover { border-color: var(--accent); }
button.toggle-btn.active { background: var(--accent-soft); border-color: var(--accent); }
.turn {
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  scroll-margin-top: 70px;
}
.turn.user { background: var(--user-bg); border-color: var(--user-border); }
.turn.assistant { background: var(--assistant-bg); }
.turn.meta-msg { opacity: 0.7; }
.turn-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
  font-size: 11.5px;
  color: var(--text-dim);
  font-family: var(--mono);
}
.turn-header .role-label { font-weight: 700; color: var(--text); font-family: var(--sans); }
.block { margin-bottom: 10px; scroll-margin-top: 70px; }
.block:last-child { margin-bottom: 0; }
.block-text { white-space: pre-wrap; word-wrap: break-word; }
details.thinking {
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  background: transparent;
}
details.thinking summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--text-dim);
  font-style: italic;
}
details.thinking .block-text {
  margin-top: 8px;
  font-size: 12.5px;
  color: var(--text-dim);
  font-style: italic;
}
.tool-card {
  border: 1px solid var(--tool-border);
  background: var(--tool-bg);
  border-radius: 8px;
  overflow: hidden;
}
.tool-card-header {
  padding: 8px 12px;
  font-family: var(--mono);
  font-size: 12.5px;
  font-weight: 600;
  display: flex;
  gap: 8px;
  align-items: center;
}
.tool-card-header .tool-icon { color: var(--accent); }
.tool-card pre {
  margin: 0;
  padding: 10px 12px;
  font-family: var(--mono);
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-wrap: break-word;
  border-top: 1px solid var(--tool-border);
}
.tool-card .result-wrap.error pre,
.tool-card .result-wrap.error .jv-wrap { background: var(--error-bg); }
.tool-card .result-label {
  padding: 6px 12px 0;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
}
.tool-card .no-result {
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-dim);
  font-style: italic;
}
.jv-wrap {
  padding: 8px 12px;
  font-family: var(--mono);
  font-size: 12px;
  border-top: 1px solid var(--tool-border);
  overflow-x: auto;
}
.jv-row { cursor: pointer; display: flex; align-items: center; gap: 4px; user-select: none; }
.jv-row:hover .jv-summary { text-decoration: underline; }
.jv-toggle { width: 12px; display: inline-block; color: var(--text-dim); font-size: 9px; flex-shrink: 0; }
.jv-summary { color: var(--text-dim); font-style: italic; }
.jv-entry { margin: 1px 0; }
.jv-key { color: var(--jv-key); }
.jv-val { white-space: pre-wrap; word-break: break-word; }
.jv-val.jv-string { color: var(--jv-string); }
.jv-val.jv-number { color: var(--jv-number); }
.jv-val.jv-boolean { color: var(--jv-boolean); }
.jv-val.jv-null { color: var(--text-dim); font-style: italic; }
.jv-children { border-left: 1px solid var(--tool-border); padding-left: 8px; margin-left: 3px; }
.collapsible-body.collapsed {
  max-height: 220px;
  overflow: hidden;
  position: relative;
}
.collapsible-body.collapsed::after {
  content: '';
  position: absolute;
  left: 0; right: 0; bottom: 0; height: 40px;
  background: linear-gradient(transparent, var(--tool-bg));
}
.expand-link {
  display: block;
  padding: 0 12px 8px;
  font-size: 11.5px;
  color: var(--accent);
  cursor: pointer;
  text-decoration: underline;
}
mark.hit { background: var(--mark); color: inherit; border-radius: 2px; }
mark.hit.current { background: var(--mark-current); }
#summary-panel {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 10px;
  margin-bottom: 18px;
}
#summary-panel > summary {
  cursor: pointer;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 8px;
}
#summary-panel > summary::-webkit-details-marker { display: none; }
#summary-panel > summary .chev { font-size: 9px; color: var(--text-dim); }
#summary-panel:not([open]) > summary .chev { transform: rotate(-90deg); display: inline-block; }
#summary-headline { font-weight: 400; color: var(--text-dim); font-family: var(--mono); font-size: 11.5px; }
#summary-body { padding: 0 14px 14px; }
#stat-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
.tile {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--bg);
}
.tile .tile-value { font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .tile-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin-top: 2px;
}
.tile.warn .tile-value { color: var(--accent); }
details.summary-section {
  border-top: 1px solid var(--border);
  padding: 7px 0 0;
  margin-top: 7px;
}
details.summary-section > summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--text-dim);
  list-style: none;
  display: flex;
  gap: 6px;
  align-items: center;
}
details.summary-section > summary::-webkit-details-marker { display: none; }
details.summary-section > summary:hover { color: var(--text); }
details.summary-section .chev { font-size: 8px; }
details.summary-section:not([open]) .chev { transform: rotate(-90deg); display: inline-block; }
.summary-list { margin: 7px 0 3px; padding: 0; list-style: none; max-height: 260px; overflow-y: auto; }
.summary-list li {
  font-family: var(--mono);
  font-size: 11.5px;
  padding: 3px 6px;
  border-radius: 4px;
  display: flex;
  gap: 8px;
  align-items: baseline;
  cursor: pointer;
}
.summary-list li:hover { background: var(--tool-bg); }
.summary-list .count { color: var(--text-dim); flex-shrink: 0; font-size: 10.5px; }
.summary-list .grow {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.summary-list li.is-error .grow { color: var(--accent); }
.token-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
}
/* Fixed-width chips rather than stretching grid cells: when cells stretch,
   a label sits far from its own value and reads as if paired with the next. */
.token-grid div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  min-width: 190px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
}
.token-grid .k { color: var(--text-dim); }
.cost-note { font-size: 11px; color: var(--text-dim); margin-top: 8px; font-style: italic; }
#meta-panel {
  margin-top: 24px;
  border-top: 1px solid var(--border);
  padding-top: 14px;
}
#meta-panel h3 { font-size: 13px; color: var(--text-dim); margin-bottom: 8px; }
#meta-panel ol { margin: 0; padding-left: 20px; font-family: var(--mono); font-size: 11.5px; color: var(--text-dim); }
#meta-panel li { margin-bottom: 3px; }
.hidden, .fhidden { display: none !important; }
</style>
</head>
<body>
<div id="layout">
  <div id="sidebar">
    <div id="sidebar-header">
      <div class="brand">snoop</div>__BACK_LINK__
      <div class="session-title">__TITLE_ESC__</div>
      <div class="meta-line">__SESSION_ID__</div>
      <div class="meta-line">__PROJECT_PATH__</div>
    </div>
    <details id="filter-details" open>
      <summary><span class="chev">&#9660;</span> Filter <span id="filter-summary"></span></summary>
      <div id="filter-body">
        <div class="filter-actions">
          <button id="filter-all" type="button">All</button>
          <button id="filter-none" type="button">None</button>
        </div>
        <div id="filter-list"></div>
      </div>
    </details>
    <ul id="nav-list"></ul>
  </div>
  <div id="resizer" title="Drag to resize · double-click to reset"></div>
  <div id="main">
    <div id="toolbar">
      <input id="search-input" type="text" placeholder="Search transcript... ( / to focus )" />
      <span id="search-count"></span>
      <button class="nav-btn" id="prev-btn" title="Previous match (Shift+Enter)">&uarr;</button>
      <button class="nav-btn" id="next-btn" title="Next match (Enter)">&darr;</button>
      <button class="toggle-btn" id="meta-toggle">Show session events</button>
    </div>
    <details id="summary-panel" open>
      <summary><span class="chev">&#9660;</span> What happened <span id="summary-headline"></span></summary>
      <div id="summary-body">
        <div id="stat-tiles"></div>
        <div id="summary-sections"></div>
      </div>
    </details>
    <div id="turns"></div>
    <div id="meta-panel" class="hidden">
      <h3>Session events (mode changes, tool availability, snapshots, turn timings...)</h3>
      <ol id="meta-list"></ol>
    </div>
  </div>
</div>

<script id="snoop-data" type="application/json">__DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('snoop-data').textContent);
const turnsEl = document.getElementById('turns');
const navListEl = document.getElementById('nav-list');
const metaListEl = document.getElementById('meta-list');

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function fmtTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleString();
  } catch (e) { return ts; }
}

function fmtClock(ts) {
  if (!ts) return '--:--:--';
  try {
    return new Date(ts).toLocaleTimeString([], { hour12: false });
  } catch (e) { return ''; }
}

function roleIcon(role) {
  return role === 'user' ? '\u{1F464}' : '\u{1F916}';
}

function toolIcon() {
  return '\u{1F527}';
}

function categoryLabel(cat) {
  if (cat === 'user_prompt') return 'User';
  if (cat === 'agent_response') return 'Assistant';
  if (cat === 'thinking') return 'Thinking';
  if (cat === 'meta_prompt') return 'Meta / slash-command noise';
  if (cat.indexOf('tool:') === 0) return cat.slice(5);
  return cat;
}

function badgeClass(cat) {
  if (cat === 'user_prompt') return 'badge-user';
  if (cat === 'agent_response') return 'badge-assistant';
  if (cat === 'thinking') return 'badge-thinking';
  if (cat === 'meta_prompt') return 'badge-meta';
  if (cat.indexOf('tool:') === 0) return 'badge-tool';
  return '';
}

// Registry of searchable text nodes: {el, text}
const searchIndex = [];

function makeTextBlock(text) {
  const div = document.createElement('div');
  div.className = 'block-text searchable';
  div.textContent = text;
  searchIndex.push({ el: div, text });
  return div;
}

function makeThinkingBlock(text) {
  const details = document.createElement('details');
  details.className = 'thinking';
  const summary = document.createElement('summary');
  summary.textContent = 'Thinking';
  details.appendChild(summary);
  const div = document.createElement('div');
  div.className = 'block-text searchable';
  div.textContent = text;
  searchIndex.push({ el: div, text });
  details.appendChild(div);
  return details;
}

// ---- Minimal JSON tree viewer: collapsed nested nodes by default, click to expand ----

function tryParseJSON(text) {
  try {
    const v = JSON.parse(text);
    if (v !== null && typeof v === 'object') return v;
  } catch (e) { /* not JSON */ }
  return undefined;
}

function jvSummary(v) {
  if (Array.isArray(v)) return 'Array(' + v.length + ')';
  return 'Object(' + Object.keys(v).length + ')';
}

function buildJSONTree(value, depth) {
  const isContainer = value !== null && typeof value === 'object';
  const wrap = document.createElement(isContainer ? 'div' : 'span');
  wrap.className = 'jv-node';

  if (isContainer) {
    const isArray = Array.isArray(value);
    const entries = isArray ? value.map((v, i) => [i, v]) : Object.entries(value);

    const row = document.createElement('div');
    row.className = 'jv-row';
    const toggle = document.createElement('span');
    toggle.className = 'jv-toggle';
    const startOpen = depth < 1;
    toggle.textContent = startOpen ? '▾' : '▸';
    row.appendChild(toggle);
    const summary = document.createElement('span');
    summary.className = 'jv-summary';
    summary.textContent = jvSummary(value);
    row.appendChild(summary);
    wrap.appendChild(row);

    const children = document.createElement('div');
    children.className = 'jv-children';
    children.style.display = startOpen ? '' : 'none';
    entries.forEach(([k, v]) => {
      const entryRow = document.createElement('div');
      entryRow.className = 'jv-entry';
      if (!isArray) {
        const keyText = k + ': ';
        const keyEl = document.createElement('span');
        keyEl.className = 'jv-key';
        keyEl.textContent = keyText;
        searchIndex.push({ el: keyEl, text: keyText });
        entryRow.appendChild(keyEl);
      }
      entryRow.appendChild(buildJSONTree(v, depth + 1));
      children.appendChild(entryRow);
    });
    wrap.appendChild(children);

    row.addEventListener('click', () => {
      const isOpen = children.style.display !== 'none';
      children.style.display = isOpen ? 'none' : '';
      toggle.textContent = isOpen ? '▸' : '▾';
    });
  } else {
    const val = document.createElement('span');
    const type = value === null ? 'null' : typeof value;
    val.className = 'jv-val jv-' + type;
    const text = value === null ? 'null' : (type === 'string' ? JSON.stringify(value) : String(value));
    val.textContent = text;
    searchIndex.push({ el: val, text });
    wrap.appendChild(val);
  }
  return wrap;
}

function makeCollapsibleText(text) {
  const wrap = document.createElement('div');
  const pre = document.createElement('pre');
  pre.className = 'searchable collapsible-body';
  pre.textContent = text;
  searchIndex.push({ el: pre, text });

  const lineCount = text.split('\n').length;
  const shouldCollapse = lineCount > 14 || text.length > 1200;
  if (shouldCollapse) pre.classList.add('collapsed');
  wrap.appendChild(pre);

  if (shouldCollapse) {
    const link = document.createElement('span');
    link.className = 'expand-link';
    link.textContent = 'Show more';
    link.addEventListener('click', () => {
      const collapsed = pre.classList.toggle('collapsed');
      link.textContent = collapsed ? 'Show more' : 'Show less';
    });
    wrap.appendChild(link);
  }
  return wrap;
}

function makeToolBlock(block) {
  const card = document.createElement('div');
  card.className = 'tool-card';

  const header = document.createElement('div');
  header.className = 'tool-card-header';
  header.innerHTML = '<span class="tool-icon">' + toolIcon() + '</span> ' + escapeHtml(block.name || 'tool');
  card.appendChild(header);

  const inputWrap = document.createElement('div');
  inputWrap.className = 'jv-wrap';
  if (block.input && typeof block.input === 'object') {
    inputWrap.appendChild(buildJSONTree(block.input, 0));
  } else {
    const t = JSON.stringify(block.input);
    const pre = document.createElement('pre');
    pre.className = 'searchable';
    pre.textContent = t;
    searchIndex.push({ el: pre, text: t });
    inputWrap.appendChild(pre);
  }
  card.appendChild(inputWrap);

  if (block.result) {
    const label = document.createElement('div');
    label.className = 'result-label';
    label.textContent = block.result.isError ? 'result (error)' : 'result';
    card.appendChild(label);

    const wrap = document.createElement('div');
    wrap.className = 'result-wrap' + (block.result.isError ? ' error' : '');
    const parsed = tryParseJSON(block.result.content || '');
    if (parsed !== undefined) {
      const jvWrap = document.createElement('div');
      jvWrap.className = 'jv-wrap';
      jvWrap.appendChild(buildJSONTree(parsed, 0));
      wrap.appendChild(jvWrap);
    } else {
      wrap.appendChild(makeCollapsibleText(block.result.content || ''));
    }
    card.appendChild(wrap);
  } else {
    const noResult = document.createElement('div');
    noResult.className = 'no-result';
    noResult.textContent = '(no result recorded)';
    card.appendChild(noResult);
  }

  return card;
}

function renderTurn(turn, index) {
  const el = document.createElement('div');
  el.className = 'turn ' + turn.role + (turn.isMeta ? ' meta-msg' : '');
  el.id = 'turn-' + index;

  const header = document.createElement('div');
  header.className = 'turn-header';
  const left = document.createElement('span');
  left.className = 'role-label';
  left.textContent = roleIcon(turn.role) + ' ' + (turn.role === 'user' ? 'User' : 'Assistant') + (turn.isMeta ? ' (meta)' : '');
  header.appendChild(left);
  const right = document.createElement('span');
  let metaBits = [fmtTime(turn.timestamp)];
  if (turn.model) metaBits.push(turn.model);
  if (turn.usage && turn.usage.output_tokens) metaBits.push(turn.usage.output_tokens + ' out tok');
  right.textContent = metaBits.filter(Boolean).join(' · ');
  header.appendChild(right);
  el.appendChild(header);

  turn.blocks.forEach(b => {
    let child = null;
    if (b.kind === 'text') child = makeTextBlock(b.text);
    else if (b.kind === 'thinking') child = makeThinkingBlock(b.text);
    else if (b.kind === 'tool_call') child = makeToolBlock(b);
    if (child) {
      child.id = 'block-' + b.id;
      child.classList.add('block');
      child.dataset.filterKey = b.category;
      el.appendChild(child);
    }
  });

  return el;
}

DATA.turns.forEach((turn, i) => turnsEl.appendChild(renderTurn(turn, i)));

DATA.meta.forEach(m => {
  const li = document.createElement('li');
  li.textContent = (m.timestamp ? fmtTime(m.timestamp) + ' — ' : '') + '[' + m.type + '] ' + m.detail;
  metaListEl.appendChild(li);
});

// ---- Session summary ----

function fmtDur(ms) {
  if (!ms || ms < 0) return '—';
  const m = Math.round(ms / 60000);
  if (m < 60) return m + 'm';
  return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}
function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(n);
}
function fmtCost(usd) {
  if (usd >= 1) return '$' + usd.toFixed(2);
  if (usd > 0) return '$' + usd.toFixed(3);
  return '$0';
}

function jumpTo(blockId) {
  const el = document.getElementById('block-' + blockId);
  if (!el) return;
  if (el.classList.contains('fhidden')) {
    // The block is filtered out; re-enable its category so the jump lands.
    activeCategories.add(el.dataset.filterKey);
    syncFilterCheckboxes();
    applyFilters();
  }
  el.scrollIntoView({ block: 'center' });
}

function statTile(value, label, opts) {
  const d = document.createElement('div');
  d.className = 'tile' + ((opts && opts.warn) ? ' warn' : '');
  const v = document.createElement('div');
  v.className = 'tile-value';
  v.textContent = value;
  const l = document.createElement('div');
  l.className = 'tile-label';
  l.textContent = label;
  d.appendChild(v);
  d.appendChild(l);
  if (opts && opts.title) d.title = opts.title;
  return d;
}

function summarySection(label, count, rows) {
  if (!rows.length) return null;
  const det = document.createElement('details');
  det.className = 'summary-section';
  const sum = document.createElement('summary');
  sum.innerHTML = '<span class="chev">&#9660;</span> ' + escapeHtml(label) + ' (' + count + ')';
  det.appendChild(sum);
  const ul = document.createElement('ul');
  ul.className = 'summary-list';
  rows.forEach(r => ul.appendChild(r));
  det.appendChild(ul);
  return det;
}

function summaryRow(leftText, mainText, blockId, opts) {
  const li = document.createElement('li');
  if (opts && opts.isError) li.className = 'is-error';
  if (leftText) {
    const c = document.createElement('span');
    c.className = 'count';
    c.textContent = leftText;
    li.appendChild(c);
  }
  const g = document.createElement('span');
  g.className = 'grow';
  g.textContent = mainText;
  g.title = (opts && opts.title) || mainText;
  li.appendChild(g);
  if (blockId) li.addEventListener('click', () => jumpTo(blockId));
  return li;
}

function renderSummary(s) {
  const tiles = document.getElementById('stat-tiles');
  const totalTokens = s.tokens.input + s.tokens.output + s.tokens.cacheRead + s.tokens.cacheWrite;

  tiles.appendChild(statTile(fmtDur(s.activeMs), 'active time',
    { title: 'Wall-clock span: ' + fmtDur(s.durationMs) }));
  tiles.appendChild(statTile(s.nPrompts, 'prompts'));
  tiles.appendChild(statTile(s.nToolCalls, 'tool calls'));
  tiles.appendChild(statTile(s.files.length, 'files touched'));
  tiles.appendChild(statTile(fmtTokens(totalTokens), 'tokens',
    { title: fmtTokens(s.tokens.output) + ' generated, the rest read back as context' }));
  tiles.appendChild(statTile(fmtCost(s.costUsd), 'est. cost',
    { title: 'Estimated at public list prices' }));
  if (s.errors.length) tiles.appendChild(statTile(s.errors.length, 'tool errors', { warn: true }));

  document.getElementById('summary-headline').textContent =
    fmtDur(s.activeMs) + ' · ' + s.nToolCalls + ' tool calls · ' + fmtCost(s.costUsd);

  const sections = document.getElementById('summary-sections');

  const fileRows = s.files.map(f =>
    summaryRow(f.count > 1 ? '×' + f.count : '', f.path, f.id, { title: f.path }));
  const filesSection = summarySection('Files touched', s.files.length, fileRows);
  if (filesSection) sections.appendChild(filesSection);

  const cmdRows = s.commands.map(c =>
    summaryRow('', c.cmd, c.id, { title: c.description || c.cmd, isError: c.isError }));
  const cmdSection = summarySection('Commands run', s.commands.length, cmdRows);
  if (cmdSection) sections.appendChild(cmdSection);

  const errRows = s.errors.map(e =>
    summaryRow(e.tool, e.preview.replace(/\s+/g, ' '), e.id, { isError: true }));
  const errSection = summarySection('Tool errors', s.errors.length, errRows);
  if (errSection) sections.appendChild(errSection);

  // Token + model breakdown
  const det = document.createElement('details');
  det.className = 'summary-section';
  const sum = document.createElement('summary');
  sum.innerHTML = '<span class="chev">&#9660;</span> Tokens &amp; cost';
  det.appendChild(sum);

  const grid = document.createElement('div');
  grid.className = 'token-grid';
  [['input', s.tokens.input], ['output', s.tokens.output],
   ['cache read', s.tokens.cacheRead], ['cache write', s.tokens.cacheWrite]].forEach(([k, v]) => {
    const row = document.createElement('div');
    row.innerHTML = '<span class="k">' + k + '</span><span>' + v.toLocaleString() + '</span>';
    grid.appendChild(row);
  });
  s.models.forEach(([name, n]) => {
    const row = document.createElement('div');
    row.innerHTML = '<span class="k">' + escapeHtml(name) + '</span><span>' + n + ' msg</span>';
    grid.appendChild(row);
  });
  det.appendChild(grid);

  const note = document.createElement('div');
  note.className = 'cost-note';
  let noteText = 'Estimated at public list prices — a negotiated rate makes the real figure lower.';
  if (s.unpricedModels.length) {
    noteText += ' Excludes ' + s.unpricedModels.join(', ') + ' (not billed).';
  }
  note.textContent = noteText;
  det.appendChild(note);
  sections.appendChild(det);
}

renderSummary(DATA.summary);

// ---- Sidebar nav list ----

function renderNavItem(item) {
  const li = document.createElement('li');
  li.className = 'nav-item';
  li.dataset.filterKey = item.category;
  li.dataset.blockId = item.id;

  const time = document.createElement('span');
  time.className = 'nav-time';
  time.textContent = fmtClock(item.timestamp);

  const badge = document.createElement('span');
  badge.className = 'nav-badge ' + badgeClass(item.category);
  badge.textContent = categoryLabel(item.category);
  badge.title = categoryLabel(item.category);

  const preview = document.createElement('span');
  preview.className = 'nav-preview';
  preview.textContent = item.preview || '(empty)';

  li.appendChild(time);
  li.appendChild(badge);
  li.appendChild(preview);
  li.addEventListener('click', () => {
    const target = document.getElementById('block-' + item.id);
    if (target) target.scrollIntoView({ block: 'center' });
  });
  return li;
}

const navByBlockId = {};
DATA.nav.forEach(item => {
  const li = renderNavItem(item);
  navByBlockId[item.id] = li;
  navListEl.appendChild(li);
});

// ---- Filters ----

const FIXED_ORDER = ['user_prompt', 'agent_response', 'thinking', 'meta_prompt'];

function sortedCategories() {
  const cats = Object.keys(DATA.categoryCounts);
  const fixed = FIXED_ORDER.filter(c => cats.indexOf(c) !== -1);
  const tools = cats.filter(c => c.indexOf('tool:') === 0).sort();
  const rest = cats.filter(c => FIXED_ORDER.indexOf(c) === -1 && c.indexOf('tool:') !== 0).sort();
  return fixed.concat(tools, rest);
}

const activeCategories = new Set();
sortedCategories().forEach(c => { if (c !== 'meta_prompt') activeCategories.add(c); });

function buildFilterPanel() {
  const list = document.getElementById('filter-list');
  sortedCategories().forEach(cat => {
    const row = document.createElement('label');
    row.className = 'filter-row';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = activeCategories.has(cat);
    cb.addEventListener('change', () => {
      if (cb.checked) activeCategories.add(cat); else activeCategories.delete(cat);
      applyFilters();
    });

    const badge = document.createElement('span');
    badge.className = 'nav-badge ' + badgeClass(cat);
    badge.textContent = categoryLabel(cat);
    badge.title = categoryLabel(cat);

    const count = document.createElement('span');
    count.className = 'filter-count';
    count.textContent = DATA.categoryCounts[cat];

    row.appendChild(cb);
    row.appendChild(badge);
    row.appendChild(count);
    list.appendChild(row);
  });
}

function syncFilterCheckboxes() {
  const cats = sortedCategories();
  Array.from(document.querySelectorAll('#filter-list input[type=checkbox]')).forEach((cb, i) => {
    cb.checked = activeCategories.has(cats[i]);
  });
}

function applyFilters() {
  document.querySelectorAll('[data-filter-key]').forEach(el => {
    const show = activeCategories.has(el.dataset.filterKey);
    el.classList.toggle('fhidden', !show);
  });
  document.querySelectorAll('.turn').forEach(t => {
    const anyVisible = Array.from(t.children).some(c => c.classList.contains('block') && !c.classList.contains('fhidden'));
    t.classList.toggle('fhidden', !anyVisible);
  });
  updateFilterSummary();
}

function updateFilterSummary() {
  const total = document.querySelectorAll('#nav-list .nav-item').length;
  const visible = document.querySelectorAll('#nav-list .nav-item:not(.fhidden)').length;
  document.getElementById('filter-summary').textContent = '(' + visible + ' / ' + total + ')';
}

buildFilterPanel();
applyFilters();

document.getElementById('filter-all').addEventListener('click', () => {
  sortedCategories().forEach(c => activeCategories.add(c));
  syncFilterCheckboxes();
  applyFilters();
});
document.getElementById('filter-none').addEventListener('click', () => {
  activeCategories.clear();
  syncFilterCheckboxes();
  applyFilters();
});

// ---- Sidebar resize ----

const layoutEl = document.getElementById('layout');
const resizerEl = document.getElementById('resizer');
const DEFAULT_SIDEBAR_WIDTH = 320;
let resizing = false;

function setSidebarWidth(px) {
  const min = 220;
  const max = Math.min(900, window.innerWidth - 300);
  const clamped = Math.max(min, Math.min(max, px));
  layoutEl.style.gridTemplateColumns = clamped + 'px 6px 1fr';
}

resizerEl.addEventListener('mousedown', () => {
  resizing = true;
  resizerEl.classList.add('dragging');
  document.body.style.userSelect = 'none';
  document.body.style.cursor = 'col-resize';
});
document.addEventListener('mousemove', (e) => {
  if (!resizing) return;
  setSidebarWidth(e.clientX);
});
document.addEventListener('mouseup', () => {
  if (!resizing) return;
  resizing = false;
  resizerEl.classList.remove('dragging');
  document.body.style.userSelect = '';
  document.body.style.cursor = '';
});
resizerEl.addEventListener('dblclick', () => setSidebarWidth(DEFAULT_SIDEBAR_WIDTH));

// Highlight active sidebar entry on scroll
const blockEls = Array.from(document.querySelectorAll('.block[id]'));
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    const bid = entry.target.id.replace('block-', '');
    const li = navByBlockId[bid];
    if (!li) return;
    if (entry.isIntersecting) {
      Object.values(navByBlockId).forEach(x => x.classList.remove('active'));
      li.classList.add('active');
      li.scrollIntoView({ block: 'nearest' });
    }
  });
}, { rootMargin: '-10% 0px -70% 0px' });
blockEls.forEach(el => observer.observe(el));

// Session events panel toggle (scrolls to it, since it lives at the bottom of a long page)
document.getElementById('meta-toggle').addEventListener('click', (e) => {
  const panel = document.getElementById('meta-panel');
  const wasHidden = panel.classList.contains('hidden');
  panel.classList.toggle('hidden');
  const nowVisible = wasHidden;
  e.target.textContent = nowVisible ? 'Hide session events' : 'Show session events';
  e.target.classList.toggle('active', nowVisible);
  if (nowVisible) panel.scrollIntoView({ block: 'start' });
});

// ---- Search ----

const searchInput = document.getElementById('search-input');
const searchCount = document.getElementById('search-count');
const prevBtn = document.getElementById('prev-btn');
const nextBtn = document.getElementById('next-btn');
let currentMatches = [];
let currentMatchIdx = -1;

function clearHighlights() {
  searchIndex.forEach(entry => {
    entry.el.textContent = entry.text;
  });
}

function runSearch(query) {
  clearHighlights();
  currentMatches = [];
  currentMatchIdx = -1;

  if (!query) {
    searchCount.textContent = '';
    return;
  }

  const q = query.toLowerCase();
  searchIndex.forEach(entry => {
    if (entry.el.closest('.fhidden')) return; // skip content hidden by filters
    const lower = entry.text.toLowerCase();
    if (!lower.includes(q)) return;

    // Rebuild the element's text with matches wrapped in <mark>.
    let html = '';
    let pos = 0;
    let searchPos = 0;
    while (true) {
      const found = lower.indexOf(q, searchPos);
      if (found === -1) break;
      html += escapeHtml(entry.text.slice(pos, found));
      const matchText = entry.text.slice(found, found + q.length);
      const markId = 'hit-' + currentMatches.length;
      html += '<mark class="hit" id="' + markId + '">' + escapeHtml(matchText) + '</mark>';
      currentMatches.push(markId);
      pos = found + q.length;
      searchPos = pos;
    }
    html += escapeHtml(entry.text.slice(pos));
    entry.el.innerHTML = html;
  });

  if (currentMatches.length > 0) {
    currentMatchIdx = 0;
    focusMatch();
  } else {
    searchCount.textContent = '0 matches';
  }
}

function revealAncestors(el) {
  // Expand collapsed long-text blocks.
  let node = el;
  while (node) {
    if (node.classList && node.classList.contains('collapsible-body') && node.classList.contains('collapsed')) {
      node.classList.remove('collapsed');
    }
    node = node.parentElement;
  }
  // Expand collapsed JSON tree ancestors.
  node = el.parentElement;
  while (node) {
    if (node.classList && node.classList.contains('jv-children') && node.style.display === 'none') {
      node.style.display = '';
      const row = node.previousElementSibling;
      const toggle = row && row.querySelector ? row.querySelector('.jv-toggle') : null;
      if (toggle) toggle.textContent = '▾';
    }
    node = node.parentElement;
  }
  // Open ancestor <details class="thinking"> if closed.
  let d = el.closest ? el.closest('details') : null;
  while (d) {
    d.open = true;
    d = d.parentElement ? d.parentElement.closest('details') : null;
  }
}

function focusMatch() {
  document.querySelectorAll('mark.hit.current').forEach(m => m.classList.remove('current'));
  if (currentMatchIdx < 0 || currentMatchIdx >= currentMatches.length) return;
  const el = document.getElementById(currentMatches[currentMatchIdx]);
  if (!el) return;
  el.classList.add('current');
  revealAncestors(el);
  el.scrollIntoView({ block: 'center' });
  searchCount.textContent = (currentMatchIdx + 1) + ' / ' + currentMatches.length;
}

let searchDebounce = null;
searchInput.addEventListener('input', (e) => {
  clearTimeout(searchDebounce);
  const value = e.target.value.trim();
  searchDebounce = setTimeout(() => runSearch(value), 150);
});
searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (currentMatches.length === 0) return;
    if (e.shiftKey) {
      currentMatchIdx = (currentMatchIdx - 1 + currentMatches.length) % currentMatches.length;
    } else {
      currentMatchIdx = (currentMatchIdx + 1) % currentMatches.length;
    }
    focusMatch();
  }
});
prevBtn.addEventListener('click', () => {
  if (currentMatches.length === 0) return;
  currentMatchIdx = (currentMatchIdx - 1 + currentMatches.length) % currentMatches.length;
  focusMatch();
});
nextBtn.addEventListener('click', () => {
  if (currentMatches.length === 0) return;
  currentMatchIdx = (currentMatchIdx + 1) % currentMatches.length;
  focusMatch();
});

document.addEventListener('keydown', (e) => {
  if (e.key === '/' && document.activeElement !== searchInput) {
    e.preventDefault();
    searchInput.focus();
  }
});
</script>
</body>
</html>
"""


def render_html(session_id, project_path, title, turns, meta_events, nav, category_counts,
                tool_names, summary, back_link=None):
    data = json.dumps({
        "turns": turns,
        "meta": meta_events,
        "nav": nav,
        "categoryCounts": category_counts,
        "toolNames": tool_names,
        "summary": summary,
    })
    html = HTML_TEMPLATE
    html = html.replace("__THEME_CSS__", THEME_CSS.strip())
    html = html.replace("__TITLE__", title.replace("</", "<\\/"))
    html = html.replace("__TITLE_ESC__", title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html = html.replace("__SESSION_ID__", session_id)
    html = html.replace("__PROJECT_PATH__", project_path)
    html = html.replace(
        "__BACK_LINK__",
        f'\n      <a class="back-link" href="{back_link}">&larr; all sessions</a>' if back_link else "",
    )
    html = html.replace("__DATA_JSON__", data.replace("</script>", "<\\/script>"))
    return html


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Cross-session index page
# ---------------------------------------------------------------------------

INDEX_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>snoop - all sessions</title>
<style>
__THEME_CSS__
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.5;
}
#wrap { max-width: 1100px; margin: 0 auto; padding: 28px 24px 80px; }
header .brand { font-weight: 700; font-size: 20px; color: var(--accent); }
header .sub { color: var(--text-dim); font-size: 12.5px; margin-top: 4px; font-family: var(--mono); }
#controls {
  position: sticky;
  top: 0;
  background: var(--bg);
  padding: 16px 0 12px;
  margin: 18px 0 4px;
  border-bottom: 1px solid var(--border);
  z-index: 5;
}
#search {
  width: 100%;
  padding: 11px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font-size: 15px;
}
#search:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.row2 { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
#result-count { font-size: 12px; color: var(--text-dim); margin-right: auto; }
select {
  padding: 5px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font-size: 12px;
}
.chip {
  font-size: 11.5px;
  padding: 3px 9px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text-dim);
  cursor: pointer;
  white-space: nowrap;
}
.chip:hover { border-color: var(--accent); }
.chip.on { background: var(--accent-soft); border-color: var(--accent); color: var(--text); }
#projects { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.card {
  display: block;
  text-decoration: none;
  color: inherit;
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 10px;
  padding: 14px 16px;
  margin-top: 12px;
}
.card:hover { border-color: var(--accent); }
.card-title { font-weight: 600; font-size: 15px; margin-bottom: 3px; }
.card-project { font-family: var(--mono); font-size: 11.5px; color: var(--text-dim); }
.card-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-dim);
  margin-top: 8px;
  font-family: var(--mono);
}
.card-tools { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 8px; }
.tool-chip {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--badge-tool-bg);
  color: var(--badge-tool-fg);
}
.snippets { margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 8px; }
.snippet {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 5px;
  padding-left: 10px;
  border-left: 2px solid var(--tool-border);
  white-space: pre-wrap;
  word-break: break-word;
}
.snippet .where { color: var(--accent); font-family: var(--mono); font-size: 10.5px; }
mark { background: var(--mark); color: inherit; border-radius: 2px; }
#empty { text-align: center; color: var(--text-dim); padding: 50px 0; font-size: 13px; }
.hidden { display: none !important; }
kbd {
  font-family: var(--mono); font-size: 10.5px; border: 1px solid var(--border);
  border-bottom-width: 2px; border-radius: 4px; padding: 0 4px; color: var(--text-dim);
}
</style>
</head>
<body>
<div id="wrap">
  <header>
    <div class="brand">snoop</div>
    <div class="sub" id="header-sub"></div>
  </header>

  <div id="controls">
    <input id="search" type="text" placeholder="Search every prompt across all sessions...  ( / to focus )" autofocus />
    <div class="row2">
      <span id="result-count"></span>
      <label style="font-size:12px;color:var(--text-dim)">sort</label>
      <select id="sort">
        <option value="recent">Most recent</option>
        <option value="oldest">Oldest</option>
        <option value="longest">Longest duration</option>
        <option value="busiest">Most tool calls</option>
        <option value="relevance">Best match</option>
      </select>
    </div>
    <div id="projects"></div>
  </div>

  <div id="results"></div>
  <div id="empty" class="hidden">No sessions match.</div>
</div>

<script id="snoop-index-data" type="application/json">__DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('snoop-index-data').textContent);
const SESSIONS = DATA.sessions;
const DEEP = DATA.deep;

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function fmtDate(ts) {
  if (!ts) return '—';
  try { return new Date(ts).toLocaleDateString([], {year:'numeric', month:'short', day:'numeric'}); }
  catch (e) { return ts; }
}
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const m = Math.round(ms / 60000);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}
function shortProject(p) {
  const parts = p.split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : p;
}

// ---- project filter ----
const allProjects = Array.from(new Set(SESSIONS.map(s => s.project))).sort();
const activeProjects = new Set(allProjects);

const projectsEl = document.getElementById('projects');
allProjects.forEach(p => {
  const chip = document.createElement('span');
  chip.className = 'chip on';
  chip.textContent = shortProject(p);
  chip.title = p;
  chip.addEventListener('click', () => {
    if (activeProjects.has(p)) { activeProjects.delete(p); chip.classList.remove('on'); }
    else { activeProjects.add(p); chip.classList.add('on'); }
    render();
  });
  projectsEl.appendChild(chip);
});

document.getElementById('header-sub').textContent =
  SESSIONS.length + ' sessions · ' + allProjects.length + ' projects · ' +
  fmtDate(SESSIONS[SESSIONS.length - 1] && SESSIONS[SESSIONS.length - 1].start) +
  ' – ' + fmtDate(SESSIONS[0] && SESSIONS[0].start) +
  (DEEP ? ' · deep index' : '');

// ---- search ----
function findMatches(session, q) {
  // Returns {count, snippets:[{where,text}]} for a lowercase query.
  const out = { count: 0, snippets: [] };
  const consider = [];
  session.prompts.forEach((p, i) => consider.push(['prompt ' + (i + 1), p]));
  if (session.title) consider.push(['title', session.title]);
  if (DEEP && session.body) consider.push(['transcript', session.body]);

  consider.forEach(([where, text]) => {
    if (!text) return;
    const lower = text.toLowerCase();
    let from = 0;
    while (true) {
      const at = lower.indexOf(q, from);
      if (at === -1) break;
      out.count++;
      if (out.snippets.length < 3) {
        const s = Math.max(0, at - 60);
        const e = Math.min(text.length, at + q.length + 90);
        out.snippets.push({
          where: where,
          pre: (s > 0 ? '…' : '') + text.slice(s, at),
          hit: text.slice(at, at + q.length),
          post: text.slice(at + q.length, e) + (e < text.length ? '…' : ''),
        });
      }
      from = at + q.length;
      if (out.count > 500) break;
    }
  });
  return out;
}

function card(session, matches) {
  const a = document.createElement('a');
  a.className = 'card';
  a.href = 'sessions/' + session.guid + '.html';

  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = session.title;
  a.appendChild(title);

  const proj = document.createElement('div');
  proj.className = 'card-project';
  proj.textContent = session.project;
  a.appendChild(proj);

  const meta = document.createElement('div');
  meta.className = 'card-meta';
  const bits = [
    [fmtDate(session.start), null],
    // Active time, not wall-clock span: a session resumed the next day spans
    // 24h but may hold minutes of work. Span is available on hover.
    [fmtDuration(session.activeMs) + ' active',
     'spans ' + fmtDuration(session.durationMs) + ' wall-clock'],
    [session.nPrompts + ' prompts', null],
    [session.nToolCalls + ' tool calls', null],
  ];
  if (session.filesTouched.length) {
    bits.push([session.filesTouched.length + ' files touched',
               session.filesTouched.join('\n')]);
  }
  if (session.nErrors) bits.push([session.nErrors + ' errors', null]);
  bits.forEach(([text, tip]) => {
    const s = document.createElement('span');
    s.textContent = text;
    if (tip) s.title = tip;
    meta.appendChild(s);
  });
  a.appendChild(meta);

  if (session.topTools.length) {
    const tools = document.createElement('div');
    tools.className = 'card-tools';
    session.topTools.forEach(([name, n]) => {
      const c = document.createElement('span');
      c.className = 'tool-chip';
      c.textContent = name + ' ×' + n;
      c.title = name;
      tools.appendChild(c);
    });
    a.appendChild(tools);
  }

  if (matches && matches.snippets.length) {
    const wrap = document.createElement('div');
    wrap.className = 'snippets';
    matches.snippets.forEach(sn => {
      const d = document.createElement('div');
      d.className = 'snippet';
      d.innerHTML = '<span class="where">' + escapeHtml(sn.where) + '</span>  ' +
        escapeHtml(sn.pre) + '<mark>' + escapeHtml(sn.hit) + '</mark>' + escapeHtml(sn.post);
      wrap.appendChild(d);
    });
    if (matches.count > matches.snippets.length) {
      const more = document.createElement('div');
      more.className = 'snippet';
      more.style.borderLeftColor = 'transparent';
      more.textContent = '+' + (matches.count - matches.snippets.length) + ' more matches in this session';
      wrap.appendChild(more);
    }
    a.appendChild(wrap);
  }
  return a;
}

const searchEl = document.getElementById('search');
const sortEl = document.getElementById('sort');
const resultsEl = document.getElementById('results');
const countEl = document.getElementById('result-count');

function render() {
  const q = searchEl.value.trim().toLowerCase();
  let rows = SESSIONS
    .filter(s => activeProjects.has(s.project))
    .map(s => ({ s: s, m: q ? findMatches(s, q) : null }))
    .filter(r => !q || r.m.count > 0);

  const mode = sortEl.value;
  const cmp = {
    recent:    (a, b) => (b.s.start || '').localeCompare(a.s.start || ''),
    oldest:    (a, b) => (a.s.start || '').localeCompare(b.s.start || ''),
    longest:   (a, b) => (b.s.durationMs || 0) - (a.s.durationMs || 0),
    busiest:   (a, b) => b.s.nToolCalls - a.s.nToolCalls,
    relevance: (a, b) => ((b.m && b.m.count) || 0) - ((a.m && a.m.count) || 0),
  }[mode];
  rows.sort(cmp);

  resultsEl.textContent = '';
  rows.forEach(r => resultsEl.appendChild(card(r.s, r.m)));

  document.getElementById('empty').classList.toggle('hidden', rows.length > 0);
  if (q) {
    const total = rows.reduce((n, r) => n + r.m.count, 0);
    countEl.textContent = total + ' match' + (total === 1 ? '' : 'es') + ' in ' +
      rows.length + ' session' + (rows.length === 1 ? '' : 's');
  } else {
    countEl.textContent = rows.length + ' session' + (rows.length === 1 ? '' : 's');
  }
}

let debounce = null;
searchEl.addEventListener('input', () => {
  clearTimeout(debounce);
  const wasDefault = sortEl.value === 'recent';
  debounce = setTimeout(() => {
    // Sorting by relevance is only meaningful once there's a query.
    if (searchEl.value.trim() && wasDefault) sortEl.value = 'relevance';
    if (!searchEl.value.trim() && sortEl.value === 'relevance') sortEl.value = 'recent';
    render();
  }, 120);
});
sortEl.addEventListener('change', render);
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && document.activeElement !== searchEl) { e.preventDefault(); searchEl.focus(); }
});

render();
</script>
</body>
</html>
"""


def render_index(sessions, deep):
    public = [{k: v for k, v in s.items() if not k.startswith("_")} for s in sessions]
    payload = json.dumps({"sessions": public, "deep": deep})
    html = INDEX_TEMPLATE
    html = html.replace("__THEME_CSS__", THEME_CSS.strip())
    html = html.replace("__DATA_JSON__", payload.replace("</script>", "<\\/script>"))
    return html


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render_session_page(session_file, back_link=None):
    events = load_events(session_file)
    turns, meta_events = build_transcript(events)
    nav, category_counts, tool_names = build_nav(turns)
    return render_html(
        session_file.stem,
        project_path(events, session_file.parent.name),
        session_title(events),
        turns, meta_events, nav, category_counts, tool_names,
        build_summary(events, turns),
        back_link=back_link,
    )


def build_index(out_dir, deep=False):
    """Write index.html plus one self-contained page per session."""
    sessions = collect_sessions(deep=deep)
    if not sessions:
        print(f"No sessions found under {CLAUDE_PROJECTS_DIR}")
        sys.exit(1)

    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for s in sessions:
        page = render_session_page(Path(s["_path"]), back_link="../index.html")
        (sessions_dir / f"{s['guid']}.html").write_text(page, encoding="utf-8")

    index_path = out_dir / "index.html"
    index_path.write_text(render_index(sessions, deep), encoding="utf-8")
    return index_path, sessions


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="snoop",
        description="Explore Claude Code session transcripts in your browser.",
        epilog="With no guid, snoop builds a searchable index of every session on disk.",
    )
    parser.add_argument("guid", nargs="?",
                        help="session guid or unique prefix; omit to build the cross-session index")
    parser.add_argument("--deep", action="store_true",
                        help="index full transcript text, not just prompts (larger page, searches everything)")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="print recent sessions to the terminal instead of opening a page")
    parser.add_argument("--out", metavar="DIR",
                        help=f"output directory (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--no-open", action="store_true",
                        help="write the files but don't launch a browser")
    args = parser.parse_args()

    if args.list_only:
        list_recent_sessions()
        return

    out_dir = Path(args.out).expanduser() if args.out else DEFAULT_OUT_DIR

    if args.guid:
        session_file = find_session_file(args.guid)
        if session_file is None:
            print(f"No session found matching '{args.guid}' under {CLAUDE_PROJECTS_DIR}\n")
            list_recent_sessions()
            sys.exit(1)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{session_file.stem}.html"
        out_path.write_text(render_session_page(session_file), encoding="utf-8")
    else:
        out_path, sessions = build_index(out_dir, deep=args.deep)
        total = sum(len(json.dumps(s)) for s in sessions)
        print(f"Indexed {len(sessions)} sessions"
              f"{' (deep)' if args.deep else ''} — search payload {total / 1024:.0f} KB")

    print(f"{out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")
    if not args.no_open:
        webbrowser.open(f"file://{out_path}")


if __name__ == "__main__":
    main()
