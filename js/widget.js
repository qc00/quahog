import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SerializeAddon } from "@xterm/addon-serialize";

const SVG_ATTRS =
  'viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const SVG = {
  // The single recording toggle shows its own state: a filled dot to start or
  // resume, pause bars while actively recording.
  record: `<svg ${SVG_ATTRS}><circle cx="12" cy="12" r="6" fill="currentColor"/></svg>`,
  pause: `<svg ${SVG_ATTRS}><line x1="9" y1="5" x2="9" y2="19"/><line x1="15" y1="5" x2="15" y2="19"/></svg>`,
  erase: `<svg ${SVG_ATTRS}><path d="M20 5H9l-6 7 6 7h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z"/><line x1="17" y1="9" x2="12" y2="14"/><line x1="12" y1="9" x2="17" y2="14"/></svg>`,
  camera: `<svg ${SVG_ATTRS}><path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/></svg>`,
};

function render({ model, el }) {
  const root = document.createElement("div");
  root.className = "qua-console";

  const bar = document.createElement("div");
  bar.className = "qua-bar";
  const title = document.createElement("span");
  title.className = "qua-title";
  title.textContent = model.get("session_name") || "session";
  // What the shell/app calls itself (OSC 0/2 window title), next to the
  // session name we gave it (2026-07-22 old plans.md §2). No icon (OSC 0/1): there's nothing
  // here to iconify/minimize, so an icon name has nowhere to show.
  const osc = document.createElement("span");
  osc.className = "qua-osc-title";
  const badge = document.createElement("span");
  badge.className = "qua-badge";
  badge.textContent = "full-screen";
  badge.style.display = "none";
  const stdinBadge = document.createElement("span");
  stdinBadge.className = "qua-badge qua-stdin-closed";
  stdinBadge.textContent = "Input disabled";
  stdinBadge.style.display = "none";
  const tools = document.createElement("span");
  tools.className = "qua-tools";
  const size = document.createElement("span");
  size.className = "qua-size";
  bar.appendChild(title);
  bar.appendChild(osc);
  bar.appendChild(badge);
  bar.appendChild(stdinBadge);
  bar.appendChild(tools);
  bar.appendChild(size);

  function renderStdin(state) {
    stdinBadge.style.display = state === "closed" ? "" : "none";
  }

  const body = document.createElement("div");
  body.className = "qua-body";

  root.appendChild(bar);
  root.appendChild(body);
  el.appendChild(root);

  // Toolbar (2026-07-22 old plans.md §6): the record toggle and ⌫ erase are always present,
  // each labeled so the icon alone doesn't have to carry the meaning. ⌫
  // flashes when a keystroke goes un-echoed or masked (kernel-classified);
  // the record toggle flashes on Enter — prompts to the user, never actions.
  function mkbtn(name, label, svg) {
    const b = document.createElement("button");
    b.className = "qua-btn qua-btn-" + name;
    b.title = label;
    const icon = document.createElement("span");
    icon.className = "qua-btn-icon";
    icon.innerHTML = svg;
    const text = document.createElement("span");
    text.className = "qua-btn-label";
    text.textContent = label;
    b.appendChild(icon);
    b.appendChild(text);
    tools.appendChild(b);
    return b;
  }
  // One toggle for the whole recording lifecycle (start / pause / resume);
  // its icon, tint and label are the sole recording-state indicator.
  const recBtn = mkbtn("rec", "Record", SVG.record);
  const eraseBtn = mkbtn("erase", "Erase", SVG.erase);
  const camBtn = mkbtn("camera", "Screenshot", SVG.camera);
  const recLabel = recBtn.querySelector(".qua-btn-label");

  let recState = { started: false, recording: false };
  function renderRec() {
    recBtn.querySelector(".qua-btn-icon").innerHTML = recState.recording ? SVG.pause : SVG.record;
    const label = !recState.started ? "Record" : recState.recording ? "Pause" : "Resume";
    recLabel.textContent = label;
    recBtn.title = label + " recording";
    recBtn.classList.toggle("qua-rec-on", recState.recording);
    // ⌫ only means something while recording (it edits the flush tail), so it
    // is genuinely disabled — no hover, no click — otherwise.
    eraseBtn.disabled = !recState.recording;
  }
  renderRec();

  function flash(b) {
    b.classList.remove("qua-flash");
    void b.offsetWidth; // restart the animation
    b.classList.add("qua-flash");
  }

  recBtn.addEventListener("click", () => model.send({ type: "pause" }));
  eraseBtn.addEventListener("click", () => model.send({ type: "erase" }));
  camBtn.addEventListener("click", () => model.send({ type: "screenshot" }));

  const term = new Terminal({
    cursorBlink: true,
    scrollback: 8000,
    fontSize: 13,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    theme: { background: "#16161e", foreground: "#c8ccd4" },
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(body);

  // OSC 0/2 (xterm.js fires onTitleChange for both). Left empty rather than
  // hidden when there's no title: the span is also the bar's spacer, and
  // .qua-osc-title:empty drops its separator so nothing shows.
  term.onTitleChange((t) => {
    osc.textContent = t;
  });

  let exited = false;

  term.onData((d) => {
    if (exited) return;
    // Enter: flash the record toggle so the user confirms the recording state
    // (2026-07-22 old plans.md §6).
    if (recState.recording && d.includes("\r")) flash(recBtn);
    model.send({ type: "stdin", data: d });
  });
  term.onResize(({ cols, rows }) => {
    size.textContent = `${cols}×${rows}`;
    model.send({ type: "resize", cols, rows });
  });

  let raf = 0;
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      try {
        fit.fit();
      } catch (e) {
        /* detached */
      }
    });
  });
  ro.observe(body);

  function onCustomMessage(msg, buffers) {
    if (msg.type === "out" && buffers && buffers.length) {
      const b = buffers[0];
      const u8 =
        b instanceof DataView
          ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength)
          : new Uint8Array(b.buffer || b);
      term.write(u8);
    } else if (msg.type === "exited") {
      exited = true;
      const code = msg.code === null || msg.code === undefined ? "?" : msg.code;
      term.write(`\r\n\x1b[2m[session exited: ${code}]\x1b[0m\r\n`);
    } else if (msg.type === "rec-state") {
      recState = { started: !!msg.started, recording: !!msg.recording };
      renderRec();
    } else if (msg.type === "echo") {
      // Un-echoed or masked keystroke while recording: prompt with ⌫.
      flash(eraseBtn);
    } else if (msg.type === "stdin-state") {
      renderStdin(msg.state);
    } else if (msg.type === "altscreen") {
      badge.style.display = msg.on ? "" : "none";
      camBtn.classList.toggle("qua-hot", !!msg.on);
    }
  }
  model.on("msg:custom", onCustomMessage);

  function onSessionNameChange() {
    title.textContent = model.get("session_name");
  }
  model.on("change:session_name", onSessionNameChange);

  // Initial fit after layout settles, then ask the kernel for scrollback.
  requestAnimationFrame(() => {
    try {
      fit.fit();
    } catch (e) {}
    model.send({ type: "ready" });
  });

  return () => {
    // Without this, a render() invoked again for the same model (e.g. a
    // reconnect/restart edge case where the previous view wasn't disposed
    // first) leaves this listener alive alongside the new one: every future
    // message -- including terminal-query output -- then gets processed
    // twice, independently, by two Terminal instances that each auto-reply
    // on the app's behalf (the mechanism behind 2026-07-22 old plans.md §6's dedup, from a
    // different angle than a single view retrying).
    model.off("msg:custom", onCustomMessage);
    model.off("change:session_name", onSessionNameChange);
    ro.disconnect();
    term.dispose();
  };
}

export default { render };
