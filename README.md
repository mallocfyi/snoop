# snoop

A searchable archive of everything you've done with Claude Code.

Every conversation is already on your disk — Claude Code writes each one to
`~/.claude/projects/…/<guid>.jsonl` and keeps it forever. snoop turns that into
something you can use: search every prompt you've ever typed across every
project, then open a session and see what the agent did, which files it
changed, what it ran, and what it cost.

![Searching across sessions, then opening one to see its summary](demo.gif)

```
python3 snoop.py           # index of every session
python3 snoop.py <guid>    # one session
```

Writes static HTML to `~/.snoop/` and opens it. No server, no dependencies, no
network — Python 3 standard library only.

Costs are estimates at public list prices; edit `MODEL_RATES` in `snoop.py` if
your rates differ.
