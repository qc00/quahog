# Quahog — interactive console sessions captured in Jupyter notebooks

**Name:** `quahog` — the hard-shell clam. Free on PyPI (verified 2026-07-10; `whelk`, `whorl`,
`nacre` are taken). The pseudo-command prefix and magic are **`qua`** — long
enough to be unambiguous and greppable, short enough to type: magic `%qua`/`%%qua`,
pseudo-commands `quassh`, `quacp`, `quawsl`. No collisions with existing tools. Verified-free
name alternates if the clam doesn't spark joy: `limpet`, `winkle`, `murex`, `cockle`.

```python
import quahog as q

h = q.bash()                          # local session, console embeds in this cell's output
```

```
%qua make test                          # line magic: run in default session, capture output
```

```
%%qua prod                              # cell magic: run several commands in session "prod"
cd /srv/app
git pull
systemctl --user restart app
```

---

## 1. Core principle: live console is ephemeral, text is canonical

> **The embedded console is a pure client-side view. The canonical record of every command is
> plain text written into the cell's output area.**

- Every command produces a `CommandResult` whose repr writes `text/plain` into the `.ipynb`
  (what git and LLMs see). Two representations are kept and exposed:
  - `r.raw` — the byte-faithful stream, escape sequences and all (also available per-command in
    the sidecar recording),
  - `r.text` — clean text with escapes stripped/rendered out (this is what goes in the cell).
- The live console widget never stores anything in the notebook. Displaying, closing, or
  hopping it never changes what's committed to git.
- Secrets *displayed* in output are acceptable (a cell can be deleted); the invariant that
  matters: **interactively typed passwords never reach disk** — not the notebook and not the
  recording (§6).
- Full keystroke recordings go to a visible, committable sidecar folder (§6), never into the
  notebook JSON.

---

## 2. Architecture

```
┌────────────────────────── frontend (browser) ──────────────────────────┐
│  anywidget view: xterm.js (+ fit, serialize, webgl addons)             │
│  toolbar: hop · float(π) · terminal-app · serve · ⏸ · ⌫ · camera      │
└───────────────▲────────────────────────────────────────────────────────┘
                │ Jupyter comms (binary buffers for PTY bytes)
┌───────────────▼──────────────── kernel (Python) ────────────────────────┐
│  quahog.Session ── registry; Popen-style API on every session           │
│   ├─ PTY driver: ptyprocess/pexpect (unix), pywinpty/ConPTY (windows)   │
│   ├─ Tap: OSC 133 parser, alt-screen detect, echo tracker, .cast writer │
│   ├─ %qua / %%qua magics · quassh/quacp pseudo-command handling                 │
│   ├─ minuter: transcript display + set_next_input queue (+ ipylab)      │
│   └─ attach server: unix socket → native window / telnet bridge         │
│  Interceptors (entry_points "quahog.interceptors"): vim-diff, passwords │
└─────────────────────────────────────────────────────────────────────────┘
```

### Chosen libraries

| Concern | Library | Why |
|---|---|---|
| Terminal rendering | **xterm.js** + fit/webgl addons | Full TTY emulation; what JupyterLab's own terminal uses |
| Widget plumbing | **anywidget** | Works in JupyterLab, Notebook 7, VS Code, Colab; binary buffers over comms |
| Local PTY (unix) | **pexpect / ptyprocess** | PTY spawn, resize, termios access |
| Local PTY (windows) | **pywinpty** (ConPTY) | Real TTY for cmd.exe, PowerShell, and `wsl.exe` |
| Command boundaries | **OSC 133** shell integration | In-band → works over ssh; industry standard |
| Cell creation | **IPython `set_next_input` payload** (+ **ipylab** fast path when in Lab) | Kernel-side, works in VS Code; see §5 |
| Session recording | **asciicast v2** written by our own tap; replay via the `asciinema` CLI (out of scope: an in-notebook player) | Timestamped, committable, replayable |
| Remote transport | **system OpenSSH** in a PTY + **ControlMaster**; **tmux control mode** (`-CC`) for implied sessions | Respects ~/.ssh/config, agents, ProxyJump |
| Screen state | **pyte** | Kernel-side "what's on screen" for prompt heuristics & screenshots |

(p)npm is used to assemble the front-end JS and CSS, which are then packaged into the python as source.

---

## 3. Kernel-side model

### `Session` — Popen-shaped, every one of them

```python
h = q.bash(cwd=..., env=...)          # also: q.zsh(), q.powershell(), q.cmd(), q.wsl(distro=None)
h = q.ssh("user@host -J bastion")     # takes a real ssh command line — paste-friendly (§7)
h = q.attach("name")                  # look up in q.sessions registry
```

Every `Session` (not just forks) carries the `subprocess.Popen` vocabulary:
`pid`, `returncode`, `poll()`, `wait(timeout)`, `terminate()`, `kill()`,
`send_signal(sig)`, plus PTY-flavored I/O: `send(bytes)`, `sendline(str)`, `resize(rows, cols)`.
`ForkHandle` (below) adds genuinely separate `stdout`/`stderr` file objects.

