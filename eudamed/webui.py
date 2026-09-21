"""A small local web UI for searching EUDAMED by name.

    python3 -m eudamed serve --key YOUR_KEY

Then open http://127.0.0.1:8100 and type any device name.

The subscription key stays on the server side: the browser calls this local
process, which calls the EUDAMED API. The key is never sent to the page, which
also avoids the browser CORS restrictions that would block calling the API
directly from JavaScript.
"""

import json
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .client import ApiError, AuthError
from .matching import FOUND, POSSIBLE
from .records import Actor
from .reference import Reference
from .search import Target, search_target


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, client, verbose=False, targets=None):
        super().__init__(address, handler)
        self.client = client
        self.verbose = verbose
        # Optional device list loaded from a CSV, exposed to the page so a name
        # can be picked instead of typed, and the whole list run in one go.
        self.targets = list(targets or ())
        self.targets_by_name = {t.name.lower(): t for t in self.targets}
        self._ref = None
        self._ref_lock = threading.Lock()

    def reference(self):
        """Load the reference table once, on first use."""
        with self._ref_lock:
            if self._ref is None:
                self._ref = Reference(self.client, verbose=self.verbose).load()
            return self._ref


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "eudamed-webui"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _send(self, status, body, ctype):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # Only this process may be called from the page; no external origins.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, default=str), "application/json; charset=utf-8")

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif parsed.path == "/favicon.svg":
                self._send(200, FAVICON, "image/svg+xml")
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif parsed.path == "/api/health":
                base = self.server.client.base
                self._json(200, {
                    "base": base,
                    "has_key": bool(self.server.client.key),
                    "format": self.server.client.fmt,
                    "thresholds": {"found": FOUND, "possible": POSSIBLE},
                    "udi_params": list(config.UDI_PARAMS),
                    # A local base means the bundled stand-in, not real EUDAMED.
                    "is_local": _is_local(base),
                    "device_count": len(self.server.targets),
                })
            elif parsed.path == "/api/devices":
                self._json(200, {"devices": [t.to_dict() for t in self.server.targets]})
            elif parsed.path == "/api/search":
                self._search(query)
            elif parsed.path == "/api/actors":
                self._actors(query)
            else:
                self._json(404, {"error": f"no such path: {parsed.path}"})
        except BrokenPipeError:
            pass
        except Exception:                                   # noqa: BLE001
            # A handler thread must never die silently; report it to the page.
            if self.server.verbose:
                traceback.print_exc()
            self._json(500, {"error": "internal error", "detail": traceback.format_exc(limit=3)})

    def _search(self, query):
        # ?target=<name> uses that device's full definition from the loaded CSV
        # (all spelling variants, broad terms and expected country) rather than
        # treating the typed text as the only search key.
        wanted = (query.get("target") or "").strip()
        preset = self.server.targets_by_name.get(wanted.lower()) if wanted else None
        if wanted and preset is None:
            self._json(404, {"error": f"{wanted!r} is not in the loaded device list"})
            return

        name = (query.get("name") or "").strip() or (preset.name if preset else "")
        if not name:
            self._json(400, {"error": "give a name to search for"})
            return

        fields = (query.get("fields") or "TRADE_NAME").upper()
        chosen = [f.strip() for f in fields.split(",") if f.strip()]
        bad = [f for f in chosen if f not in config.UDI_PARAMS]
        if bad or not chosen:
            named = ", ".join(bad) or fields
            self._json(400, {"error": f"not documented /udi parameter(s): {named}"})
            return

        if preset is not None:
            target = Target(name=preset.name, description=preset.description,
                            ca=preset.ca,
                            country=(query.get("country") or preset.country).strip(),
                            keys=preset.keys, broad=preset.broad)
        else:
            target = Target(name=name, country=(query.get("country") or "").strip(),
                            keys=[name])
        reference = self.server.reference() if query.get("codes", "1") != "0" else None
        try:
            result = search_target(self.server.client, target, reference=reference,
                                   top=int(query.get("top") or 10),
                                   min_score=float(query.get("min_score") or 0.3),
                                   fields=",".join(chosen))
        except AuthError as exc:
            self._json(401, {"error": "auth", "detail": str(exc)})
            return
        except (ApiError, ValueError) as exc:
            self._json(502, {"error": "api", "detail": str(exc)})
            return
        # Only warn when a candidate in THIS result actually carries an
        # unresolved code, not merely because the reference table has some
        # ambiguous id somewhere.
        if reference is not None and reference.ambiguous and _has_unresolved_codes(result):
            result["reference_ambiguous"] = {str(k): v for k, v in reference.ambiguous.items()}
        self._json(200, result)

    def _actors(self, query):
        name = (query.get("name") or "").strip()
        if not name:
            self._json(400, {"error": "give a name to search for"})
            return
        try:
            rows, _ = self.server.client.actors(NAME=name)
        except AuthError as exc:
            self._json(401, {"error": "auth", "detail": str(exc)})
            return
        except (ApiError, ValueError) as exc:
            self._json(502, {"error": "api", "detail": str(exc)})
            return
        self._json(200, {"actors": [Actor(r).to_dict() for r in rows]})


