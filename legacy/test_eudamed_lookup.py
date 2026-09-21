import io, json, os, sys, urllib.error, urllib.parse, urllib.request, types
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eudamed_lookup as el


# ---------- fake transport ----------
class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

DEVICE_ROWS = {
    "deprexis": [{
        "uuid": "u-deprexis", "tradeName": "deprexis", "manufacturerName": "GAIA AG",
        "manufacturerSrn": "DE-MF-000012345", "riskClass": {"code": "RISK_CLASS.IIA"},
        "deviceStatusType": {"code": "DEVICE_STATUS.ON_THE_MARKET"},
        "primaryDi": "0426", "basicUdi": "B0426", "versionNumber": 2, "latestVersion": True,
        "authorisedRepresentativeName": "", "basicUdiDiDataUlid": "bulid-1",
    }],
    "kalmeda": [
        {"uuid": "u-kal", "tradeName": "Kalmeda", "manufacturerName": "mynoise GmbH",
         "manufacturerSrn": "DE-MF-000099", "riskClass": {"code": "RISK_CLASS.I"},
         "deviceStatusType": {"code": "DEVICE_STATUS.ON_THE_MARKET"}, "primaryDi": "0001",
         "basicUdi": "B1", "versionNumber": 1, "latestVersion": True, "basicUdiDiDataUlid": None},
        {"uuid": "u-kal-old", "tradeName": "Kalmeda tinnitus", "manufacturerName": "mynoise GmbH",
         "manufacturerSrn": "DE-MF-000099", "riskClass": {"code": "RISK_CLASS.I"},
         "deviceStatusType": {"code": "DEVICE_STATUS.NO_LONGER"}, "primaryDi": "0002",
         "basicUdi": "B2", "versionNumber": 1, "latestVersion": False, "basicUdiDiDataUlid": None},
    ],
}
CALLS = []

def make_urlopen(first_page_ok=0, huge_total_for=(), fail_detail=False):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        CALLS.append(url)
        path, _, qs = url.partition("?")
        q = dict(urllib.parse.parse_qsl(qs))
        path = path[len(el.BASE):]
        if path == "/devices/udiDiData":
            page = int(q.get("page", "0"))
            if page < first_page_ok:
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            term = (q.get("tradeName") or "").lower()
            if term in huge_total_for:
                return FakeResp(json.dumps({"content": [], "totalElements": 999999, "last": True}).encode())
            rows = DEVICE_ROWS.get(term, [])
            return FakeResp(json.dumps({"content": rows, "totalElements": len(rows), "last": True}).encode())
        if path.startswith("/devices/udiDiData/"):
            if fail_detail:
                raise urllib.error.HTTPError(url, 500, "boom", {}, None)
            return FakeResp(json.dumps({
                "cndNomenclatures": [{"code": "Z12010203", "description": {"texts": [{"text": "medical software"}]}}],
                "additionalInformationUrl": "https://example.org/info",
                "deviceStatus": {"statusDate": "2023-05-01"},
                "marketInfoLink": {"msWhereAvailable": [{"country": {"iso2Code": "DE"}}, {"country": {"iso2Code": "AT"}}]},
            }).encode())
        if path.startswith("/devices/basicUdiData/"):
            return FakeResp(json.dumps({
                "legislation": {"code": "LEGISLATION.MDR"},
                "specialDeviceType": {"code": "SPECIAL_DEVICE_TYPE.NONE"},
                "deviceName": "deprexis online therapy",
                "manufacturer": {"actorDataPublicView": {"country": {"iso2Code": "DE"}, "website": "https://gaia-group.com"}},
                "deviceCertificateInfoList": [{"certificateNumber": "CE-123", "notifiedBody": {"name": "TUV"}, "certificateExpiry": "2027-01-01"}],
            }).encode())
        raise urllib.error.HTTPError(url, 404, "nope", {}, None)
    return urlopen

@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(el.urllib.request, "urlopen", make_urlopen())
    monkeypatch.setattr(el.time, "sleep", lambda *_: None)


# ---------- pure helpers ----------
def test_norm_and_squash():
    assert el.norm("Oviva Direkt für Adipositas") == "oviva direkt fur adipositas"
    assert el.norm("i.s.h.med") == "i s h med"
    assert el.squash("i.s.h. med") == "ishmed"
    assert el.norm(None) == ""
    assert el.norm("PINK! Coach") == "pink coach"

def test_code_and_text():
    assert el.code({"code": "RISK_CLASS.IIA"}) == "IIA"
    assert el.code({"code": ""}) == ""
    assert el.code(None) == ""
    assert el.code("RISK_CLASS.I") == ""
    assert el.text({"texts": [{"text": "hi"}]}) == "hi"
    assert el.text({"texts": []}) == ""
    assert el.text("plain") == "plain"
    assert el.text(None) == ""