Every handle also exposes **`h.stdin`** following the `sys.stdin` convention: a text file-like
(`h.stdin.write("yes\n")`) with a raw byte layer underneath (`h.stdin.buffer.write(b"\x03")`).
Programmatic keystrokes can bypass the recording: `record=False` on `send()`/`sendline()`, or
the `h.stdin.raw` unrecorded variant — the bytes go to the PTY but the `.cast` gets only an
`[input suppressed]` placeholder. This is the sanctioned path for feeding secrets from a
keyring/vault into an interactive prompt without them ever touching disk.

### Running commands: `%qua` magic first, method second

```
%qua ls -la                     # line magic → default session (last created / q.default)
%qua -s prod -t 300 make test   # explicit session, timeout
%qua& tail -f app.log           # no-wait variant
```

```
%%qua prod
step one
step two
```

- The magic is sugar over `h.run(cmd, wait=True)`; the method remains for programmatic use
  (`r = h.run("make test", wait=False)`; `r.wait()`, `r.done`, `r.returncode`, `r.raw`, `r.text`).
- Captured interactive commands (§5) are written as `%qua …` cells, so the recorded notebook
  stays terse and re-runnable.
- **A new cell *type* is not possible**: the nbformat schema fixes `cell_type` to
  code/markdown/raw; unknown types fail validation and every frontend (Lab, NB7, VS Code)
  hard-codes the set. The portable equivalent is exactly this: a cell magic plus cell
  metadata/tags (`{"quahog": {"session": "prod"}}`) that a styling extension *could* use later
  for shell syntax highlighting in Lab. VS Code offers no hook for custom cell types either.
- Mechanics: commands are typed into the PTY with bracketed paste; OSC 133 `C`→`D;<exit>`
  delimit output; `run()` refuses (or queues, opt-in) while the alt-screen is active.
- A PTY merges stdout/stderr by nature — separated streams are what `fork()` is for.

### `h.fork()` — new command, new stdio, new handle

- **Local:** injected shell helper creates a FIFO trio, launches `cmd <f0 >f1 2>f2 &`; kernel
  returns a `ForkHandle` with real separate `stdout`/`stderr`.
- **Remote (mux):** `ssh -S <ctlpath> host cmd` — new exec channel, no re-auth, works through
  bastions.
- **Remote (implied tmux session, §7):** `tmux new-window` + `pipe-pane` capture — forking
  without even a new ssh channel.
