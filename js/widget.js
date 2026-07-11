import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SerializeAddon } from "@xterm/addon-serialize";

function render({ model, el }) {
  const root = document.createElement("div");
  root.className = "qua-console";

  const bar = document.createElement("div");
  bar.className = "qua-bar";
  const title = document.createElement("span");
  title.className = "qua-title";
  title.textContent = model.get("session_name") || "session";
  const size = document.createElement("span");
  size.className = "qua-size";
  bar.appendChild(title);
  bar.appendChild(size);

  const body = document.createElement("div");
  body.className = "qua-body";

  root.appendChild(bar);
  root.appendChild(body);
  el.appendChild(root);

  function showFrozen(html) {
    root.classList.add("qua-frozen");
    body.innerHTML = html || "";
    size.textContent = "hopped ⤵";
  }

  if (model.get("frozen")) {
    showFrozen(model.get("frozen_html"));
    return;
  }

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
    if (!exited && !frozen) model.send({ type: "stdin", data: d });
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