def _is_local(base):
    host = urllib.parse.urlparse(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


CODED_FIELDS = ("risk_class", "legislation", "market_status", "special_type")


def _has_unresolved_codes(result):
    """True when a coded field still shows a bare numeric id.

    A resolved code reads like CLASS_IIA or MDR; an unresolved one is the raw
    id the API returned, e.g. "2".
    """
    for candidate in result.get("candidates", ()):
        for field in CODED_FIELDS:
            value = str(candidate.get(field) or "")
            if value and value.isdigit():
                return True
    return False


def serve(client, port=8100, host="127.0.0.1", verbose=False, targets=None):
    return UIServer((host, port), Handler, client, verbose=verbose, targets=targets)


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#1f4f8a"/>'
    '<circle cx="14" cy="14" r="6.5" fill="none" stroke="#fff" stroke-width="2.6"/>'
    '<path d="M19 19l6 6" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>'
    "</svg>"
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EUDAMED Search</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--ink:#17202b;--muted:#5d6b7a;--line:#d9dee4;--bg:#fff;--band:#f2f5f8;--card:#fff;
--found:#1d6b45;--possible:#9a5b00;--none:#8a94a0;--link:#1f4f8a;--warn:#8a3a2a;--chip:#eef2f6;
--accent:#1f4f8a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ink:#e6ebf0;--muted:#9aa7b4;
--line:#2e3742;--bg:#121820;--band:#1a222c;--card:#18202a;--found:#5cc48f;--possible:#e3a64a;
--none:#7d8894;--link:#8ab8f0;--warn:#f0a090;--chip:#232d38;--accent:#8ab8f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 16px 72px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,select,button{font:inherit;padding:9px 11px;border:1px solid var(--line);
border-radius:7px;background:var(--card);color:var(--ink)}
#name{flex:1;min-width:220px;font-size:16px}
button{background:var(--accent);color:#fff;border-color:transparent;cursor:pointer;font-weight:600;
padding-inline:18px}
button:disabled{opacity:.55;cursor:default}
details.opts{margin:0 0 18px;font-size:13px;color:var(--muted)}
details.opts summary{cursor:pointer}
.optrow{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:10px}
.optrow label{display:flex;gap:5px;align-items:center;color:var(--ink)}
#status{margin:0 0 16px;font-size:14px;min-height:1.4em}
#status.err{color:var(--warn)}
.banner{background:var(--band);border-left:3px solid var(--warn);padding:10px 14px;
border-radius:0 6px 6px 0;margin:0 0 16px;font-size:13px}
.verdict{font-size:19px;font-weight:600;margin:0 0 4px}
.verdict.found{color:var(--found)}.verdict.possible{color:var(--possible)}.verdict.none{color:var(--none)}
.verdict.error{color:var(--warn)}
.errbox{background:var(--band);border-left:3px solid var(--warn);padding:11px 14px;
border-radius:0 6px 6px 0;margin:0 0 14px;font-size:13px}
.errbox strong{color:var(--warn)}
.errbox ul{margin:7px 0 0;padding-left:18px}
.errbox li{margin:3px 0;word-break:break-word}
.card{border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin:0 0 12px;
background:var(--card)}
.card h3{margin:0;font-size:16px;display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.chip{display:inline-block;background:var(--chip);color:var(--muted);border-radius:10px;
padding:1px 8px;font-size:11px;white-space:nowrap;font-weight:600}
.chip.mfr{color:var(--warn)}
.chip.score{color:var(--ink)}
.warnbox{border-left:3px solid var(--warn);padding:7px 12px;margin:10px 0 0;font-size:13px;
color:var(--warn)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:11px 0 0;font-size:13px}
dt{color:var(--muted)}dd{margin:0;word-break:break-word}
a{color:var(--link)}
.meta{color:var(--muted);font-size:12px;margin-top:14px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.demo{background:#fff4e5;border-left:3px solid var(--possible);color:#7a4a00;
padding:10px 14px;border-radius:0 6px 6px 0;margin:0 0 16px;font-size:13px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .demo{background:#2a2113;color:#e3b872}}
.mylist{margin:0 0 20px}
.mylist h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
margin:0 0 9px;font-weight:600}
.names{display:flex;flex-wrap:wrap;gap:6px}
.name-btn{background:var(--chip);color:var(--ink);border:1px solid var(--line);border-radius:14px;
padding:4px 11px;font-size:13px;cursor:pointer;font-weight:400}
.name-btn:hover{border-color:var(--accent);color:var(--accent)}
.name-btn.active{background:var(--accent);color:#fff;border-color:transparent}
.batchbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:11px}
.bar{flex:1;min-width:140px;height:6px;background:var(--chip);border-radius:3px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .2s}
table.sum{width:100%;border-collapse:collapse;margin-top:6px;font-size:14px}
table.sum th,table.sum td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line)}
table.sum th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
table.sum tr.row{cursor:pointer}
table.sum tr.row:hover{background:var(--band)}
.st.found{color:var(--found)}.st.possible{color:var(--possible)}.st.none{color:var(--none)}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--muted);
border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;vertical-align:-1px}
@keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<main>
<h1>EUDAMED Search</h1>
<p class="sub" id="sub">connecting&hellip;</p>
<div id="demo"></div>

