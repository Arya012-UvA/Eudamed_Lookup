"""Normalised views over /udi and /actors rows.

Field names come from the spec's query parameters where possible, since a
filterable column is very likely present in the response under the same name.
Everything else is a list of plausible spellings; the first populated one wins.
The untouched source row is kept as .raw.
"""

from . import devicetype
from .fields import index_row, pick


def coded(value):
    """Flatten a coded field to its short label.

    The two backends encode these differently. The datalake API returns a
    numeric ``*_ID`` column that /reference resolves. The web-UI backend
    instead nests the code, ``{"code": "RISK_CLASS.IIA"}`` — stringifying that
    dict leaks ``{'code': 'RISK_CLASS.IIA'}`` into reports, so unwrap it to
    the part after the last dot.
    """
    if isinstance(value, dict):
        raw = value.get("code") or value.get("CODE") or ""
        return str(raw).rsplit(".", 1)[-1] if raw else ""
    return "" if value is None else str(value)


UI_DEVICE = "https://ec.europa.eu/tools/eudamed/#/screen/search-device/{uuid}"
UI_SEARCH = "https://ec.europa.eu/tools/eudamed/#/screen/search-device?deviceIdentifier={di}"


class Device:
    """One /udi row."""

    def __init__(self, raw):
        self.raw = raw or {}
        i = index_row(self.raw)
        self.trade_name = str(pick(i, "TRADE_NAME", "tradeName"))
        self.device_name = str(pick(i, "DEVICE_NAME", "deviceName"))
        self.device_model = str(pick(i, "DEVICE_MODEL", "deviceModel"))
        self.primary_di = str(pick(i, "PRIMARY_DI", "primaryDi", "primaryDiCode"))
        self.basic_udi = str(pick(i, "BASIC_UDI", "basicUdi", "basicUdiDiCode"))
        self.reference = str(pick(i, "REFERENCE", "referenceNumber"))
        self.nomenclature_code = str(pick(i, "NOMENCLATURE_CODE", "nomenclatureCode", "emdnCode"))
        self.medical_purpose = str(pick(i, "MEDICAL_PURPOSE", "medicalPurpose"))
        self.mf_srn = str(pick(i, "MF_SRN", "mfSrn", "manufacturerSrn"))
        # Not query-filterable, so the spelling is a guess; kept wide.
        self.manufacturer_name = str(pick(
            i, "MF_NAME", "MANUFACTURER_NAME", "manufacturerName", "mfName", "actorName"))
        self.risk_class = coded(pick(i, "RISK_CLASS", "riskClass", "RISK_CLASS_CODE"))
        self.risk_class_id = pick(i, "RISK_CLASS_ID", "riskClassId", default=None)
        self.legislation = coded(pick(i, "APPLICABLE_LEGISLATION", "applicableLegislation"))
        self.legislation_id = pick(i, "APPLICABLE_LEGISLATION_ID",
                                   "applicableLegislationId", default=None)
        # PLACED_ON_THE_MARKET_ID resolves to a COUNTRY ("Israel") in the live
        # /reference table, not a status. The device's market status is the
        # separate DEVICE_STATUS_TYPE_ID column.
        self.placed_on_market = coded(pick(i, "PLACED_ON_THE_MARKET", "placedOnTheMarket"))
        self.placed_on_market_id = pick(i, "PLACED_ON_THE_MARKET_ID",
                                        "placedOnTheMarketId", default=None)
        self.device_status = coded(pick(i, "DEVICE_STATUS_TYPE", "deviceStatusType"))
        self.device_status_id = pick(i, "DEVICE_STATUS_TYPE_ID", "deviceStatusTypeId",
                                     "STATUS_ID", default=None)
        self.special_type = coded(pick(i, "SPECIAL_DEVICE_TYPE", "specialDeviceType"))
        self.special_type_id = pick(i, "SPECIAL_DEVICE_TYPE_ID",
                                    "specialDeviceTypeId", default=None)
        self.uuid = str(pick(i, "UUID", "uuid", "id", "udiDiDataUuid"))
        self.latest_version = pick(i, "LATEST_VERSION", "latestVersion", default=None)
        self.version = pick(i, "VERSION_NUMBER", "versionNumber", "VERSION", default=None)
        # Further real columns, from the live field list.
        self.secondary_di = str(pick(i, "SECONDARY_DI", "secondaryDi"))
        self.authorised_rep = str(pick(i, "AR_NAME", "arName"))
        self.authorised_rep_srn = str(pick(i, "AR_SRN", "arSrn"))
        self.active = pick(i, "ACTIVE", "active", default=None)
        self.implantable = pick(i, "IMPLANTABLE", "implantable", default=None)

    @property
    def kind(self):
        """('software' | 'other' | 'unknown', reason) per the record itself.

        Derived, not a registry column, so it lives here rather than being
        mapped from a field. See eudamed.devicetype for the reasoning.
        """
        return devicetype.classify(self)

    @property
    def country(self):
        """Manufacturer country, inferred from the SRN prefix (e.g. DE-MF-...)."""
        return self.mf_srn[:2].upper() if len(self.mf_srn) >= 2 else ""

    @property
    def link(self):
        """Best available human link. Falls back to a UDI-DI search when the
        API returns no UUID - the device-screen URL needs one."""
        if self.uuid:
            return UI_DEVICE.format(uuid=self.uuid)
        if self.primary_di:
            return UI_SEARCH.format(di=self.primary_di)
        return ""

    def to_dict(self):
        kind, reason = self.kind
        return {
            "trade_name": self.trade_name, "device_name": self.device_name,
            "device_model": self.device_model, "manufacturer_name": self.manufacturer_name,
            "mf_srn": self.mf_srn, "manufacturer_country": self.country,
            "primary_di": self.primary_di, "basic_udi": self.basic_udi,
            "reference": self.reference, "nomenclature_code": self.nomenclature_code,
            "risk_class": self.risk_class, "legislation": self.legislation,
            "placed_on_market": self.placed_on_market, "device_status": self.device_status,
            "special_type": self.special_type, "secondary_di": self.secondary_di,
            "authorised_rep": self.authorised_rep, "authorised_rep_srn": self.authorised_rep_srn,
            "active": self.active,
            "medical_purpose": self.medical_purpose, "version": self.version,
            "latest_version": self.latest_version, "uuid": self.uuid, "link": self.link,
            "device_kind": kind, "device_kind_reason": reason,
        }

    def identity(self):
        """Key for de-duplicating the same device seen via different queries."""
        for value in (self.uuid, self.primary_di, self.basic_udi):
            if value:
                return value
        return f"{self.trade_name}|{self.mf_srn}|{self.device_name}"


class Actor:
    """One /actors row."""

    #: Set to "ui-substring" when this actor was reached through the
    #: undocumented substring backend rather than by an exact name match.
    matched_via = ""

    def __init__(self, raw):
        self.raw = raw or {}
        i = index_row(self.raw)
        self.actor_id = str(pick(i, "ACTOR_ID", "actorId", "srn"))
        self.name = str(pick(i, "NAME", "name", "actorName"))
        self.abbreviated_name = str(pick(i, "ABBREVIATED_NAME", "abbreviatedName"))
        self.actor_type = str(pick(i, "ACTOR_TYPE", "actorType"))
        self.ca_name = str(pick(i, "CA_NAME", "caName"))
        self.ca_actor_id = str(pick(i, "CA_ACTOR_ID", "caActorId"))
        self.country = str(pick(i, "ACT_COUNTRY_ISO2_CODE", "actCountryIso2Code",
                                "countryIso2Code")).upper()

    def to_dict(self):
        return {
            "actor_id": self.actor_id, "name": self.name,
            "abbreviated_name": self.abbreviated_name, "actor_type": self.actor_type,
            "country": self.country, "ca_name": self.ca_name, "ca_actor_id": self.ca_actor_id,
            "matched_via": self.matched_via,
        }
