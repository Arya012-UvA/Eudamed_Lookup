import argparse
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

BASE = os.environ.get("EUDAMED_BASE", "https://ec.europa.eu/tools/eudamed/api")
UI_DEVICE = "https://ec.europa.eu/tools/eudamed/#/screen/search-device/{uuid}"
UA = "Mozilla/5.0 (compatible; eudamed-lookup/1.0)"
MAX_PLAUSIBLE_TOTAL = 20000

DEVICES = [
    ("Actimi Telecare", "Software for patients with heart failure", "Baden Wurttemberg, DE", "DE", ["Actimi Telecare"], ["Actimi"]),
    ("companion patella", "Software for patients with knee pain", "Bavaria, DE", "DE", ["companion patella"], ["companion"]),
    ("deprexis", "Software for patients with depression", "Hamburg, DE", "DE", ["deprexis"], []),
    ("elevida", "Software for patients with multiple sclerosis", "Hamburg, DE", "DE", ["elevida"], []),
    ("Floy Signal", "?", "Bavaria, DE", "DE", ["Floy Signal"], ["Floy"]),
    ("HelloBetter ratiopharm chronischer Schmerz", "Software for patients with chronic pain", "Hamburg, DE", "DE", ["HelloBetter ratiopharm chronischer Schmerz", "HelloBetter ratiopharm", "HelloBetter chronischer Schmerz"], ["HelloBetter"]),
    ("HelloBetter Stress und Burnout", "Software for patients with stress and burnout", "Hamburg, DE", "DE", ["HelloBetter Stress und Burnout", "HelloBetter Stress"], ["HelloBetter"]),
    ("i.s.h. med", "Hospital information system", "NL", "NL", ["i.s.h.med", "i.s.h. med", "ish med"], ["ishmed"]),
    ("Kalmeda", "Software for patients with tinnitus", "North Rhine-Westfalia, DE", "DE", ["Kalmeda"], []),
    ("Kranus Edera", "Software for patients with erectile dysfunction", "Bavaria, DE", "DE", ["Kranus Edera", "Edera"], ["Kranus"]),
    ("mebix", "Software for patients with diabetes", "Thuringia, DE", "DE", ["mebix"], []),
    ("Meine Tinnitus App", "Software for patients with tinnitus", "Hamburg, DE", "DE", ["Meine Tinnitus App", "Meine Tinnitus"], ["Tinnitus App"]),
    ("MindDoc", "Software for patients with psychological diseases (?)", "Bavaria, DE", "DE", ["MindDoc"], ["Mind Doc"]),
    ("neolexon Aphasie", "Software for patients with aphasia", "Bavaria, DE", "DE", ["neolexon Aphasie", "neolexon"], []),
    ("optimune", "Software for patients with breast cancer", "Hamburg, DE", "DE", ["optimune"], []),
    ("Oviva Direkt für Adipositas", "Software for patients with obesity", "Saarland, DE", "DE", ["Oviva Direkt für Adipositas", "Oviva Direkt"], ["Oviva"]),
    ("PINK! Coach", "Software for patients with breast cancer", "Hamburg, DE", "DE", ["PINK! Coach", "PINK Coach"], ["PINK"]),
    ("QuickBird Studios mamly", "?", "Bavaria, DE", "DE", ["mamly"], ["QuickBird"]),
    ("sinCephalea", "Software for patients with migraine", "Schleswig-Holstein, DE", "DE", ["sinCephalea"], []),
    ("velibra", "Software for patients with anxiety disorder", "Hamburg, DE", "DE", ["velibra"], []),
    ("vorvida", "Software for patients with alcohol addiction", "Hamburg, DE", "DE", ["vorvida"], []),
    ("Vitadio", "Software for patients with chronic diseases", "CZ", "CZ", ["Vitadio"], []),
    ("Veye Engine", "\u201corchestration layer around various clinical modules known as Devices\u201d", "NL", "NL", ["Veye Engine"], ["Veye"]),
]