<form id="f">
  <input id="name" type="search" placeholder="Device trade name, e.g. MindDoc"
         autocomplete="off" list="names" autofocus aria-label="Device name">
  <datalist id="names"></datalist>
  <select id="country" aria-label="Expected manufacturer country">
    <option value="">Any country</option>
    <option value="DE">DE</option><option value="NL">NL</option><option value="CZ">CZ</option>
    <option value="AT">AT</option><option value="FR">FR</option><option value="IT">IT</option>
    <option value="ES">ES</option><option value="BE">BE</option><option value="DK">DK</option>
    <option value="SE">SE</option><option value="PL">PL</option><option value="IE">IE</option>
  </select>
  <button id="go" type="submit">Search</button>
</form>

<details class="opts">
  <summary>Options</summary>
  <div class="optrow">
    <span>Search fields:</span>
    <label><input type="checkbox" class="fld" value="TRADE_NAME" checked> TRADE_NAME</label>
    <label><input type="checkbox" class="fld" value="DEVICE_NAME"> DEVICE_NAME</label>
    <label><input type="checkbox" class="fld" value="BASIC_UDI"> BASIC_UDI</label>
    <label><input type="checkbox" class="fld" value="PRIMARY_DI"> PRIMARY_DI</label>
    <label><input type="checkbox" class="fld" value="MF_SRN"> MF_SRN</label>
  </div>
  <div class="optrow">
    <label>Min score <input id="min" type="number" min="0" max="1" step="0.05" value="0.3"
      style="width:5.5em"></label>
    <label>Max results <input id="top" type="number" min="1" max="50" value="10"
      style="width:5em"></label>
    <label><input id="codes" type="checkbox" checked> Resolve numeric codes via /reference</label>
  </div>
