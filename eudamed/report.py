"""Write results as JSON, CSV and a self-contained HTML report."""

import csv
import json
import os
import re
import time

CSV_FIELDS = [
    "name", "ca", "expected_country", "status", "score", "matched_on",
    "trade_name", "device_name", "manufacturer_name", "mf_srn", "manufacturer_country",
    "risk_class", "legislation", "device_status", "placed_on_market", "special_type",
    "primary_di", "basic_udi", "nomenclature_code", "medical_purpose",
    "candidates", "total_matches", "errors", "link",
]


def write_json(results, path, meta=None):
    payload = {"meta": meta or {}, "results": results}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            # Only promote a candidate to the flat row when it is an actual
            # match; a sub-threshold lead must not read as the answer.
            best = result["candidates"][0] if (
                result["candidates"]
                and result["status"] not in ("not found", "error")) else {}
            writer.writerow({
                **best,
                "name": result["name"],
                "ca": result.get("ca", ""),
                "expected_country": result.get("country", ""),
                "status": result["status"],
                "candidates": len(result["candidates"]),
                "total_matches": result.get("total_matches", 0),
                "errors": " | ".join(result.get("errors", [])),
            })


def safe_json(obj):
    """JSON safe to embed in a <script> block."""
    # U+2028/U+2029 are valid in JSON strings but are statement terminators
    # in JavaScript, so they must be escaped rather than embedded literally.
    return (json.dumps(obj, ensure_ascii=False, default=str)
            .replace("</", "<\\/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def write_html(results, path, meta=None):
    payload = {"DATA": safe_json(results), "META": safe_json(meta or {})}
    # One pass, so data containing a placeholder token cannot corrupt the next
    # substitution.
    html = re.sub(r"__(DATA|META)__", lambda m: payload[m.group(1)], TEMPLATE)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)


MD_SECTIONS = [
    ("Identification", [
        ("Trade name", "trade_name"), ("Device name", "device_name"),
        ("Model", "device_model"), ("Reference", "reference"),
        ("UDI-DI (primary)", "primary_di"), ("Secondary DI", "secondary_di"),
        ("Basic UDI-DI", "basic_udi"), ("EUDAMED UUID", "uuid"),
    ]),
    ("Classification", [
        ("Risk class", "risk_class"), ("Applicable legislation", "legislation"),
        ("Special device type", "special_type"),
        ("EMDN / nomenclature code", "nomenclature_code"),
        ("Intended medical purpose", "medical_purpose"),
    ]),
    ("Status and market", [
        ("Device status", "device_status"), ("Placed on the market in", "placed_on_market"),
        ("Active", "active"), ("Latest version", "latest_version"), ("Version", "version"),
    ]),
    ("Economic operators", [
        ("Manufacturer", "manufacturer_name"), ("Manufacturer SRN", "mf_srn"),
        ("Manufacturer country", "manufacturer_country"),
        ("Authorised representative", "authorised_rep"),
        ("Authorised rep. SRN", "authorised_rep_srn"),
    ]),
    ("Match provenance", [
        ("Score", "score"), ("Evidence", "matched_on"), ("EUDAMED link", "link"),
    ]),
]


def _md_escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_markdown(results, path, meta=None):
    """A detailed per-device report, readable as-is and printable to PDF."""
    meta = meta or {}
    out = []
    add = out.append

    add("# EUDAMED device report")
    add("")
    add(f"- Generated: {meta.get('generated', '')}")
    add(f"- Source: `{meta.get('base', '')}`")
    if meta.get("fields"):
        add(f"- Searched on: `{meta['fields']}`")
    add(f"- Devices in this report: {len(results)}")
    add(f"- Requests issued: {meta.get('requests', '?')}")
    add("")
    add("> Scores rank candidates; they do not confirm registration. Verify every")
    add("> match through its EUDAMED link before relying on it.")
    add("")

    tally = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    add("## Summary")
    add("")
    add("| Device | Status | Best match | Manufacturer | Risk class | Evidence |")
    add("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        best = r["candidates"][0] if (
            r["candidates"] and r["status"] not in ("not found", "error")) else {}
        add("| {} | {} | {} | {} | {} | {} |".format(
            _md_escape(r["name"]), r["status"],
            _md_escape(best.get("trade_name", "\u2014")),
            _md_escape(best.get("manufacturer_name", "\u2014")),
            _md_escape(best.get("risk_class", "\u2014")),
            _md_escape(best.get("matched_on", "\u2014"))))
    add("")
    add("Totals: " + ", ".join(f"**{n}** {k}" for k, n in sorted(tally.items())))
    add("")

    for r in results:
        add("---")
        add("")
        add(f"## {r['name']}")
        add("")
        if r.get("description"):
            add(f"*{r['description']}*")
            add("")
        facts = []
        if r.get("ca"):
            facts.append(f"Competent authority as listed: {r['ca']}")
        if r.get("country"):
            facts.append(f"Expected manufacturer country: {r['country']}")
        facts.append(f"Status: **{r['status']}**")
        facts.append(f"Rows returned by the API: {r.get('total_matches', 0)}")
        for f in facts:
            add(f"- {f}")
        add("")

        if r["status"] == "error":
            add("> **Not checked.** Every request for this device failed, so its")
            add("> registration is unknown rather than absent.")
            add("")
            for err in r.get("errors", []):
                add(f"- `{err}`")
            add("")
            continue

        if not r["candidates"]:
            add("No candidate scored above the minimum. This is not proof of absence:")
            add("the device may be registered under a different trade name, or the")
            add("filter may not match the way the term was typed.")
            add("")
        for n, cand in enumerate(r["candidates"], start=1):
            label = cand.get("trade_name") or cand.get("device_name") or "(unnamed)"
            add(f"### Candidate {n}: {label}")
            add("")
            if cand.get("matched_on") == "manufacturer":
                add("> **Manufacturer-name match only.** The trade name does not match")
                add("> the device searched for. Scored below the match threshold; treat")
                add("> this as a lead, not a match.")
                add("")
            for section, fields in MD_SECTIONS:
                rows = [(lbl, cand.get(key)) for lbl, key in fields
                        if cand.get(key) not in (None, "", [])]
                if not rows:
                    continue
                add(f"**{section}**")
                add("")
                add("| Field | Value |")
                add("| --- | --- |")
                for lbl, val in rows:
                    add(f"| {lbl} | {_md_escape(val)} |")
                add("")
            extra = {k: v for k, v in cand.items()
                     if k not in {key for _, fs in MD_SECTIONS for _, key in fs}
                     and k != "raw" and v not in (None, "", [])}
            if extra:
                add("**Other reported fields**")
                add("")
                add("| Field | Value |")
                add("| --- | --- |")
                for k in sorted(extra):
                    add(f"| {k} | {_md_escape(extra[k])} |")
                add("")

        queries = r.get("queries") or []
        if queries:
            add("**Queries issued**")
            add("")
            add("| Parameter | Term | Result |")
            add("| --- | --- | --- |")
            for q in queries:
                outcome = "error" if q.get("error") else f"{q.get('rows', 0)} row(s)"
                add(f"| `{q.get('param', '')}` | {_md_escape(q.get('term', ''))} | {outcome} |")
            add("")
        if r.get("errors"):
            add("**Errors**")
            add("")
            for err in r["errors"]:
                add(f"- `{err}`")
            add("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")


def write_all(results, outdir, meta=None):
    os.makedirs(outdir, exist_ok=True)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M"), **(meta or {})}
    paths = {
        "json": os.path.join(outdir, "results.json"),
        "csv": os.path.join(outdir, "results.csv"),
        "html": os.path.join(outdir, "report.html"),
        "md": os.path.join(outdir, "report.md"),
    }
    write_json(results, paths["json"], meta)
    write_csv(results, paths["csv"])
    write_html(results, paths["html"], meta)
    write_markdown(results, paths["md"], meta)
    return paths


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EUDAMED Lookup</title>
<style>
:root{--ink:#17202b;--muted:#5d6b7a;--line:#d9dee4;--bg:#fff;--band:#f2f5f8;--card:#fff;
--found:#1d6b45;--possible:#9a5b00;--none:#8a94a0;--link:#1f4f8a;--warn:#8a3a2a;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ink:#e6ebf0;--muted:#9aa7b4;
--line:#2e3742;--bg:#121820;--band:#1a222c;--card:#18202a;--found:#5cc48f;--possible:#e3a64a;
--none:#7d8894;--link:#8ab8f0;--warn:#f0a090;--chip:#232d38}}
:root[data-theme="dark"]{--ink:#e6ebf0;--muted:#9aa7b4;--line:#2e3742;--bg:#121820;--band:#1a222c;
--card:#18202a;--found:#5cc48f;--possible:#e3a64a;--none:#7d8894;--link:#8ab8f0;--warn:#f0a090;--chip:#232d38}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:32px 16px 72px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 20px;font-size:14px}
.tally{display:flex;gap:26px;margin:0 0 20px;flex-wrap:wrap}
.tally b{font-size:28px;display:block;line-height:1.1}
.tally span{color:var(--muted);font-size:13px}
.note{background:var(--band);border-left:3px solid var(--possible);padding:10px 14px;
border-radius:0 6px 6px 0;margin:0 0 20px;font-size:13px;color:var(--muted)}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
input,select{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--ink)}
input{flex:1;min-width:210px}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;min-width:720px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
tr.dev{cursor:pointer}
tr.dev:hover{background:var(--band)}
tr.dev:focus-visible{outline:2px solid var(--link);outline-offset:-2px}
.st{font-weight:600}
.st.found{color:var(--found)}.st.possible{color:var(--possible)}.st.none{color:var(--none)}
.st.error{color:var(--warn)}
.muted{color:var(--muted);font-size:13px}
.chip{display:inline-block;background:var(--chip);color:var(--muted);border-radius:10px;
padding:1px 8px;font-size:11px;white-space:nowrap}
.chip.mfr{color:var(--warn);font-weight:600}
tr.more td{background:var(--band);padding:14px 16px}
.cand{padding:12px 0;border-top:1px solid var(--line)}
.cand:first-child{border-top:0;padding-top:2px}
.cand dl{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:8px 0 0;font-size:13px}
.cand dt{color:var(--muted)}.cand dd{margin:0;word-break:break-word}
.warnbox{border-left:3px solid var(--warn);padding:6px 12px;margin:8px 0 0;font-size:13px;color:var(--warn)}
a{color:var(--link)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
@media(max-width:600px){main{padding:20px 16px 56px}h1{font-size:22px}}
</style>
</head>
<body>
<main>
<h1>EUDAMED Lookup</h1>
<p class="sub" id="sub"></p>
<div class="tally" id="tally"></div>
<div class="note" id="note"></div>
<div class="controls">
<input id="q" type="search" placeholder="Filter by name, manufacturer, SRN or UDI-DI" aria-label="Filter">
<select id="f" aria-label="Status"><option value="">All statuses</option>
<option>found</option><option>possible</option><option>not found</option>
<option>error</option></select>
<select id="m" aria-label="Evidence"><option value="">All evidence</option>
<option value="trade_name">Trade name</option><option value="device_name">Device name</option>
<option value="manufacturer">Manufacturer only</option></select>
</div>
<div class="wrap"><table>
<thead><tr><th>Device</th><th>Status</th><th>Best match</th><th>Evidence</th>
<th>Manufacturer</th><th>Class</th><th>Status</th></tr></thead>
<tbody id="rows"></tbody>
</table></div>
</main>
<script>
const DATA = __DATA__, META = __META__;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const cls = s => s === "found" ? "found" : s === "possible" ? "possible"
  : s === "error" ? "error" : "none";
const openRows = new Set();
document.getElementById("sub").textContent =
  `${DATA.length} device(s) searched against ${META.base || "the EUDAMED Public API"} on ${META.generated || "-"}`
  + `${META.fields ? " by " + META.fields : ""}. Click a row for all candidates.`;
const count = s => DATA.filter(d => d.status === s).length;
document.getElementById("tally").innerHTML = ["found","possible","not found","error"]
  .filter(s => s !== "error" || count(s))
  .map(s => `<div><b class="st ${cls(s)}">${count(s)}</b><span>${s}</span></div>`).join("");
const nErr = count("error");
const mfrOnly = DATA.filter(d => d.candidates.some(c => c.matched_on === "manufacturer")).length;
document.getElementById("note").innerHTML =
  (nErr ? `<strong>${nErr} device(s) could not be checked</strong> &mdash; every request for them
    failed, so their registration is unknown, not absent. Fix the errors shown on those rows and
    re-run. ` : "")
  + `Scores rank candidates, they do not confirm registration &mdash; always open the EUDAMED link before relying on a match.`
  + (mfrOnly ? ` ${mfrOnly} device(s) have manufacturer-only leads, shown with a <span class="chip mfr">manufacturer</span> chip: the manufacturer name matched but the trade name did not, so these are never counted as found.` : "");
const FIELDS = [["Trade name","trade_name"],["Device name","device_name"],["Model","device_model"],
["Manufacturer","manufacturer_name"],["Manufacturer SRN","mf_srn"],["Manufacturer country","manufacturer_country"],
["Risk class","risk_class"],["Legislation","legislation"],["Device status","device_status"],
["Placed on market","placed_on_market"],
["Special type","special_type"],["UDI-DI","primary_di"],["Basic UDI-DI","basic_udi"],
["EMDN / nomenclature","nomenclature_code"],["Medical purpose","medical_purpose"],
["Reference","reference"],["Version","version"],["Score","score"],["Evidence","matched_on"]];
function chip(c){
  const m = c.matched_on || "none";
  return `<span class="chip${m === "manufacturer" ? " mfr" : ""}">${esc(m)}</span>`;
}
function detail(d){
  const cands = d.candidates.map(c => {
    const warn = c.matched_on === "manufacturer"
      ? `<div class="warnbox">Manufacturer-name match only &mdash; the trade name does not match "${esc(d.name)}". Capped below the match threshold; verify manually.</div>` : "";
    const rows = FIELDS.filter(([,k]) => c[k] !== undefined && c[k] !== "" && c[k] !== null)
      .map(([l,k]) => `<dt>${l}</dt><dd>${esc(c[k])}</dd>`).join("");
    const title = esc(c.trade_name || c.device_name || "(unnamed)");
    return `<div class="cand">${c.link ? `<a href="${esc(c.link)}" target="_blank" rel="noopener">${title}</a>` : title} ${chip(c)}${warn}<dl>${rows}</dl></div>`;
  }).join("") || `<p class="muted">No candidates above the score threshold. Try other spellings in <code>keys</code>, or search <code>DEVICE_NAME</code> as well as <code>TRADE_NAME</code>.</p>`;
  const qs = (d.queries || []).map(q =>
    `<code>${esc(q.param)}=${esc(q.term)}</code> ${q.error ? "error" : q.rows + " row(s)"}`).join(", ");
  const errs = (d.errors || []).length
    ? `<div class="warnbox">${d.errors.map(esc).join("<br>")}</div>` : "";
  return `<div><p class="muted">${esc(d.description || "")}${d.ca ? " &middot; Competent authority: " + esc(d.ca) : ""}</p>`
    + `${cands}<p class="muted">Queries: ${qs}</p>${errs}</div>`;
}
function render(){
  const q = document.getElementById("q").value.toLowerCase().trim();
  const fs = document.getElementById("f").value, ms = document.getElementById("m").value;
  const html = DATA.map((d,i) => {
    const hay = [d.name, ...d.candidates.flatMap(c =>
      [c.trade_name, c.device_name, c.manufacturer_name, c.mf_srn, c.primary_di])].join(" ").toLowerCase();
    if (q && !hay.includes(q)) return "";
    if (fs && d.status !== fs) return "";
    if (ms && !d.candidates.some(c => (c.matched_on || "").startsWith(ms))) return "";
    const b = d.status !== "not found" && d.candidates.length ? d.candidates[0] : {};
    const row = `<tr class="dev" tabindex="0" data-i="${i}" aria-expanded="${openRows.has(i)}">`
      + `<td>${esc(d.name)}</td><td class="st ${cls(d.status)}">${esc(d.status)}</td>`
      + `<td>${esc(b.trade_name || "")}</td><td>${b.trade_name ? chip(b) : ""}</td>`
      + `<td>${esc(b.manufacturer_name || "")}<div class="muted">${esc(b.mf_srn || "")}</div></td>`
      + `<td>${esc(b.risk_class || "")}</td><td>${esc(b.device_status || "")}</td></tr>`;
    return openRows.has(i) ? row + `<tr class="more"><td colspan="7">${detail(d)}</td></tr>` : row;
  }).join("");
  document.getElementById("rows").innerHTML = html
    || `<tr><td colspan="7" class="muted">Nothing matches the current filters.</td></tr>`;
}
function toggle(tr){
  const i = +tr.dataset.i;
  openRows.has(i) ? openRows.delete(i) : openRows.add(i);
  render();
  document.querySelector(`tr.dev[data-i="${i}"]`)?.focus();
}
const rowsEl = document.getElementById("rows");
rowsEl.addEventListener("click", e => {
  const tr = e.target.closest("tr.dev");
  if (tr && !e.target.closest("a")) toggle(tr);
});
rowsEl.addEventListener("keydown", e => {
  const tr = e.target.closest("tr.dev");
  if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(tr); }
});
for (const id of ["q","f","m"])
  document.getElementById(id).addEventListener(id === "q" ? "input" : "change", render);
render();
</script>
</body>
</html>
"""
