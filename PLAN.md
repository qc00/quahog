# Quahog — interactive console sessions captured in Jupyter notebooks

---

## 1. Operation overview

The state and interactions with a PTY are all captured in a `Session` and can be accessed in many ways:

- The primary UI is an XTerm.js-based console embedded in a Jupyter cell output.
- There are also APIs in the `Session` to read/write/run commands against the PTY.
- Various tools, interceptors and `qua` commands exist to improve user experience and scriptability.

---

## 2. Architecture

```
┌────────────────────────── frontend (browser) ──────────────────────────┐
│  anywidget view: xterm.js (+ fit, serialize, webgl addons)             │
│  toolbar: session name · title · float · terminal · serve · record · erase · snapshot  │
└───────────────▲────────────────────────────────────────────────────────┘
                │ Jupyter comms (binary buffers for PTY bytes)
┌───────────────▼──────────────── kernel (Python) ────────────────────────┐
│  quahog.Session                                                         │
│   ├─ PTY driver: ptyprocess (unix), pywinpty/ConPTY (windows)           │
│   ├─ Tap: OSC 133 parser, alt-screen detect, echo tracker, .cast writer │
│   ├─ %qua/%%qua magics · exec/fork sessions · qua cat/tar/download      │
│   └─ attach server: unix socket → native window / telnet bridge         │
│  Interceptors: special handling for certain commands, passwords, etc.   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Chosen libraries

| Concern | Library | Why |
|---|---|---|
| Terminal rendering | **xterm.js** + fit addons | Full TTY emulation; what JupyterLab's own terminal uses |
| Widget plumbing | **anywidget** | Works in JupyterLab, Notebook 7, VS Code, Colab; binary buffers over comms |
| Local PTY (unix) | **ptyprocess** | PTY spawn, resize, termios access |
| Local PTY (windows) | **pywinpty** (ConPTY) | Real TTY for cmd.exe, PowerShell, and `wsl.exe` |
| Command boundaries | **OSC 133** shell integration | In-band → works over ssh; industry standard |
| Command minutes writing | **IPython `set_next_input`** | Kernel-side, works in VS Code; see §5 |
| Session recording | **asciicast v2** written by our own tap; replay via the `asciinema` CLI (out of scope: an in-notebook player) | Timestamped, committable, replayable |
| Remote | **socat** pty for `exec` | PTY is the only channel that survives an arbitrary multi-step jump path. |
| Screen state | **pyte** | Kernel-side "what's on screen" for prompt heuristics & screenshots |

(p)npm is used to assemble the front-end JS and CSS, which are then packaged into the python as source.

---

## 3. Kernel-side model

### `Session` — a live PTY with a small, subprocess.Popen-like surface

```python
h = q.bash(cwd=..., env=...)          # also: q.zsh(), q.powershell(), q.cmd(), q.wsl(distro=None)
```

Every `Session` carries
- the **non-blocking** subset of the `subprocess.Popen` vocabulary as plain methods:
  `pid`, `returncode`, `poll()`, `terminate()`, `kill()`, `send_signal(sig)`
— plus PTY-flavored I/O:
  `send(bytes)`, `sendline(str)`, `resize(rows, cols)`.

Anything that **waits** will eventually be refactored to a coroutine, awaited with Jupyter's native top-level `await`:
`await h.wait()` for process exit, `await h.run(...)` / `await r.wait(...)`.
Awaiting instead of blocking keeps the kernel event loop free, so live console views
keep updating while you wait.

`Session` also exposes **`h.stdin`** following the `sys.stdin` convention: a text file-like
(`h.stdin.write("yes\n")`) with a raw byte layer underneath (`h.stdin.buffer.write(b"\x03")`).
Programmatic keystrokes can bypass the recording: `record=False` on `send()`/`sendline()`, or
the `h.stdin.raw` unrecorded variant — the bytes go to the PTY but the `.cast` gets only an
`[input suppressed]` placeholder. This is the sanctioned path for feeding secrets from a
keyring/vault into an interactive prompt without them ever touching disk.

### `SessionState` — all cross-thread state behind one lock

Three kinds of thread touch a session at once: the PTY **reader thread** (drains bytes, drives
the OSC tap, appends to the streams), the **kernel/event-loop thread** (widget input, resize,
toolbar actions), and short-lived **worker threads** (auto-inject/reinject, copies). To avoid
threading mistakes, every piece of mutable state shared across those threads lives in one
`SessionState` object that owns a single lock and exposes only methods that take it.

What moves inside: the scrollback ring, the pyte screen mirror, the OSC parser, the lifetime
`raw`/`text` streams (plus their decoder and flush buffers), the active `run()` result, the
interactive-capture state, the `minutes` list, the active-interceptor list, the input-dedup
buffer, and the view registry. Methods return **snapshots** (`text()` hands back a `str`,
`views()` a tuple copy); all I/O — xterm writes, `display`/`update_display_data`, `.cast`
appends — happens *outside* the lock on the returned value, so the lock never spans a blocking
send and one non-reentrant lock suffices. `Session` becomes a thin façade: PTY lifecycle, the
Popen-ish surface, and orchestration, delegating all shared state to its `SessionState`.

### Running commands

```
%qua ls -la                     # line magic → default session (last created / q.default)
%qua -s prod -t 300 make test   # explicit session, timeout
%qua tail -f app.log &          # trailing & (as in any shell) → don't wait for it
```

```
%%qua prod
step one
step two
```

- The `%qua`/`%%qua` magics are **synchronous**: they type the command and block the cell until
  it finishes (`-t` sets a timeout), as today; a trailing `&` (or `-b`) returns immediately with
  the live result. The programmatic method is a **coroutine**: `r = await h.run("make test",
  timeout=300)`; for fire-and-forget, `r = await h.run("make test", wait=False)` hands back the
  live result the moment the command is typed, and you `await r.wait()` / poll `r.done`,
  `r.returncode`, `r.raw`, `r.text` later. Magic and method share **one** launch-and-capture
  core; only the wait differs — a synchronous wait on the completion event for the magic, an
  awaited bridge over the same event for the method.
- Captured interactive commands (§5) are written as `%qua …` cells, so the recorded notebook
  stays terse and re-runnable.
- Mechanics: commands are typed into the PTY with bracketed paste; OSC 133 `C`→`D;<exit>`
  delimit output; `run()` refuses (or queues, opt-in) while the alt-screen is active.
- A PTY merges stdout/stderr by nature — separated streams are what `fork()` is for (locally).

### `h.exec()` and `h.fork()` — a command as its own object

`run()` types a command and folds its output into the cell. `exec()` and `fork()` instead hand
back a session-like object with its own `text`/`raw`/`returncode`/`stdin` and a displayable
console. They differ in the **channel** the command runs on:

- **`h.exec(cmd, background=False, mirror=False) -> ExecSession`** runs the command on the
  **current shell's own PTY** — wherever you're navigated to — so it needs nothing on the far
  end but `socat`, which provides the command a pty (keeping `isatty()` true: colours,
  tty-sensitive behaviour). A PTY is one merged stream, and the kernel can't tell one program's
  bytes from another's on it, so exec's output is **tagged** to be demuxed into the
  `ExecSession`: a filter strips escape sequences from the pty stream (which both yields clean
  text and guarantees the payload can't contain the `BEL`/`ST` that would break the frame — so
  no base64) and wraps each chunk as `OSC 2607;QUA;O;<id>;<clean text>`; completion emits
  `OSC 2607;QUA;X;<id>;<rc>`, which the kernel reads and strips. This per-chunk tagging — not the
  OSC 133 `C`/`D` time-brackets `run()` relies on — is exactly what makes **`background=True` /
  a trailing `&`** work: a backgrounded job's output interleaves on the one tty with the live
  shell, so only self-identifying chunks can be attributed to it. A *foreground* exec owns stdin
  until the prompt returns (`session.stdin`/`sendline()` raise meanwhile; feed the command via
  `handle.stdin`); a *backgrounded* one doesn't lock stdin (feeding a background job stdin is
  limited — a bg process can't read the controlling tty — a documented gap). `mirror` decides
  whether the exec's I/O is *also* folded into the parent's streams/console/recording (default
  off — isolation is the point). One honest limit: the captured output is escape-stripped clean
  text, so full-screen/binary fidelity lives in the console or a `screenshot()`, not the
  `ExecSession`.

- **`h.fork(cmd) -> ForkSession`** (**local sessions only**) runs the command over a fresh FIFO
  trio the kernel creates (`cmd <f0 >f1 2>f2 &`, via the injected `__qua_fork`), which buys
  genuinely separate `stdout`/`stderr` and true concurrency with the session — a single PTY
  can't. Because the FIFOs live on the kernel's own filesystem, fork works only while the
  session is on the **local** host; once you've navigated to a remote host the far shell can't
  see them, so reach for `exec` there instead (it rides the PTY). Reach for fork when you need
  the streams split; reach for exec when you need remote, or a live console for the command.
  (A **remote** `fork()` — separate streams over a side channel such as a tmux pane — is a
  candidate for a later release; the local FIFO path is what ships first.)


---

## 4. The widget: embed, resize, pop-out

- **Embed:** anywidget view hosting xterm.js; kernel-side ring buffer (~2 MB) replays
  scrollback to newly attached views, so `display(h)` after a page reload reconnects.
  Every cell that displays the session — `display(h)` — gets its own live, independent
  view; there is no "anchor" or "hop": output fans out to all attached views, and input
  from any of them merges into the one PTY, exactly like two clients attached to the
  same tmux session (nothing technical prevents this — it's the same fan-out the tap
  already does for pop-out views, below). Each cell's single output carries both the
  live widget and the current console log as `text/plain` (§5), kept in sync via
  `update_display_data`.
- **Resize:** CSS `resize: both` + fit addon; view reports cols/rows → `setwinsize()`.
- **Pop-out — no companion extension.** Three independent features, each a method *and* a
  toolbar button, freely combinable. The toolbar is our own HTML, so icons are a vendored SVG
  set (Lucide/codicons) — proper graphical icons, not unicode glyphs — with one deliberate
  exception: **float's icon is the Greek letter π** (a floating thing on legs).
  1. *Float* — `h.float()` / **π**: the widget detaches into a draggable/resizable overlay in
     the notebook page. Zero dependencies.
  2. *Terminal app* — `h.terminal()` / terminal icon: the kernel opens a local attach server
     (unix socket) and launches the platform terminal app running the bridge client
     (`python -m quahog attach <id>`):
     macOS `open -na iTerm/Terminal`, Linux `x-terminal-emulator`, Windows `wt.exe`.
  3. *Serve* — `h.serve()` / server icon: binds a localhost-only TCP port with a one-shot token and
     prints ready-to-paste lines: `telnet 127.0.0.1 4821` / `nc 127.0.0.1 4821`. Useful for
     remote Jupyter: the same attach command can be pasted into a **Jupyter terminal tab**
     (classic `/terminals/N` URLs still exist in jupyter-server, but its API can't specify the
     command a terminal runs, and Lab has no URL scheme that opens a terminal with a command —
     so we print the command for the user to paste rather than pretending to automate it).
- **Concurrent displays** — embedded, pop-out, or any mix — are all just views on the same
  session. The only genuinely shared state is the PTY's single winsize: views can't each have
  their own dimensions. Policy: **last resizer wins**; other views render at the PTY's size
  (scroll/clip if their box is smaller). `h.resize(lock=True)` pins the size for demos.
- **Screenshot:** a labeled toolbar button (always available, prominent in full-screen mode)
  dumps the current screen as preformatted text into every live view's console log (§5);
  `h.screenshot()` does the same programmatically. Implemented kernel-side from the pyte
  screen, so it works even with no view attached.

---

## 5. Minuting interactive commands to cells

When the user types a command into the widget of a supported shell, the tap assembles
`{command, output, exit_code, cwd}` from the OSC 133 markers — identical shape to a `run()`.
Getting that into a *cell* is the frontend-dependent part:

| Approach | Works in Lab/NB7 | Works in VS Code | Fit |
|---|---|---|---|
| `get_ipython().set_next_input(text)` | ✅ | ⚠️ see note | Unsatisfactory, but only way to work in VS Code |
| JupyterLab `app:commands` (via ipylab) | ✅ | ❌ | Dropped — manual dump removed the need for a fast path |

Note: `set_next_input` rides on an `execute_reply` **payload**, so payloads written *between*
executions never render, and when several are written in one execution **VS Code honors only
the last**.

- **Output stays in every cell that displayed the session.** A cell's single output already
  carries the live widget-view mimetype *and* a `text/plain` console log (§4) — the session's
  clean accumulated text plus any non-literal blocks (screenshots, interceptor notes). As
  interactive commands complete, `text/plain` is refreshed via `update_display_data` for every
  such cell (not just the most recent one), so a git diff, nbconvert, or an LLM reading the
  `.ipynb` sees one readable output per display, no separate transcript block.
- **`h.minutes` list** Every interactive command appends a
  `Minute` NamedTuple: `when` (timestamp), `command`, and `raw`/`text` **slice objects**
  indexing into the handle's session-lifetime streams `h.raw` and `h.text`, so `h.text[m.text]` is that command's output.
- **Cell creation is explicit:**
  `h.dump_minutes_as_cell(since=LAST_DUMP, until=None, prefix_per_cmd=True)`.
  `since`/`until` accept a list index, a date/datetime, or the `LAST_DUMP` sentinel (the index
  after the last dumped entry, also readable as `h.last_dump`). `prefix_per_cmd`: `True` →
  one `%qua cmd` line per command (easy to split into cells); `False` → a single `%%qua`
  header; `None` → bare commands. The cell is written directly via the `set_next_input`
  payload; nothing is returned.
- Toggle: `h.minuting = True/False` (plus a widget toolbar button later) — it only controls
  appending to the list. The feature is called **minuting**, as in writing the meeting
  minutes; deliberately nothing like the word "record", which §6 owns for keystroke recording.
  Commands typed while input suppression is active (§6) are never minuted.

---

## 6. Full-screen apps, recording, and password hygiene

- **Alt-screen detection:** the tap watches `CSI ?1049h/l` (and `?47h`). During alt-screen the
  session is *interactive*: `run()` blocks, minuting pauses, widget shows a badge and
  surfaces the screenshot button. `vim`, `less`, `htop` work naturally.
- **Recording:** an option at session creation and forking — `q.bash(record=True)`,
  `h.fork(cmd, record=True)`, `h.exec(cmd, record=True)` (forks and execs get their own `.cast`
  file, defaulting to the parent's setting) — plus `h.record(True/False)` as the runtime
  toggle. Recording tees PTY traffic into
  **asciicast v2** files in a *visible, committable* folder next to the notebook:
  `deploy.ipynb` → `deploy.quahog/<session>-<ts>.cast`. (Notebook path discovered via
  `JPY_SESSION_NAME` env, VS Code's `__vsc_ipynb_file__`, else cwd fallback.) Cell outputs
  reference the file so replay tooling can find it. Committing is the expected workflow; add to
  `.gitignore` only if you choose to.
- **Password hygiene — the one hard invariant: interactive passwords never reach disk.**
  - *Delayed-flush tail:* the `.cast` writer keeps the last few seconds of events in memory
    before flushing to disk. Within that window the **⌫ erase button always works** — the user
    can redact the most recent keystroke(s) *regardless of what was or wasn't echoed*. Erasure
    rewrites input events in the tail in place (into `[input suppressed]` placeholders), so
    nothing is ever reordered and timestamps stay monotonic. No next-event decision logic.
  - *Automatic suppression is narrow and mechanism-level.* Input is auto-replaced by a
    placeholder only when:
    (a) the local PTY's termios reports `ECHO` off — which covers local password prompts
    including `sudo` with `pwfeedback` (it still turns `ECHO` off and paints the `*`s itself), or
    (b) a shipped **password interceptor** (below: `sudo`, `su`, `ssh`, `passwd`) matched the
    running command and suppressed input recording in its `before()` — this is how *remote*
    prompts are covered.
    Un-echoed input outside those two cases is **not** auto-suppressed: plenty of legitimate
    keystrokes produce no echo (TUI mode switches, arrow keys, `vim` normal-mode commands).
    The echo classifier stays three-way (verbatim / masked `*`/`•` / none) to drive the
    affordances below, and the test matrix must include `sudo` with `pwfeedback` enabled.
  - *Widget affordances:*
    - **⏸ pause recording** and **⌫ erase previous keystroke(s)** buttons always in the toolbar;
    - whenever a keystroke goes un-echoed or masked-echoed, the **⌫ erase button flashes** — a
      prompt, not an action; if the user cared, they press it;
    - on **Enter**, the **⏸/resume button flashes** to prompt the user to confirm recording
      state. Enter triggers no erasure of its own.
  - *Residual gap, stated honestly:* an unrecognized command prompting for a secret on a
    *remote* host is covered only by the flashing ⌫ within the flush window — or by writing a
    one-line password interceptor for that command.
  - Output-side secrets in cells are explicitly out of scope — delete the cell if needed.
- **Interceptors** (`entry_points` group `quahog.interceptors`): plugins that match known
  commands and act around them — `match(argv, session)`, `before(ctx)`,
  `after(ctx) -> CellOutput | None`, and that's the whole API: `ctx` exposes the same recorder
  controls as the toolbar (pause/resume recording, suppress input), so there is no dedicated
  password hook — a password interceptor is just a `before()` that suppresses input recording,
  released in `after()` or when the prompt is answered. Shipped:
  - `vim`/`nano`/`vi <file>` → snapshot before (via injected `__qua_snapshot`, works remotely),
    unified diff as cell output after;
  - `less`/`man` → no cell effect (the screenshot button covers "what did I look at");
  - **password interceptors** for `sudo`, `su`, `ssh`, `passwd` — this is where the
    prompt-detection regexes (`password:`, `passphrase`, `PIN`, localized variants) live,
    scoped to a matched command instead of running globally, so recording suppression pre-arms
    for known prompts without ever surprising the user elsewhere.

---

## 7. Remote sessions: navigate interactively, operate over the PTY

quahog doesn't connect to remote hosts for you. You reach one the way you would in any
terminal — `ssh`, `sudo`, a restricted shell, whatever the path requires — from a local
session, and quahog operates on **wherever that PTY currently sits**, tracking only
what the shell integration reports (cwd, shell kind, whether integration is live). Every remote
feature — `exec`/`fork` (§3) and copy (below) — rides the one channel that survives an arbitrary
multi-step login: the interactive PTY byte stream.

That single-channel constraint is deliberate: a real jump path
often isn't a single tunnelable command line, and an ssh `ControlMaster` socket lives on the
*client* host, out of the kernel's reach past the first hop — so a managed ssh/mux/ProxyJump
layer would buy nothing that survives the navigation anyway.

### The injected snippet

POSIX-sh/zsh/pwsh snippet typed as **plain (unencoded) shell source** via bracketed paste.
Golf it as hard as we like to keep the paste short — the only rule is *no encoding or
obfuscation* to avoid tripping antivirus tooling.

- emits OSC 133 A/B/C/D + OSC 7 (cwd) + a quahog-private handshake (`OSC 2607;QUA;I`: shell
  kind, host, user — confirms a successful (re)inject),
- defines `__qua_snapshot` (interceptors), the `exec` markers `__qua_xb`/`__qua_xe`, and the
  in-console copy pseudo-commands `quahog cat` / `quahog tar` / `quahog download` (below).

`h.reinject(full=True)` re-types the whole snippet after `su`, `docker exec`, a hand-typed
`ssh`, `exec zsh`, or a restricted shell that dropped it — required on any shell that can't
source quahog's local file (i.e. anything remote). The widget shows an "integration lost —
reinject?" hint when prompts stop carrying markers.

**The private OSC command number is `2607`**, chosen because it is unclaimed — the earlier
`5522` collides with other tools. Every quahog payload is framed `OSC 2607 ; QUA ; <kind> ;
<args…>`: the `QUA` signature means the kernel ignores any foreign sequence that ever reused
the number, and quahog's own sequences stay inert in a terminal that doesn't know 2607. The
`<kind>`s are `I` (handshake: shell/host/user), `E` (typed-command text — minuting and
interceptors), `O`/`X` (exec output / exit — §3), and the copy requests (below).

### Running commands remotely

`run()` and the `%qua`/`%%qua` magics work over **any integrated shell**: once you've navigated
to a remote host and re-injected (below), the remote shell emits the same OSC 133 `C`/`D`
markers, so typing a command and capturing its output is identical to local. `exec()` (§3)
likewise works at any depth — nothing about it is ssh-specific; it acts on whatever shell the
session is currently sitting in, riding the current PTY, so it needs only `socat` on the far
end. The one exception is `fork()`: its separate streams come from kernel-local FIFOs, so it is
a **local-session** feature for now (a remote fork over a tmux pane may come later); once you've
navigated away, `exec` is the way to run a command as its own object.

### `quahog cat` / `quahog tar` / `quahog download` — file copy over the PTY

The injected `quahog` function emits a private OSC and the kernel performs
the transfer over the same PTY channel — no scp, no ControlPath, working at any navigation depth.

```
quahog cat build/app.tar.gz > /srv/app.tar.gz   # local → remote: kernel resolves the path
                                                # relative to the notebook and streams the bytes
                                                # into the PTY; it sends a length header first so
                                                # the helper reads exactly that many (head -c) —
                                                # binary-exact, no in-band EOF, no base64
