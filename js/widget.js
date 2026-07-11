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
  const badge = document.createElement("span");
  badge.className = "qua-badge";
  badge.textContent = "full-screen";
  badge.style.display = "none";
  const tools = document.createElement("span");
  tools.className = "qua-tools";
  const size = document.createElement("span");
  size.className = "qua-size";
  bar.appendChild(title);
  bar.appendChild(badge);
  bar.appendChild(tools);
  bar.appendChild(size);

  const body = document.createElement("div");
  body.className = "qua-body";

  root.appendChild(bar);
  root.appendChild(body);
  el.appendChild(root);

  function showFrozen(html) {
    root.classList.add("qua-frozen");
    tools.style.display = "none";
    body.innerHTML = html || "";
    size.textContent = "hopped ⤵";
  }

  if (model.get("frozen")) {
    showFrozen(model.get("frozen_html"));
    return;
  }

  // Toolbar (PLAN.md §6): ⏸ pause and ⌫ erase are always present; the ⌫
  // flashes when a keystroke goes un-echoed or masked (kernel-classified),
  // ⏸ flashes on Enter — prompts to the user, never actions.
  function mkbtn(name, tip, svg) {
    const b = document.createElement("button");
    b.className = "qua-btn qua-btn-" + name;
    b.title = tip;
    b.innerHTML = svg;
    tools.appendChild(b);
    return b;
  }
  // One toggle for the whole recording lifecycle (start / pause / resume);
  // its icon and tint are the sole recording-state indicator.
  const recBtn = mkbtn("rec", "start recording", SVG.record);
  const eraseBtn = mkbtn("erase", "erase previous keystroke(s) from the recording", SVG.erase);
  const camBtn = mkbtn("camera", "screenshot into the notebook", SVG.camera);

  let recState = { started: false, recording: false };
  function renderRec() {
    recBtn.innerHTML = recState.recording ? SVG.pause : SVG.record;
    recBtn.title = !recState.started
      ? "start recording"
      : recState.recording
        ? "pause recording"
        : "resume recording";
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
  const ser = new SerializeAddon();
  term.loadAddon(fit);
  term.loadAddon(ser);
  term.open(body);

  let exited = false;
  let frozen = false;

  term.onData((d) => {
    if (exited || frozen) return;
    // Enter: flash the record toggle so the user confirms the recording state
    // (PLAN.md §6).
    if (recState.recording && d.includes("\r")) flash(recBtn);
    model.send({ type: "stdin", data: d });
  });
  term.onResize(({ cols, rows }) => {
    size.textContent = `${cols}×${rows}`;
    if (!frozen) model.send({ type: "resize", cols, rows });
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

  function freeze() {
    if (frozen) return;
    frozen = true;
    let html = "";
    try {
      html = ser.serializeAsHTML({ includeGlobalBackground: true });
    } catch (e) {
      try {
        const pre = document.createElement("pre");
        pre.textContent = ser.serialize();
        html = pre.outerHTML;
      } catch (e2) {
        html = "";
      }
    }
    model.set("frozen", true);
    model.set("frozen_html", html);
    model.save_changes();
    ro.disconnect();
    term.dispose();
    showFrozen(html);
  }

  model.on("msg:custom", (msg, buffers) => {
    if (frozen) return;
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
    } else if (msg.type === "freeze") {
      freeze();
    } else if (msg.type === "rec-state") {
      recState = { started: !!msg.started, recording: !!msg.recording };
      renderRec();
    } else if (msg.type === "echo") {
      // Un-echoed or masked keystroke while recording: prompt with ⌫.
      flash(eraseBtn);
    } else if (msg.type === "altscreen") {
      badge.style.display = msg.on ? "" : "none";
      camBtn.classList.toggle("qua-hot", !!msg.on);
    }
  });

  model.on("change:session_name", () => {
    title.textContent = model.get("session_name");
  });

  // Initial fit after layout settles, then ask the kernel for scrollback.
  requestAnimationFrame(() => {
    try {
      fit.fit();
    } catch (e) {}
    model.send({ type: "ready" });
  });

  return () => {
    ro.disconnect();
    if (!frozen) term.dispose();
  };
}

export default { render };