class Client:
    def __init__(self, delay, retries, timeout, verbose):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.verbose = verbose
        self.first_page = None

    def get(self, path, params=None):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = None
        for attempt in range(self.retries):
            time.sleep(self.delay if attempt == 0 else self.delay * (2 ** attempt))
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if self.verbose:
                    print(f"  GET {url} ok", file=sys.stderr)
                return data
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (400, 404):
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
                last = e
        raise RuntimeError(f"{url} -> {last}")

    def search_page(self, term, page, size):
        return self.get("/devices/udiDiData", {
            "tradeName": term,
            "page": page,
            "pageSize": size,
            "size": size,
            "iso2Code": "en",
            "languageIso2Code": "en",
        })

    def search(self, term, size, max_pages):
        if self.first_page is None:
            for candidate in (0, 1):
                try:
                    data = self.search_page(term, candidate, size)
                    self.first_page = candidate
                    break
                except RuntimeError:
                    continue
            else:
                raise RuntimeError(f"search failed for {term!r} with page=0 and page=1")
        else:
            data = self.search_page(term, self.first_page, size)
        total = data.get("totalElements") or 0
        if total > MAX_PLAUSIBLE_TOTAL:
            return [], total, True
        rows = list(data.get("content") or [])
        page = self.first_page
        while not data.get("last", True) and page - self.first_page + 1 < max_pages:
            page += 1
            data = self.search_page(term, page, size)
            rows.extend(data.get("content") or [])
        return rows, total, False


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def squash(s):
    return norm(s).replace(" ", "")


def score(keys, country, row):
    tn = norm(row.get("tradeName"))
    tq = squash(row.get("tradeName"))
    mn = norm(row.get("manufacturerName"))
    best = 0.0
    for k in keys:
        nk = norm(k)
        sk = squash(k)
        if not nk:
            continue
        if tq == sk:
            s = 1.0
        elif sk in tq:
            s = 0.92
        elif len(tq) >= 4 and tq in sk:
            s = 0.8
        else:
            a = set(nk.split())
            b = set(tn.split()) | set(mn.split())
            overlap = len(a & b) / len(a) if a else 0.0
            s = 0.85 * max(SequenceMatcher(None, sk, tq).ratio(), overlap)
        best = max(best, s)
    srn = (row.get("manufacturerSrn") or "")[:2].upper()
    if country and srn == country:
        best += 0.05
    elif country and srn and srn != country:
        best -= 0.1
    return round(max(0.0, min(1.0, best)), 3)


def code(obj):
    if isinstance(obj, dict):
        c = obj.get("code") or ""
        return c.rsplit(".", 1)[-1] if c else ""
    return ""


def text(obj):
    if isinstance(obj, dict):
        for t in obj.get("texts") or []:
            if t.get("text"):
                return t["text"]
    return obj if isinstance(obj, str) else ""


def enrich(client, row):
    out = {}
    try:
        d = client.get(f"/devices/udiDiData/{row['uuid']}", {"languageIso2Code": "en"})
        out["emdn"] = "; ".join(
            f"{c.get('code', '')} {text(c.get('description'))}".strip() for c in d.get("cndNomenclatures") or []
        )
        out["info_url"] = d.get("additionalInformationUrl") or ""
        out["status_date"] = (d.get("deviceStatus") or {}).get("statusDate") or ""
        out["markets"] = ", ".join(
            (m.get("country") or {}).get("iso2Code", "")
            for m in ((d.get("marketInfoLink") or {}).get("msWhereAvailable") or [])
        )
    except (RuntimeError, KeyError, AttributeError, TypeError) as e:
        out["detail_error"] = str(e)
    bid = row.get("basicUdiDiDataUlid")
    if bid:
        try:
            b = client.get(f"/devices/basicUdiData/{bid}", {"languageIso2Code": "en"})
            out["legislation"] = code(b.get("legislation"))
            out["special_type"] = code(b.get("specialDeviceType"))
            out["device_name"] = b.get("deviceName") or ""
            actor = ((b.get("manufacturer") or {}).get("actorDataPublicView") or {})
            out["manufacturer_country"] = (actor.get("country") or {}).get("iso2Code", "")
            out["manufacturer_website"] = actor.get("website") or ""
            out["certificates"] = "; ".join(
                " ".join(filter(None, [
                    c.get("certificateNumber"),
                    (c.get("notifiedBody") or {}).get("name"),
                    f"exp {c.get('certificateExpiry')}" if c.get("certificateExpiry") else "",
                ]))
                for c in b.get("deviceCertificateInfoList") or []
            )
        except (RuntimeError, KeyError, AttributeError, TypeError) as e:
            out["basic_error"] = str(e)
    return out