def test_classify_boundaries():
    assert el.classify(1.0) == el.classify(0.85) == "found"
    assert el.classify(0.849) == el.classify(0.6) == "possible"
    assert el.classify(0.599) == "not found"

def test_score_exact_and_country_bonus():
    row = {"tradeName": "deprexis", "manufacturerName": "GAIA AG", "manufacturerSrn": "DE-MF-1"}
    assert el.score(["deprexis"], "DE", row) == 1.0          # 1.0 + bonus, clamped
    assert el.score(["deprexis"], "NL", row) == 0.9          # 1.0 - 0.1 penalty
    assert el.score(["deprexis"], "", row) == 1.0

def test_score_substring_and_fuzzy():
    row = {"tradeName": "Kalmeda tinnitus app", "manufacturerName": "x", "manufacturerSrn": ""}
    assert el.score(["Kalmeda"], "", row) == 0.92            # key inside trade name
    row2 = {"tradeName": "PINK", "manufacturerName": "x", "manufacturerSrn": ""}
    assert el.score(["PINK! Coach"], "", row2) == 0.8        # trade name inside key
    row3 = {"tradeName": "totally unrelated", "manufacturerName": "x", "manufacturerSrn": ""}
    assert el.score(["deprexis"], "", row3) < 0.6
    assert el.score([], "", row3) == 0.0
    assert el.score([""], "", row3) == 0.0

def test_score_is_clamped():
    row = {"tradeName": "zzz", "manufacturerName": "zzz", "manufacturerSrn": "NL-MF-1"}
    assert 0.0 <= el.score(["deprexis"], "DE", row) <= 1.0

def test_candidate_shape():
    c = el.candidate(DEVICE_ROWS["deprexis"][0], 0.9)
    assert c["riskClass"] == "IIA" and c["deviceStatus"] == "ON_THE_MARKET"
    assert c["link"] == el.UI_DEVICE.format(uuid="u-deprexis")
    assert el.candidate({}, 0.1)["link"] == ""

def test_safe_json_escapes_script_close():
    assert "</" not in el.safe_json({"x": "</script><img src=x>"})
    assert "<\\/script>" in el.safe_json({"x": "</script>"})