- **Nested (user typed `ssh host2` inside the session):** the reinject handshake records the
  hop chain → `ssh -S sock host1 -- ssh host2 -- cmd`. Best-effort, documented limits.

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
  header; `None` → bare commands. Returns the text.
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
  `q.ssh("user@host", record=True)`, `h.fork(cmd, record=True)` (forks get their own `.cast`
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

## 7. Shell integration & remote sessions

### The injected snippet

POSIX-sh/zsh/pwsh snippet typed as **plain (unencoded) shell source** via bracketed paste.
Golf it as hard as we like to keep the paste short — the only rule is *no encoding or
obfuscation*: `eval "$(base64 -d …)"` is a classic malware-delivery signature that can trip
EDR/antivirus tooling. Bracketed paste handles the "survives weird terminals" problem; the
source just has to be paste-safe (no tabs, no prompt-expansion traps):

- emits OSC 133 A/B/C/D + OSC 7 (cwd) + a quahog-private OSC handshake (shell kind, hostname,
  depth — how nested-hop chains are tracked),
- defines `__qua_fork` (FIFO trio), `__qua_snapshot` (interceptors), and the **in-console
  pseudo-commands** `quassh` and `quacp` (below).

`h.reinject()` re-types it after `su`, `docker exec`, hand-typed `ssh`, `exec zsh`. The widget
shows an "integration lost — reinject?" hint when prompts stop carrying markers.

### `quassh` — a pseudo-command, paste-friendly by design

The primary interface is **a real ssh command line with a `q` in front**, usable in three
places with identical syntax:

```
%quassh -J bastion user@host            # magic in a cell  → returns/binds a handle
h = q.ssh("-J bastion user@host")     # same parser, programmatic
quassh user@host                        # typed inside an existing quahog console:
                                      # the injected function wraps real ssh and notifies the
                                      # kernel via OSC → kernel binds a handle to the nested hop
```

Under the hood: system OpenSSH in a PTY with `ControlMaster=auto` + `ControlPath` added
(unless the user passed their own), so `~/.ssh/config`, agents, MFA prompts all behave — they
appear in the embedded console like any terminal. On connect, auto-inject; `mux` keeps the
control socket for `fork()` and `quacp`.

### Multiplexers: not an argument, just the command line

`screen`/`tmux` is expressed exactly as you'd write it in real ssh, and **`-t` keeps its real
meaning**:

```
quassh user@host -t tmux    # TTY allocated → tmux UI appears → alt-screen/full-screen mode,
                          # outermost alt-screen heuristic suppressed, OSC markers trusted
quassh user@host tmux       # no -t: real ssh would die ("open terminal failed…") — quahog
                          # reinterprets this as the *implied session*: it runs
                          # `tmux -CC new -A -s quahog-<id>` (control mode) — headless text
                          # protocol, no UI, session persists, reattachable after kernel
                          # restart, and new-window/pipe-pane gives us remote fork() for free
quassh user@host screen     # minimal support: `screen -dmS` + `screen -X stuff` command
                          # injection; no control mode, so tmux is the recommended multiplexer
```

### `quacp` — file copy pseudo-command

```
%quacp build/app.tar.gz prod:/srv/releases/     # scp-style; "prod" is a session name or host
%quacp prod:/var/log/app.log ./logs/
quacp ./notes.txt laptop:                        # typed *inside* a remote console: the injected
                                               # function sends an OSC request; the kernel
                                               # performs the copy over the mux socket
q.cp(src, dst)  /  h.cp(src, dst)              # programmatic equivalents mirror quacp's name
                                               # and path syntax exactly; on a handle, a bare
                                               # (un-prefixed) path means "on this session"
```

Transport: `scp -o ControlPath` on the mux socket; nested chains stream tar through
`ssh -S … host1 -- ssh host2 -- tar …`.

---

## 8. Shell support matrix

| Feature | bash/zsh | WSL (`q.wsl()`) | PowerShell | cmd.exe |
|---|---|---|---|---|
| Embedded TTY console | ✅ | ✅ pywinpty→`wsl.exe` | ✅ ConPTY | ✅ ConPTY |
| OSC 133 markers | ✅ | ✅ (bash inside) | ✅ prompt + PSReadLine | ❌ |
| `%qua` / `run()` with exit code | ✅ | ✅ | ✅ | ⚠️ sentinel wrap (`& echo <s> %errorlevel%`) |
| Interactive → cells | ✅ | ✅ | ✅ | ❌ |
| `fork()` | ✅ FIFOs | ✅ FIFOs in WSL fs | ✅ named pipes | ❌ |
| Interceptors | ✅ | ✅ | ✅ | ❌ |
| `quassh` target / `quacp` | ✅ | ✅ | ✅ (pwsh remote) | ❌ |

cmd.exe is deliberately bare-minimum: embed + sentinel-wrapped `run()`, nothing else.
WSL is a first-class initial shell: Windows-side PTY, POSIX-side integration.

---

## 9. Repository layout

```
quahog/
  pyproject.toml            # hatchling; deps: anywidget, pexpect, ptyprocess; [win]: pywinpty; pyte
  src/quahog/
    __init__.py             # bash(), zsh(), powershell(), cmd(), wsl(), ssh(), attach(), sessions
    magics.py               # %qua, %%qua, %quassh, %quacp
    session.py              # Session (Popen-style), tap/fan-out, ring buffer
    pty_unix.py  pty_win.py
    osc.py                  # OSC 133/7/private parser (incremental)
    result.py               # CommandResult (.raw/.text), ForkHandle
    record.py               # asciicast v2 writer, delayed-flush tail, echo classifier
    minutes.py              # minuting: anchor transcript, set_next_input queue, ipylab path
    attach.py               # unix-socket attach server, telnet bridge, `python -m quahog attach`
    inject/                 # posix.sh, zsh.zsh, pwsh.ps1 (+ quassh/quacp functions)
    remote.py               # ssh cmdline parser, mux, tmux -CC driver, quacp transports
    interceptors/           # interceptor API + vim/nano/password built-ins
    widget/                 # anywidget ESM bundle (xterm.js vendored at build time), CSS
  js/                       # bundle sources; esbuild; npm is dev-only, output committed/shipped
  tests/                    # pytest; PTY integration tests via pexpect against real bash
  examples/demo.ipynb
```

---

## 10. Milestones

1. **MVP (local bash):** Session + unix PTY, xterm.js embed with resize, OSC 133 inject/parse,
   `run()`/`CommandResult` (`.raw`/`.text`), `%qua`/`%%qua` magics, Popen-style Session API,
   registry. *Exit criterion: a committed notebook shows every command's output as plain text.*
2. **Cells & hop:** anchor-cell transcript via `update_display` and `set_next_input`
   queue-and-flush (validate both in VS Code explicitly), ipylab fast path, hop with
   SerializeAddon freeze, multi-session, local `fork()`.
3. **Recording & hygiene:** asciicast writer + `<notebook>.quahog/` sidecars, alt-screen mode,
   delayed-flush tail + echo classifier, ⏸/⌫ toolbar with flashing affordances, screenshot
   button, interceptor API (vim-diff + password interceptors).
4. **Remote:** `quassh` (parser, mux, auto-inject, `reinject()`), fork over control socket,
   `quacp`/`cp()`, tmux `-CC` implied sessions, `-t` full-screen path, bastion + nested hops.
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
- **tmux -CC protocol** is semi-documented (iTerm2 is the reference consumer); budget time for
  protocol quirks.
- **cmd.exe** stays capped by design; PowerShell/WSL are the real Windows targets.
- Name availability re-verified at registration time; `quahog` free on PyPI as of 2026-07-10.