def classify(s):
    if s >= 0.85:
        return "found"
    if s >= 0.6:
        return "possible"
    return "not found"


def candidate(row, s):
    return {
        "score": s,
        "tradeName": row.get("tradeName") or "",
        "manufacturerName": row.get("manufacturerName") or "",
        "manufacturerSrn": row.get("manufacturerSrn") or "",
        "riskClass": code(row.get("riskClass")),
        "deviceStatus": code(row.get("deviceStatusType")),
        "primaryDi": row.get("primaryDi") or "",
        "basicUdi": row.get("basicUdi") or "",
        "version": row.get("versionNumber"),
        "latestVersion": row.get("latestVersion"),
        "authorisedRepresentative": row.get("authorisedRepresentativeName") or "",
        "uuid": row.get("uuid") or "",
        "basicUdiDiDataUlid": row.get("basicUdiDiDataUlid") or "",
        "link": UI_DEVICE.format(uuid=row.get("uuid")) if row.get("uuid") else "",
    }


def load_devices(path):
    if not path:
        return DEVICES
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            split = lambda v: [x.strip() for x in (v or "").split("|") if x.strip()]
            keys = split(r.get("keys")) or [r["name"]]
            out.append((r["name"], r.get("description", ""), r.get("ca", ""), (r.get("country") or "").upper(), keys, split(r.get("broad"))))
    return out


def run(args):
    client = Client(args.delay, args.retries, args.timeout, args.verbose)
    results = []
    for name, desc, ca, country, keys, broad in load_devices(args.input):
        print(f"{name}", file=sys.stderr)
        seen = {}
        queries = []
        errors = []
        for term in keys + broad:
            try:
                rows, total, ignored = client.search(term, args.page_size, args.max_pages)
            except RuntimeError as e:
                errors.append(str(e))
                queries.append({"term": term, "total": None, "filter_ignored": False})
                continue
            queries.append({"term": term, "total": total, "filter_ignored": ignored})
            if ignored:
                errors.append(f"tradeName filter appears ignored for {term!r} (total {total})")
            for row in rows:
                uid = row.get("uuid") or row.get("primaryDi")
                if uid and uid not in seen:
                    seen[uid] = row
        ranked = sorted(
            (candidate(r, score(keys, country, r)) for r in seen.values()),
            key=lambda c: (-c["score"], not c["latestVersion"], c["tradeName"]),
        )
        ranked = [c for c in ranked if c["score"] >= args.min_score][: args.top]
        status = classify(ranked[0]["score"]) if ranked else "not found"
        if args.details:
            for c in ranked:
                if c["score"] >= 0.6 and c["uuid"]:
                    c.update(enrich(client, {"uuid": c["uuid"], "basicUdiDiDataUlid": c["basicUdiDiDataUlid"]}))
        results.append({
            "name": name, "description": desc, "ca": ca, "country": country,
            "status": status, "queries": queries, "errors": errors, "candidates": ranked,
        })
        print(f"  -> {status} ({len(ranked)} candidates)", file=sys.stderr)
    return results


