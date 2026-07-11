# quahog

Interactive console sessions — with full TTY emulation — captured in Jupyter
notebooks as auditable plain text.

```python
import quahog as q

h = q.bash()          # spawn a shell; the cell shows a live, resizable console
h                     # (display the handle to embed the console)
```

```
%qua make test        # run a command in the default session, output captured
%qua -s bash1 ls -la  # pick a session; -t 30 timeout; -b don't wait
```

```
%%qua
cd /tmp
echo one
echo two
```

The core rule: **the live console is a pure client-side view; the canonical
record of every command is plain text in the cell output** — what git, and
LLMs reading your notebook, see.

Programmatic API:

```python
r = h.run("ls -la")   # CommandResult
r.text                # clean text (escape sequences stripped)
r.raw                 # byte-faithful output, colors and all
r.returncode, r.ok
r = h.run("sleep 9; echo hi", wait=False); r.wait(30)

f = h.fork("make -j8")        # fresh stdio: separate stdout/stderr,
f.stdout, f.stderr, f.wait()  # session stays free; Popen-shaped handle

h.pid, h.poll(), h.wait(), h.terminate(), h.kill()   # Popen-shaped
h.stdin.write("y\n"); h.interrupt(); h.resize(30, 120)
h.reinject()          # re-type shell integration after exec/su

q.sessions            # registry; q.attach("bash1"); q.default
```

Minuting: commands you type *into the embedded console* are captured — their
output is appended as plain text to the cell that displayed the console (the
transcript), and each one is tracked in `h.minutes`, a list of
`Minute(when, command, raw, text, returncode)` where `raw`/`text` are slice
objects into the session-lifetime streams `h.raw` / `h.text` (so
`h.text[m.text]` is that command's output). Turn them into a notebook cell
explicitly:

```python
h.dump_minutes_as_cell()                  # everything since the last dump
h.dump_minutes_as_cell(since=0)           # or by index / date / datetime
h.dump_minutes_as_cell(prefix_per_cmd=False)  # one %%qua block instead of %qua lines
```

The new cell is created via a `set_next_input` payload riding the calling
cell's execution — the only payload timing JupyterLab *and* VS Code honor
(payloads written between executions are dropped; only the last per execution
wins). One dump per cell. Toggle capture with `h.minuting`.
Displaying `h` again in another cell *hops* the live console there; the old
view freezes into a static snapshot.

Design document: [PLAN.md](PLAN.md). Milestones 1–2 are done (local bash/zsh,
unix only; run/fork/magics/minuting/hop). Recording, ssh, and Windows support
are later milestones.

## Development


Tests: `pip install -e '.[dev]' && pytest`