</details>

<section class="mylist" id="mylist" hidden>
  <h2>My list <span id="listcount" class="chip"></span></h2>
  <div class="names" id="namebtns"></div>
  <div class="batchbar">
    <button id="runall" type="button">Run all</button>
    <div class="bar"><i id="prog"></i></div>
    <span class="sub" id="progtxt"></span>
  </div>
  <div id="summary"></div>
</section>

<div id="status"></div>
<div id="out"></div>
</main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const $ = id => document.getElementById(id);
const FIELDS = [["Trade name","trade_name"],["Device name","device_name"],["Model","device_model"],
["Manufacturer","manufacturer_name"],["Manufacturer SRN","mf_srn"],["Country","manufacturer_country"],
["Risk class","risk_class"],["Legislation","legislation"],["Market status","market_status"],
["Special type","special_type"],["UDI-DI","primary_di"],["Basic UDI-DI","basic_udi"],
["EMDN / nomenclature","nomenclature_code"],["Medical purpose","medical_purpose"],
["Reference","reference"],["Version","version"]];
let TH = {found:0.85, possible:0.6};

let DEVICES = [];

fetch("/api/health").then(r => r.json()).then(h => {
  TH = h.thresholds || TH;
  $("sub").innerHTML = `Querying <code>${esc(h.base)}</code>`
    + (h.has_key ? "" : ' &middot; <strong>no subscription key configured</strong>');
  if (h.is_local) {
    $("demo").className = "demo";
    $("demo").innerHTML = `<strong>Demo mode.</strong> This is the bundled local stand-in, which
      contains only 4 fixture devices: <code>MindDoc</code>, <code>Moodpath</code>,
      <code>Kalmeda</code> and <code>Vitadio</code>. Any other name will correctly come back
      <em>not found</em> &mdash; it is not in the fixture. For real answers, restart without
      <code>--base</code> and with a real subscription key.`;
  }
}).catch(() => { $("sub").textContent = "cannot reach the local server"; });

fetch("/api/devices").then(r => r.json()).then(d => {
  DEVICES = d.devices || [];
  if (!DEVICES.length) return;
  $("mylist").hidden = false;
  $("listcount").textContent = DEVICES.length;
  $("names").innerHTML = DEVICES.map(x => `<option value="${esc(x.name)}">`).join("");
  $("namebtns").innerHTML = DEVICES.map((x, i) =>
    `<button type="button" class="name-btn" data-i="${i}" title="${esc(x.description || "")}">${esc(x.name)}</button>`).join("");
  $("namebtns").addEventListener("click", e => {
    const btn = e.target.closest(".name-btn");
    if (btn) runOne(DEVICES[+btn.dataset.i].name, btn);
  });
}).catch(() => {});

const cls = s => s >= TH.found ? "found" : s >= TH.possible ? "possible" : "none";
const word = s => s >= TH.found ? "found" : s >= TH.possible ? "possible" : "not found";
const statusCls = s => s === "found" ? "found" : s === "possible" ? "possible"
  : s === "error" ? "error" : "none";

/* Turn a raw request failure into something actionable. */
function explain(errors) {
  const all = (errors || []).join(" ");
  if (/10061|Connection refused|actively refused/i.test(all))
    return `Nothing is listening at the address being queried. If you are using the bundled
      stand-in, start it in another terminal with <code>python -m eudamed.fakeserver</code>
      and search again.`;
  if (/getaddrinfo|Name or service not known|nodename nor servname|11001/i.test(all))
    return `The API host name could not be resolved. Check the <code>--base</code> URL and your
      network or DNS.`;
  if (/timed out|timeout/i.test(all))
    return `The API did not respond in time. Try again, or raise <code>--timeout</code>.`;
  if (/\b40[13]\b|subscription key/i.test(all))
    return `The subscription key was rejected. Restart with a valid <code>--key</code>, or try
      <code>--auth-mode query</code>.`;
  if (/\b404\b/i.test(all))
    return `The endpoint was not found. Check that <code>--base</code> ends in
      <code>/eudamed</code>.`;
  return `The request did not complete, so nothing was checked.`;
}