quahog tar somedir | (cd /dest && tar x)        # same, over a tar stream the kernel builds first
cat /var/log/app.log | quahog download app.log  # remote → local: quahog download brackets its
                                                # stdin with OSC start/end tags and base64-frames
                                                # it (a file can't be escape-stripped, and its
                                                # size is unknown while streaming, so base64 is
                                                # the safe framing); the cell renders a download box
```

Programmatic twins `h.upload()` / `h.download()` mirror these; typing the `quahog`
pseudo-commands is the primary, paste-friendly interface. (VS Code's data-URI download handling
is weaker than Lab/NB7 — verify and document, per §5.)

---

## 8. Shell support matrix

| Feature | bash/zsh | WSL (`q.wsl()`) | PowerShell | cmd.exe |
|---|---|---|---|---|
| Embedded TTY console | ✅ | ✅ pywinpty→`wsl.exe` | ✅ ConPTY | ✅ ConPTY |
| OSC 133 markers | ✅ | ✅ (bash inside) | ✅ prompt + PSReadLine | ❌ |
| `%qua` / `run()` with exit code | ✅ | ✅ | ✅ | ⚠️ sentinel wrap (`& echo <s> %errorlevel%`) |
| Interactive → cells | ✅ | ✅ | ✅ | ❌ |
| `exec()` (own session) | ✅ socat pty | ✅ socat in WSL | ⚠️ pwsh port | ❌ |
| `fork()` (separate streams, local only) | ✅ FIFOs | ✅ FIFOs in WSL fs | ⚠️ named pipes | ❌ |
| Interceptors | ✅ | ✅ | ✅ | ❌ |
| `quahog cat/tar/download` copy | ✅ | ✅ | ⚠️ pwsh port | ❌ |

cmd.exe is deliberately bare-minimum: embed + sentinel-wrapped `run()`, nothing else.
WSL is a first-class initial shell: Windows-side PTY, POSIX-side integration.

---

## 9. Repository layout

```
quahog/
  pyproject.toml            # hatchling; deps: anywidget, ptyprocess, ipython, pyte; [win]: pywinpty
  src/quahog/
    __init__.py             # bash(), zsh(), powershell(), cmd(), wsl(), sessions
    magics.py               # %qua, %%qua
    session.py              # Session façade: PTY lifecycle, async run()/wait(), fan-out
    state.py                # SessionState: all cross-thread state behind one lock
    pty_unix.py  pty_win.py
    osc.py                  # OSC 133/7/2607 parser (incremental)
    result.py               # CommandResult (.raw/.text; awaitable)
    record.py               # asciicast v2 writer, delayed-flush tail, echo classifier
    minutes.py              # minuting: Note/Transcript side-output, dump_minutes_as_cell
    attach.py               # unix-socket attach server, telnet bridge, `python -m quahog attach`
    inject/                 # posix.sh, zsh.zsh, pwsh.ps1 (+ quahog cat/tar/download, exec markers)
    runner.py               # exec(): ExecSession, socat-pty tagging, OSC 2607 O/X demux
    copy.py                 # quahog cat/tar upload (length-framed) + download (base64) + box
    fork.py                 # fork(): ForkSession over a local FIFO trio (local sessions only)
    interceptors/           # interceptor API + vim/nano/password built-ins
    widget/                 # anywidget ESM bundle (xterm.js vendored at build time), CSS
  js/                       # bundle sources; esbuild; npm is dev-only, output committed/shipped
  tests/                    # pytest; PTY integration tests against real bash
  examples/demo.ipynb
