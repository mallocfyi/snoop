# snoop

Explore a Claude Code session transcript in your browser — full conversation
order, tool calls and their results, search, and filtering. Pure client-side:
one Python script generates a self-contained HTML file and opens it. No
server, no dependencies beyond the Python 3 standard library.

## Usage

```
python3 snoop.py <guid>       # open a session by its full GUID or a prefix of it
python3 snoop.py              # list recent sessions (guid, project, title) to pick from
```

The `<guid>` is the same session id you'd pass to `claude --resume <guid>`.
Claude Code stores each conversation as `~/.claude/projects/<encoded-cwd>/<guid>.jsonl`;
`snoop.py` searches all project folders for a match, so you don't need to be
in the original working directory or know which project it belongs to. A
unique prefix of the guid works too.

Generated pages are written to `$TMPDIR/snoop/<guid>.html` and opened with
your default browser.

## Features

- **Full transcript** — every user message, assistant response, and tool
  call/result rendered in order, grouped into conversation turns.
- **Sidebar navigation** — one entry per prompt/response/tool call, each with
  a timestamp, a type badge, and a preview. Click to jump to it in the
  transcript.
- **Filtering** — toggle categories on/off (user prompts, assistant
  responses, thinking, or any individual tool by name) from the sidebar
  filter panel. Slash-command scaffolding (`/model`, etc.) is filtered out
  by default since it's rarely useful.
- **Search** — full-text search across messages and tool input/output, with
  match count and prev/next navigation (`/` to focus the search box, `Enter`
  / `Shift+Enter` to step through matches).
- **JSON viewer** — tool inputs and JSON-shaped tool results render as a
  collapsible tree instead of a wall of text; long plain-text output gets a
  "Show more" toggle.
- **Session events log** — a separate panel (off by default) for
  lower-level events: mode changes, permission-mode changes, file history
  snapshots, tool availability changes, turn durations.
- **Resizable sidebar** — drag the divider to widen it (useful for long MCP
  tool names); double-click the divider to reset.
- Light/dark theme follows your system setting.

## Requirements

Python 3, standard library only. No `pip install` needed.