function card(c, searched) {
  const isMfr = c.matched_on === "manufacturer";
  const rows = FIELDS.filter(([, k]) => c[k] !== undefined && c[k] !== "" && c[k] !== null)
    .map(([l, k]) => `<dt>${l}</dt><dd>${esc(c[k])}</dd>`).join("");
  const title = esc(c.trade_name || c.device_name || "(unnamed)");
  const warn = isMfr
    ? `<div class="warnbox">Manufacturer-name match only &mdash; the trade name does not match
       "${esc(searched)}". Scored below the match threshold on purpose; this is a lead, not a
       match.</div>` : "";
  return `<div class="card"><h3>${c.link
      ? `<a href="${esc(c.link)}" target="_blank" rel="noopener">${title}</a>` : title}
    <span class="chip score">${c.score}</span>
    <span class="chip${isMfr ? " mfr" : ""}">${esc(c.matched_on)}</span></h3>
    ${warn}<dl>${rows}</dl></div>`;
}

function render(d) {
  const best = d.candidates[0];
  const isError = d.status === "error";
  let html = `<p class="verdict ${isError ? "error" : best ? cls(best.score) : "none"}">`
    + `${isError ? "could not check" : best ? word(best.score) : "not found"}</p>`;

  if (isError) {
    // Every request failed, so registration is unknown - not absent.
    html += `<div class="errbox"><strong>This is not a "not found".</strong> Every request
      failed, so the register was never consulted and this device's registration is
      <em>unknown</em>. ${explain(d.errors)}
      <ul>${(d.errors || []).map(e => `<li><code>${esc(e)}</code></li>`).join("")}</ul></div>`;
  } else if (!d.candidates.length) {
    html += `<p class="sub">No candidate scored above the minimum. Try enabling DEVICE_NAME
      under Options, lowering the minimum score, or a different spelling.</p>`;
  }
  if (d.reference_ambiguous) {
    html += `<div class="banner">Some numeric codes could not be resolved: /reference reuses ids
      across code tables and has no column saying which table an id belongs to, so the raw id is
      shown rather than a possibly wrong label.</div>`;
  }
  html += d.candidates.map(c => card(c, d.name)).join("");
  const qs = (d.queries || []).map(q =>
    `<code>${esc(q.param)}=${esc(q.term)}</code> ${q.error ? "error" : q.rows + " row(s)"}`).join(", ");
  html += `<p class="meta">${d.total_matches} row(s) returned. Queries: ${qs}.
    Scores rank candidates; they do not confirm registration &mdash; open the EUDAMED link to
    verify.</p>`;
  if (!isError && (d.errors || []).length)
    html += `<div class="warnbox">${d.errors.map(esc).join("<br>")}</div>`;
  $("out").innerHTML = html;
}

function currentOpts() {
  const fields = [...document.querySelectorAll(".fld:checked")].map(c => c.value);
  return { fields, top: $("top").value, min: $("min").value,
           codes: $("codes").checked ? "1" : "0" };
}

async function query({ name, target }) {
  const o = currentOpts();
  const params = { fields: o.fields.join(","), top: o.top, min_score: o.min, codes: o.codes };
  if (target) params.target = target; else params.country = $("country").value;
  if (name) params.name = name;
  const r = await fetch("/api/search?" + new URLSearchParams(params));
  const d = await r.json();
  if (!r.ok) throw Object.assign(new Error(d.detail || d.error || "request failed"), { payload: d });
  return d;
}

/* One device from the loaded list, using its full definition. */
async function runOne(name, btn) {
  document.querySelectorAll(".name-btn.active").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  $("name").value = name;
  $("summary").innerHTML = "";
  $("status").className = "";
  $("status").innerHTML = `<span class="spin"></span> searching ${esc(name)}&hellip;`;
  $("out").innerHTML = "";
  try {
    const d = await query({ target: name });
    $("status").textContent = "";
    render(d);
  } catch (err) {
    $("status").className = "err";
    $("status").textContent = err.message;
  }
}