CSV_FIELDS = [
    "name", "ca", "status", "score", "tradeName", "manufacturerName", "manufacturerSrn",
    "riskClass", "deviceStatus", "legislation", "special_type", "primaryDi", "basicUdi",
    "emdn", "certificates", "markets", "candidates", "errors", "link",
]


def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            top = r["candidates"][0] if r["candidates"] and r["status"] != "not found" else {}
            w.writerow({**top, "name": r["name"], "ca": r["ca"], "status": r["status"],
                        "candidates": len(r["candidates"]), "errors": " | ".join(r["errors"])})


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EUDAMED lookup</title>
<style>
:root{--ink:#17202b;--muted:#5d6b7a;--line:#d9dee4;--bg:#ffffff;--band:#f2f5f8;--found:#1d6b45;--possible:#9a5b00;--none:#8a94a0;--link:#1f4f8a}
@media (prefers-color-scheme:dark){:root{--ink:#e6ebf0;--muted:#9aa7b4;--line:#2e3742;--bg:#121820;--band:#1a222c;--found:#5cc48f;--possible:#e3a64a;--none:#7d8894;--link:#8ab8f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 24px}
.tally{display:flex;gap:28px;margin:0 0 20px;flex-wrap:wrap}
.tally b{font-size:28px;display:block;line-height:1}
.tally span{color:var(--muted);font-size:13px}
.controls{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
input,select{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
input{flex:1;min-width:200px}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:13px;color:var(--muted);font-weight:600}
tr.dev{cursor:pointer}
tr.dev:hover,tr.dev:focus{background:var(--band);outline:none}
tr.dev:focus-visible{outline:2px solid var(--link);outline-offset:-2px}
.st{font-weight:600}
.st.found{color:var(--found)}.st.possible{color:var(--possible)}.st.none{color:var(--none)}
.muted{color:var(--muted);font-size:13px}
tr.more td{background:var(--band);padding:14px 16px}
.cand{padding:10px 0;border-top:1px solid var(--line)}
.cand:first-child{border-top:0;padding-top:0}
.cand dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:6px 0 0;font-size:13px}
.cand dt{color:var(--muted)}.cand dd{margin:0;word-break:break-word}
a{color:var(--link)}
.err{color:var(--possible);font-size:13px}
</style>
</head>
<body>
<main>
<h1>EUDAMED lookup</h1>
<p class="sub" id="sub"></p>
<div class="tally" id="tally"></div>
<div class="controls">
<input id="q" type="search" placeholder="Filter by name, manufacturer or SRN" aria-label="Filter">
<select id="f" aria-label="Status"><option value="">All statuses</option><option>found</option><option>possible</option><option>not found</option></select>
</div>
<div class="wrap"><table>
<thead><tr><th>Device</th><th>Status</th><th>Best match</th><th>Manufacturer</th><th>Class</th><th>Market status</th></tr></thead>
<tbody id="rows"></tbody>
</table></div>
</main>
<script>
const DATA = __DATA__;
const META = __META__;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const cls = s => s === "found" ? "found" : s === "possible" ? "possible" : "none";
const open = new Set();
document.getElementById("sub").textContent = `Searched ${DATA.length} devices by trade name on ${META.generated}. Click a row to see all candidates.`;
const count = s => DATA.filter(d => d.status === s).length;
document.getElementById("tally").innerHTML = ["found","possible","not found"].map(s => `<div><b class="st ${cls(s)}">${count(s)}</b><span>${s}</span></div>`).join("");
const fields = [["Trade name","tradeName"],["Manufacturer","manufacturerName"],["SRN","manufacturerSrn"],["Risk class","riskClass"],["Market status","deviceStatus"],["Legislation","legislation"],["Special type","special_type"],["UDI-DI","primaryDi"],["Basic UDI-DI","basicUdi"],["EMDN","emdn"],["Certificates","certificates"],["Markets","markets"],["Status date","status_date"],["Version","version"],["Authorised rep.","authorisedRepresentative"],["Score","score"]];
function detail(d) {
  const c = d.candidates.map(c => `<div class="cand"><a href="${esc(c.link)}" target="_blank" rel="noopener">${esc(c.tradeName) || "(no trade name)"}</a><dl>${fields.filter(([,k]) => c[k] !== undefined && c[k] !== "" && c[k] !== null).map(([l,k]) => `<dt>${l}</dt><dd>${esc(c[k])}</dd>`).join("")}</dl></div>`).join("") || `<p class="muted">No candidates. Try other spellings or the manufacturer name in the input CSV.</p>`;
  const q = `<p class="muted">Queries: ${d.queries.map(q => `${esc(q.term)} (${q.total ?? "error"})`).join(", ")}</p>`;
  const e = d.errors.length ? `<p class="err">${d.errors.map(esc).join("<br>")}</p>` : "";
  return `<div><p class="muted">${esc(d.description)}. Competent authority: ${esc(d.ca)}</p>${c}${q}${e}</div>`;
}
function render() {
  const q = document.getElementById("q").value.toLowerCase();
  const f = document.getElementById("f").value;
  document.getElementById("rows").innerHTML = DATA.map((d, i) => {
    const hay = [d.name, ...d.candidates.flatMap(c => [c.tradeName, c.manufacturerName, c.manufacturerSrn])].join(" ").toLowerCase();
    if ((q && !hay.includes(q)) || (f && d.status !== f)) return "";
    const b = d.status !== "not found" ? d.candidates[0] : {};
    const row = `<tr class="dev" tabindex="0" data-i="${i}" aria-expanded="${open.has(i)}"><td>${esc(d.name)}</td><td class="st ${cls(d.status)}">${d.status}</td><td>${esc(b.tradeName)}</td><td>${esc(b.manufacturerName)}<div class="muted">${esc(b.manufacturerSrn)}</div></td><td>${esc(b.riskClass)}</td><td>${esc(b.deviceStatus)}</td></tr>`;
    return open.has(i) ? row + `<tr class="more"><td colspan="6">${detail(d)}</td></tr>` : row;
  }).join("");
}
function toggle(el) { const i = +el.dataset.i; open.has(i) ? open.delete(i) : open.add(i); render(); document.querySelector(`tr.dev[data-i="${i}"]`)?.focus(); }
document.getElementById("rows").addEventListener("click", e => { const tr = e.target.closest("tr.dev"); if (tr && !e.target.closest("a")) toggle(tr); });
document.getElementById("rows").addEventListener("keydown", e => { const tr = e.target.closest("tr.dev"); if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(tr); } });
document.getElementById("q").addEventListener("input", render);
document.getElementById("f").addEventListener("change", render);
render();
</script>
</body>
</html>
"""


def safe_json(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def write_html(results, path):
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M")}
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML.replace("__DATA__", safe_json(results)).replace("__META__", safe_json(meta)))


def main():
    p = argparse.ArgumentParser(description="Search EUDAMED for a list of devices by trade name.")
    p.add_argument("--input", help="CSV with columns name,description,ca,country,keys,broad (keys/broad separated by |)")
    p.add_argument("--out", default="eudamed_results")
    p.add_argument("--no-details", dest="details", action="store_false")
    p.add_argument("--delay", type=float, default=0.8)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--min-score", type=float, default=0.45)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)
    results = run(args)
    with open(os.path.join(args.out, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    write_csv(results, os.path.join(args.out, "results.csv"))
    write_html(results, os.path.join(args.out, "report.html"))
    tally = {s: sum(r["status"] == s for r in results) for s in ("found", "possible", "not found")}
    print(f"\n{tally}  ->  {os.path.abspath(args.out)}/report.html", file=sys.stderr)


if __name__ == "__main__":
    main()