```

---

## 10. Milestones

1. **MVP (local bash):** Session + unix PTY, xterm.js embed with resize, OSC 133 inject/parse,
   async `run()` / awaitable `CommandResult` (`.raw`/`.text`), `%qua`/`%%qua` magics, the
   (mostly-sync) Popen-ish Session API, registry. *Exit criterion: a committed notebook shows
   every command's output as plain text.*
2. **Cells & multi-view:** every `display(h)` an independent live view (fan-out, **no hop**),
   `text/plain` kept via `update_display_data`; pull-based `dump_minutes_as_cell` over a
   `set_next_input` payload (validated in VS Code explicitly); multi-session; local `fork()`.
3. **Recording & hygiene:** asciicast writer + `<notebook>.quahog/` sidecars, alt-screen mode,
   delayed-flush tail + echo classifier, ⏸/⌫ toolbar with flashing affordances, screenshot
   button, interceptor API (vim-diff + password interceptors).
4. **Remote & concurrency (over the PTY):** reach targets by navigating interactively (real
   ssh/sudo/restricted shells) + `reinject(full=True)`; the `OSC 2607;QUA` private channel;
   `exec()` returning an `ExecSession` (socat pty, escape-stripped OSC-tagged output, `&`
   support, `mirror`); local `fork()`/`ForkSession` over a FIFO trio (separate streams;
   remote fork over a tmux pane deferred to a later release); `quahog cat`/`quahog tar`/`quahog
   download` copy. (Managed ssh and hop chaining has been descoped.)
5. **Pop-out extras & Windows:** terminal-app launch + telnet attach server; pywinpty backend,
   PowerShell integration, WSL, cmd.exe subset.

---

## 11. Risks & open questions

- **VS Code behavior is the linchpin of §5** — verify early (milestone 2 gate): `set_next_input`
  payload handling (multiple queued payloads per execution vs. one batched `%%qua` cell) and
  whether `update_display_data` is honored/persisted after the anchor cell finishes executing.
- **OSC passthrough:** tmux ≥3.3 is clean; GNU screen needs the re-emit workaround; mosh is
  known to strip sequences — document.
- **Remote echo correlation** can be confused by TUIs that consume input without echoing;
  consequence is only over-suppression of the recording, never leakage — acceptable bias.
- **`exec` output fidelity:** the tagger strips escape sequences, so the handle holds clean
  text only — full-screen/binary output isn't faithfully captured there (console or
  `screenshot()` for that). socat is assumed on targets; verify the exact pty address flags.
- **Copy framing asymmetry:** upload is length-framed raw (kernel knows the size → binary-exact,
  no base64); download must base64 (a file can't be escape-stripped and its streaming size is
  unknown up front). Confirm large-file latency over the typed PTY path is acceptable.
- **Async surface & the magics:** the programmatic waits are coroutines (top-level `await`),
  while `%qua`/`%%qua` stay synchronous and block the cell (as today), sharing the same
  launch-and-capture core and waiting on the completion event synchronously. Confirm a blocking
  `%qua` doesn't starve *other* sessions' live views for longer than acceptable — if it does,
  the fallback is async magics (verify IPython/ipykernel coroutine-magic support first).
- **cmd.exe** stays capped by design; PowerShell/WSL are the real Windows targets.
- Name availability re-verified at registration time; `quahog` free on PyPI as of 2026-07-10.