# ---------- client ----------
def test_client_retries_then_raises(monkeypatch):
    calls = []
    def boom(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError("down")
    monkeypatch.setattr(el.urllib.request, "urlopen", boom)
    c = el.Client(0, 3, 5, False)
    with pytest.raises(RuntimeError):
        c.get("/devices/udiDiData")
    assert len(calls) == 3

def test_client_no_retry_on_400(monkeypatch):
    calls = []
    def bad(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)
    monkeypatch.setattr(el.urllib.request, "urlopen", bad)
    c = el.Client(0, 4, 5, False)
    with pytest.raises(RuntimeError):
        c.get("/devices/udiDiData")
    assert len(calls) == 1

def test_client_zero_retries_never_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(el.urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    c = el.Client(0, 0, 5, False)
    with pytest.raises(RuntimeError):
        c.get("/devices/udiDiData")
    assert calls == []

def test_search_detects_one_based_pagination(monkeypatch):
    monkeypatch.setattr(el.urllib.request, "urlopen", make_urlopen(first_page_ok=1))
    c = el.Client(0, 2, 5, False)
    rows, total, ignored = c.search("deprexis", 100, 3)
    assert c.first_page == 1 and total == 1 and not ignored and len(rows) == 1

def test_search_flags_ignored_filter(monkeypatch):
    monkeypatch.setattr(el.urllib.request, "urlopen", make_urlopen(huge_total_for=("deprexis",)))
    c = el.Client(0, 2, 5, False)
    rows, total, ignored = c.search("deprexis", 100, 3)
    assert ignored is True and rows == [] and total == 999999

def test_search_paginates(monkeypatch):
    pages = [
        {"content": [{"uuid": "a"}], "totalElements": 2, "last": False},
        {"content": [{"uuid": "b"}], "totalElements": 2, "last": True},
    ]
    seq = iter(pages)
    monkeypatch.setattr(el.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(json.dumps(next(seq)).encode()))
    c = el.Client(0, 2, 5, False)
    rows, total, _ = c.search("x", 1, 5)
    assert [r["uuid"] for r in rows] == ["a", "b"]

def test_search_respects_max_pages(monkeypatch):
    monkeypatch.setattr(el.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp(json.dumps(
                            {"content": [{"uuid": "a"}], "totalElements": 99, "last": False}).encode()))
    c = el.Client(0, 2, 5, False)
    rows, _, _ = c.search("x", 1, 2)
    assert len(rows) == 2

def test_enrich_populates_and_survives_errors(monkeypatch):
    c = el.Client(0, 2, 5, False)
    out = el.enrich(c, {"uuid": "u-deprexis", "basicUdiDiDataUlid": "bulid-1"})
    assert out["emdn"] == "Z12010203 medical software"
    assert out["markets"] == "DE, AT" and out["legislation"] == "MDR"
    assert out["certificates"] == "CE-123 TUV exp 2027-01-01"
    assert "detail_error" not in out and "basic_error" not in out

    monkeypatch.setattr(el.urllib.request, "urlopen", make_urlopen(fail_detail=True))
    out2 = el.enrich(el.Client(0, 1, 5, False), {"uuid": "u-deprexis", "basicUdiDiDataUlid": "bulid-1"})
    assert "detail_error" in out2 and out2.get("legislation") == "MDR"


# ---------- input loading ----------
def test_load_devices_default():
    assert el.load_devices(None) is el.DEVICES
    assert len(el.DEVICES) == 23
    for row in el.DEVICES:
        assert len(row) == 6 and isinstance(row[4], list) and isinstance(row[5], list)
        assert row[4], f"{row[0]} has no keys"

def test_load_devices_csv(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("name,description,ca,country,keys,broad\n"
                 "deprexis,depression,Hamburg,de,deprexis|Deprexis 2,GAIA\n"
                 "Bare,,,,,\n", encoding="utf-8")
    got = el.load_devices(str(p))
    assert got[0] == ("deprexis", "depression", "Hamburg", "DE", ["deprexis", "Deprexis 2"], ["GAIA"])
    assert got[1] == ("Bare", "", "", "", ["Bare"], [])   # keys fall back to name


# ---------- full run + writers ----------
def _args(**kw):
    base = dict(input=None, out="out", details=True, delay=0, retries=2, timeout=5,
                page_size=100, max_pages=3, top=5, min_score=0.45, verbose=False)
    base.update(kw)
    return types.SimpleNamespace(**base)

def test_run_end_to_end(tmp_path, monkeypatch, capsys):
    csvp = tmp_path / "in.csv"
    csvp.write_text("name,description,ca,country,keys,broad\n"
                    "deprexis,depression,Hamburg DE,DE,deprexis,\n"
                    "Kalmeda,tinnitus,NRW DE,DE,Kalmeda,\n"
                    "Nonexistent,nothing,XX,DE,zzzznope,\n", encoding="utf-8")
    results = el.run(_args(input=str(csvp)))
    assert [r["status"] for r in results] == ["found", "found", "not found"]
    dep = results[0]
    assert dep["candidates"][0]["tradeName"] == "deprexis"
    assert dep["candidates"][0]["emdn"] == "Z12010203 medical software"
    assert dep["errors"] == []
    kal = results[1]
    assert kal["candidates"][0]["latestVersion"] is True          # latest sorted first
    assert results[2]["candidates"] == []

    out = tmp_path / "out"
    out.mkdir()
    el.write_csv(results, out / "results.csv")
    el.write_html(results, out / "report.html")
    import csv as _csv
    rows = list(_csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert [r["name"] for r in rows] == ["deprexis", "Kalmeda", "Nonexistent"]
    assert rows[0]["tradeName"] == "deprexis" and rows[0]["riskClass"] == "IIA"
    assert rows[2]["tradeName"] == "" and rows[2]["status"] == "not found"
    h = (out / "report.html").read_text(encoding="utf-8")
    assert "__DATA__" not in h and "__META__" not in h
    assert json.loads(h.split("const DATA = ")[1].split(";\nconst META")[0].replace("<\\/", "</"))

def test_run_records_search_errors(tmp_path, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(el.urllib.request, "urlopen", boom)
    csvp = tmp_path / "in.csv"
    csvp.write_text("name,description,ca,country,keys,broad\nX,,,DE,X,\n", encoding="utf-8")
    results = el.run(_args(input=str(csvp), retries=1))
    assert results[0]["status"] == "not found" and results[0]["errors"]

def test_main_writes_all_three_outputs(tmp_path, monkeypatch):
    csvp = tmp_path / "in.csv"
    csvp.write_text("name,description,ca,country,keys,broad\ndeprexis,d,H,DE,deprexis,\n", encoding="utf-8")
    outdir = tmp_path / "res"
    monkeypatch.setattr(sys, "argv", ["eudamed_lookup.py", "--input", str(csvp),
                                      "--out", str(outdir), "--delay", "0", "--retries", "2"])
    el.main()
    assert (outdir / "results.json").exists()
    assert (outdir / "results.csv").exists()
    assert (outdir / "report.html").exists()
    data = json.loads((outdir / "results.json").read_text(encoding="utf-8"))
    assert data[0]["status"] == "found"

def test_html_injection_is_escaped(tmp_path):
    results = [{"name": "</script><script>alert(1)</script>", "description": "d", "ca": "", "country": "DE",
                "status": "not found", "queries": [], "errors": [], "candidates": []}]
    p = tmp_path / "r.html"
    el.write_html(results, p)
    h = p.read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in h
