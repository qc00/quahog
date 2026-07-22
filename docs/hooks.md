# The injected shell hooks

quahog's shell integration is in `src/quahog/injection.py` which holds string constants (`_SHARED`, `_BASH`, `_ZSH`)
and assembles the snippet for the shell type detected via probing.

Currently, it supports **bash and zsh only**.

The script carries no comments; this file is where it is explained instead.

## How it talks to the kernel

Their only (universal) way the hook(s) can communicate with the kernel is via
the PTY they are already attached to, so the hook writes everything to
stdout as an escape sequence, mixed in with ordinary command output. The
kernel's OSC parser picks those sequences back out of the byte stream.

Two OSC numbers are used.

**OSC 133** is the standard shell-integration marker set, understood by other
terminals too: `A` prompt start, `B` prompt end, `C` command about to run,
`D;<code>` command finished. `run()` captures whatever appears between `C` and
`D`, and takes the exit status from `D`.

**OSC 2607** is quahog's own, chosen because it is unclaimed. Every payload is
signed `QUA`, so the kernel ignores any foreign sequence that ever reused the
number, and quahog's sequences stay inert in a terminal that doesn't know 2607.

| Payload | Meaning |
|---|---|
| `P;<value>` | probe answer: empty = bash, a zsh version string = zsh |
| `I` | integration is live — sent once, as the last line of the hook snippet |
| `E;<text>` | the command text that was typed (as parsed by the shell) |
| `O;<id>;<fd>;<base64>` | a raw output chunk from an `exec()`; `fd` is 1 (stdout) or 2 (stderr) |
| `X;<id>;<rc>` | an `exec()` finished, with its exit status |
| `U;<path>` | a `quacat` request — the kernel streams the requested file via PTY |

### The probe

Injection is one `printf` typed as its own line:

```
printf '\033]2607;QUA;P;%s\007' "${ZSH_VERSION:-}"
```

`$ZSH_VERSION` is empty under bash, a version string under zsh — the shell
does the substitution, the kernel does the branch. This is valid in sh, bash,
and zsh, so it works at any navigation depth without knowing in advance what
it will land in.

Once the kernel knows the kind, it assembles `_SHARED + _BASH` (bash) or
`_SHARED + _ZSH` (zsh), appends the handshake `printf '\033]2607;QUA;I\007'` as
the final line, joins every physical line with `` ; `` into one command, and
types it (`injection.py`, chunked through `Session.type_plumbing`). One command
means one prompt cycle, so the hooks are never half-wired while later lines are
still typing.

Before that line goes over, a lone `set +H` is typed and run first: `_SHARED`
now includes `__qua_fork`, whose `"$!"` contains a literal `!`, and bash's
interactive history expansion (on by default) fires on it — checked before the
line is even parsed, so it can't be escaped away within the same line. It has
to be disabled by a separate, prior Enter. Harmless no-op under zsh, which
isn't tripped by this case anyway.

### Why `E` exists when OSC 133 already brackets the command

`C` and `D` say *that* a command ran and how it exited, but neither carries
**what** the command was, and the kernel cannot reliably recover it on its own.

It cannot read it off the screen, because the text there is preceded by an
arbitrary user-defined prompt with no marker separating the two.

It cannot use the keystrokes it forwarded either, because for an interactively
typed command the bytes going in are not the command coming out. Recalling a
line from history sends one arrow key, tab completion rewrites the line in
place, and editing moves the cursor around.

So the shell reports the text itself. That text is what minuting records, and
what interceptors match on to decide whether to act.

## Functions

### `__qua_preexec` / `__qua_precmd`

The prompt-cycle hooks, and the only part that genuinely differs between the
two shells.

zsh has first-class `preexec_functions` and `precmd_functions` arrays, and
hands the command text straight to preexec as `$1`.

bash has no preexec hook at all. It fakes one with a `trap … DEBUG`, which
otherwise fires for every command in a pipeline, so `__qua_at_prompt` gates it
to once per prompt cycle. It must also preserve `$?` for whatever else is in
`PROMPT_COMMAND`. The guard skips `__qua_precmd` itself, or pressing Enter on
an empty line — where the only command in the cycle is `PROMPT_COMMAND` —
would minute a phantom command.

Both re-append the `133;B` marker to `PS1` on every prompt if it has gone
missing. Appending once at source time is not enough: prompt themes that
reassign `PS1` from their own precmd — common on exactly the remote hosts this
targets — would strip the marker and silently kill integration.

Neither reports a hostname. The prompt already shows it, and nothing
kernel-side reads it.

The assembled snippet ends with `I`, which is the proof that injection took.

### `__qua_exec`

Runs a command concurrently with the live shell, over `perl`'s `IPC::Open3`:
stdout and stderr are read from two separate pipes with `IO::Select` (a
`Symbol::gensym` handle for stderr; `open3` would otherwise silently merge it),
so a chunk from either stream is tagged with which one it came from and
base64-framed — raw bytes survive the wire intact, no escape-stripping needed,
and no ambiguity about which stream a chunk belongs to. `$|=1` and
`sysread`/`syswrite` (bypassing perl's own stdio buffering) keep chunks
streaming rather than sitting in a buffer until the command ends.

The command's stdin is closed immediately (`close $i`); there is no pty here
(unlike a real terminal), so `isatty()` is false for whatever runs. One
`X;<id>;<rc>` carries the exit status once `waitpid` returns.

Needs only `perl` (with core modules) on the far end — nothing ssh-specific,
it acts on whatever shell the session currently sits in.

### `quacat`

The upload half of file copy: puts the tty into raw mode, emits a `U` request,
reads a fixed-width length header, then reads exactly that many bytes.
Counting the bytes is what makes it binary-exact — there is no in-band
end-of-file marker that the file's own contents could imitate. The
`</dev/tty` redirects are a cheap guard against `x | quacat foo`, which would
otherwise never read a real header back.

The remote → local direction has no injected counterpart any more; bring a
file back with `Session.exec()` (e.g. `exec("base64 remote-file")` and decode
client-side) instead.

### `__qua_fork`

Runs a command against a trio of FIFOs the kernel created, so stdout and
stderr stay separate instead of being merged by the PTY, and writes the exit
status to a fourth. Prints the child pid. Those FIFOs are kernel-local, which
is what keeps `fork()` a local-session feature.

## Constraints worth knowing before editing

**bash 3.2 is the floor**, because that is what ships on macOS. No `PS0`, no
associative arrays, no bracketed paste.

**No encoding or obfuscation** — plain shell source only, no base64 of the
script and no `eval` of an encoded blob (PLAN.md §7). The point is to avoid
tripping antivirus tooling that watches for exactly that shape.

**Size is a correctness concern, not just a speed one.** The script is typed
into a live interactive shell; see `injection.py` for how, and for the
measurements behind it.

**A literal `!` anywhere in the assembled line is dangerous** under bash's
default interactive history expansion, which runs before the line is parsed at
all — quoting doesn't protect against it, and a `set +H` earlier in the *same*
line doesn't either (see the probe section above). Anything added here that
needs one has to go out on its own prior line.
