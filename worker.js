// Hueface worker: runs the Python core (hueface.py) with Pyodide, off the main thread.
const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/";
importScripts(PYODIDE_URL + "pyodide.js");

let hf = null;
let fontBytes = null;

const ready = (async () => {
  const py = await loadPyodide({ indexURL: PYODIDE_URL });
  await py.loadPackage("fonttools");
  const src = await (await fetch("hueface.py", { cache: "no-cache" })).text();
  py.FS.writeFile("/home/pyodide/hueface.py", src);
  py.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
  hf = py.pyimport("hueface");
  return py;
})();

function reply(id, payload, transfer) {
  self.postMessage({ id, ...payload }, transfer || []);
}

self.onmessage = async (e) => {
  const { id, cmd } = e.data;
  try {
    await ready;
    if (cmd === "init") {
      reply(id, { ok: true, version: hf.VERSION });
    } else if (cmd === "analyze") {
      fontBytes = e.data.bytes;
      reply(id, JSON.parse(hf.analyze(fontBytes)));
    } else if (cmd === "build") {
      const res = hf.build_report(fontBytes, JSON.stringify(e.data.config));
      const ok = res.get(0);
      if (ok) {
        const buf = res.get(1).getBuffer("u8");
        const font = buf.data.slice();      // copy out of Python memory
        buf.release();
        const info = JSON.parse(res.get(2));
        res.destroy();
        reply(id, { ok: true, font, info }, [font.buffer]);
      } else {
        const error = res.get(1);
        res.destroy();
        reply(id, { ok: false, error });
      }
    }
  } catch (err) {
    reply(id, { ok: false, error: "The font engine failed: " + (err && err.message ? err.message : err) });
  }
};