/* The whole loaded list, one request set per device, with progress. */
const BATCH = [];
$("runall")?.addEventListener("click", async () => {
  const btn = $("runall");
  btn.disabled = true;
  BATCH.length = 0;
  $("out").innerHTML = "";
  $("status").textContent = "";
  for (let i = 0; i < DEVICES.length; i++) {
    const dev = DEVICES[i];
    $("progtxt").textContent = `${i + 1} / ${DEVICES.length} \u00b7 ${dev.name}`;
    $("prog").style.width = ((i + 1) / DEVICES.length * 100) + "%";
    try {
      BATCH.push(await query({ target: dev.name }));
    } catch (err) {
      BATCH.push({ name: dev.name, status: "not found", candidates: [], queries: [],
                   errors: [err.message], total_matches: 0 });
    }
    renderSummary();
  }
  $("progtxt").textContent = `done \u00b7 ${DEVICES.length} device(s)`;
  btn.disabled = false;
});

function renderSummary() {
  const tally = s => BATCH.filter(d => d.status === s).length;
  const rows = BATCH.map((d, i) => {
    const b = d.candidates[0] && d.status !== "not found" ? d.candidates[0] : {};
    return `<tr class="row" data-b="${i}"><td>${esc(d.name)}</td>
      <td class="st ${statusCls(d.status)}">${esc(d.status)}</td>
      <td>${esc(b.trade_name || "")}</td>
      <td>${esc(b.manufacturer_name || "")}</td>
      <td>${b.matched_on ? `<span class="chip${b.matched_on === "manufacturer" ? " mfr" : ""}">${esc(b.matched_on)}</span>` : ""}</td></tr>`;
  }).join("");
  const nErr = tally("error");
  const head = `<p class="sub" style="margin:14px 0 0">`
    + `<strong>${tally("found")}</strong> found &middot; `
    + `<strong>${tally("possible")}</strong> possible &middot; `
    + `<strong>${tally("not found")}</strong> not found`
    + (nErr ? ` &middot; <strong class="st error">${nErr}</strong> could not be checked` : "")
    + ` &middot; click a row for detail</p>`;
  const table = `<table class="sum"><thead><tr><th>Device</th><th>Status</th>`
    + `<th>Best match</th><th>Manufacturer</th><th>Evidence</th></tr></thead>`
    + `<tbody>${rows}</tbody></table>`;
  $("summary").innerHTML = head + table;
  $("summary").querySelectorAll("tr.row").forEach(tr => tr.addEventListener("click", () => {
    const d = BATCH[+tr.dataset.b];
    $("name").value = d.name;
    render(d);
    $("out").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

$("f").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("name").value.trim();
  if (!name) return;
  document.querySelectorAll(".name-btn.active").forEach(b => b.classList.remove("active"));
  $("summary").innerHTML = "";
  const fields = [...document.querySelectorAll(".fld:checked")].map(c => c.value);
  if (!fields.length) {
    $("status").className = "err";
    $("status").textContent = "Select at least one search field under Options.";
    return;
  }
  $("go").disabled = true;
  $("status").className = "";
  $("status").innerHTML = `<span class="spin"></span> searching ${esc(name)}&hellip;`;
  $("out").innerHTML = "";
  // If the typed name is in the loaded list, use its full definition.
  const known = DEVICES.find(x => x.name.toLowerCase() === name.toLowerCase());
  try {
    const d = await query(known ? { target: known.name } : { name });
    $("status").textContent = "";
    render(d);
  } catch (err) {
    const p = err.payload || {};
    $("status").className = "err";
    $("status").innerHTML = p.error === "auth"
      ? `Subscription key rejected. Restart with a valid <code>--key</code>.<br>
         <span class="sub">${esc(p.detail || "")}</span>`
      : `${esc(err.message)}`;
  } finally {
    $("go").disabled = false;
  }
});
</script>
</body>
</html>
"""
