import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";

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

  let exited = false;
  term.onData((d) => {
    if (!exited) model.send({ type: "stdin", data: d });
  });
  term.onResize(({ cols, rows }) => {
    size.textContent = `${cols}×${rows}`;
    model.send({ type: "resize", cols, rows });
  });

  model.on("msg:custom", (msg, buffers) => {
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
    }
  });

  model.on("change:session_name", () => {
    title.textContent = model.get("session_name");
  });

  // Fit whenever our box changes (the container is CSS-resizable).
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

  // Initial fit after layout settles, then ask the kernel for scrollback.
  requestAnimationFrame(() => {
    try {
      fit.fit();
    } catch (e) {}
    model.send({ type: "ready" });
  });

  return () => {
    ro.disconnect();
    term.dispose();
  };
}

export default { render };
