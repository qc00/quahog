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

h.pid, h.poll(), h.wait(), h.terminate(), h.kill()   # Popen-shaped
h.stdin.write("y\n"); h.interrupt(); h.resize(30, 120)
h.reinject()          # re-type shell integration after exec/su

q.sessions            # registry; q.attach("bash1"); q.default
```

Design document: [PLAN.md](PLAN.md). This is milestone 1 (local bash/zsh,
unix only). Recording, ssh, minuting, and Windows support are later milestones.

## Development


Tests: `pip install -e '.[dev]' && pytest`
