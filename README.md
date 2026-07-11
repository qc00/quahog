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

Recording and hygiene:

```python
h = q.bash(record=True)      # or h.record(True) any time; sidecar next to the
                              # notebook: deploy.ipynb -> deploy.quahog/*.cast
h.record(False)              # pause; h.record(True) resumes
h.erase(2)                   # redact the last 2 keystrokes from the recording
                              # (works within the delayed-flush tail, echoed or not)
h.screenshot()                # current screen -> anchor cell, as text
h.cast_path, h.recording      # where it's writing, whether it's live
```

The one hard invariant: interactively typed passwords never reach disk.
Input is auto-replaced with `[input suppressed]` when the local terminal is in
canonical no-echo mode (`read -s`, `passwd`, `sudo` with `pwfeedback`) or a
password interceptor has pre-armed suppression for a matched remote prompt
(`sudo`/`su`/`ssh`/`passwd`). Feed secrets from a keyring without ever
recording them: `h.send(secret, record=False)` or `h.stdin.raw.write(...)`.
Widget toolbar: ⏸ pause/resume, ⌫ erase (flashes on an un-echoed or masked
keystroke), camera for a screenshot.

Interceptors (`quahog.interceptors`) match a command and act around it —
`vim`/`nano` diff the file before/after into the cell; `less`/`man` are a
no-op (use the screenshot button); `sudo`/`su`/`ssh`/`passwd` arm password
suppression. Register your own: `quahog.interceptors.register(my_interceptor)`,
or ship one via the `quahog.interceptors` entry-point group.

While a full-screen app owns the alt-screen (`vim`, `less`, `htop`), `run()`
refuses and minuting pauses — interact through the embedded console instead.

Design document: [PLAN.md](PLAN.md). Milestones 1–3 are done (local bash/zsh,
unix only; run/fork/magics/minuting/hop/recording/interceptors). ssh and
Windows support are later milestones.

## Development


Tests: `pip install -e '.[dev]' && pytest`
