"""Client for the EUDAMED web UI's own backend.

Two different APIs serve EUDAMED:

``datalake``
    ``api.datalake.sante.service.ec.europa.eu/eudamed`` - the documented
    public API. Stable and versioned, but its filters are **exact match**, so
    a device cannot be found unless its full registered trade name is already
    known.

``ui`` (this module)
    ``ec.europa.eu/tools/eudamed/api`` - what the EUDAMED website itself calls.
    Undocumented and unversioned, so it can change without notice, but it does
    **substring** search and paginates. That makes it the only backend that can
    answer "is there a device whose name looks like this?", which is the usual
    question.

Use ``ui`` to find devices by name and ``datalake`` when a documented, stable
contract matters.
"""

import json

from .client import ApiError, Client

DEFAULT_UI_BASE = "https://ec.europa.eu/tools/eudamed/api"

#: Documented-API parameter names mapped to this backend's spellings. Only
#: tradeName is proven; the rest follow the same camelCase convention and are
#: reported as errors by the API if wrong, rather than silently ignored.
PARAM_MAP = {
    "TRADE_NAME": "tradeName",
    # Actor name. Unproven, like most of this map; an unknown parameter is
    # either rejected with a 400 or ignored, and an ignored filter would blow
    # past MAX_PLAUSIBLE_TOTAL below rather than pass silently.
    "NAME": "name",
    "DEVICE_NAME": "deviceName",
    "BASIC_UDI": "basicUdi",
    "PRIMARY_DI": "primaryDi",
    "MF_SRN": "manufacturerSrn",
    "NOMENCLATURE_CODE": "nomenclatureCode",
}

# A tradeName filter that is ignored server-side would return the entire
# register, so an implausibly large total is treated as "filter not applied".
MAX_PLAUSIBLE_TOTAL = 20000


class UiClient(Client):
    """Paginating client for the web-UI backend."""

    SEND_FORMAT = False
    DEVICE_PATH = "/devices/udiDiData"
    ACTOR_PATH = "/actors/actorDataPublicView"
    HAS_REFERENCE = False

    def __init__(self, base=None, page_size=100, max_pages=5, language="en", **kw):
        kw.setdefault("key", "")
        super().__init__(base=base or DEFAULT_UI_BASE, **kw)
        self.page_size = page_size
        self.max_pages = max_pages
        self.language = language
        self._first_page = None

    def build_url(self, path, params=None, allow_undocumented=False):
        translated = {}
        for name, value in (params or {}).items():
            if value in (None, ""):
                continue
            translated[PARAM_MAP.get(name, name)] = value
        translated.setdefault("languageIso2Code", self.language)
        return super().build_url(path, translated, allow_undocumented=True)

    def _page(self, path, params, page):
        query = dict(params or {})
        query["page"] = page
        query["pageSize"] = self.page_size
        query["size"] = self.page_size
        return super().request(path, query, allow_undocumented=True)

    def request(self, path, params=None, allow_undocumented=False):
        """Fetch every page of a query and return (rows, last_body).

        The backend has been seen to number pages from 0 in some deployments
        and 1 in others, so the first page index is probed once. Unlike the
        predecessor script the probe is not latched across a failure, so a
        transient error cannot shift every later request by one page.
        """
        pages = (self._first_page,) if self._first_page is not None else (0, 1)
        data = body = None
        first = None
        last_error = None
        for candidate in pages:
            try:
                data, body = self._page(path, params, candidate)
            except ApiError as exc:
                last_error = exc
                continue
            first = candidate
            break
        if first is None:
            raise last_error or ApiError(f"{path} returned no usable page")
        self._first_page = first

        payload = _envelope(body)
        total = payload.get("totalElements") or 0
        if total > MAX_PLAUSIBLE_TOTAL:
            raise ApiError(
                f"{path} reported {total} results for {params!r}, which means the "
                "filter was ignored rather than applied; refusing to treat the whole "
                "register as matches")

        rows = list(data)
        page = first
        while not payload.get("last", True) and (page - first + 1) < self.max_pages:
            page += 1
            more, body = self._page(path, params, page)
            rows.extend(more)
            payload = _envelope(body)
        return rows, body

    # -- detail endpoints ----------------------------------------------
    def device_detail(self, uuid):
        rows, _ = super().request(f"/devices/udiDiData/{uuid}", {}, allow_undocumented=True)
        return rows[0] if rows else {}

    def basic_udi_detail(self, ulid):
        rows, _ = super().request(f"/devices/basicUdiData/{ulid}", {},
                                  allow_undocumented=True)
        return rows[0] if rows else {}

    def udi(self, **params):
        return self.request(self.DEVICE_PATH, params)

    def actors(self, **params):
        return self.request(self.ACTOR_PATH, params)

    def reference(self, **params):
        raise ApiError("the web-UI backend has no /reference operation; "
                       "use --backend datalake for reference codes")


def _envelope(body):
    """The pagination wrapper, for totalElements / last."""
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
