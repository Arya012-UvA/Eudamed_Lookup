"""Static facts taken from the EUDAMED Public API v1.0 OpenAPI document."""

import os

DEFAULT_BASE = "https://api.datalake.sante.service.ec.europa.eu/eudamed"

# Not a parameter in the OpenAPI document, but the APIM gateway requires it and
# the developer portal's own request template includes it. Harmless if ignored.
API_VERSION = "v1.0"

KEY_ENV = "EUDAMED_SUBSCRIPTION_KEY"
BASE_ENV = "EUDAMED_BASE"

# securitySchemes from the spec: apiKeyHeader / apiKeyQuery.
KEY_HEADER = "Ocp-Apim-Subscription-Key"
KEY_QUERY = "subscription-key"

FORMATS = ("json", "csv")

# Query parameters each operation accepts, per the spec. Anything not listed
# here is rejected before a request is sent, so a typo fails loudly and local
# rather than being silently dropped by the gateway.
UDI_PARAMS = (
    "PRIMARY_DI", "BASIC_UDI", "TRADE_NAME", "DEVICE_NAME", "DEVICE_MODEL",
    "REFERENCE", "NOMENCLATURE_CODE", "RISK_CLASS_ID", "APPLICABLE_LEGISLATION_ID",
    "PLACED_ON_THE_MARKET_ID", "MF_SRN", "SPECIAL_DEVICE_TYPE_ID", "MEDICAL_PURPOSE",
)
ACTOR_PARAMS = (
    "ACTOR_ID", "NAME", "ABBREVIATED_NAME", "ACTOR_TYPE", "CA_NAME", "CA_ACTOR_ID",
    "ACT_COUNTRY_ISO2_CODE",
)
REFERENCE_PARAMS = ("ID", "CODE", "LANGUAGE")

# Fields a device name can appear in. Filters are exact, so searching all of
# them is what makes a name search work: a device registered as
# "MindDoc: Your Companion" has DEVICE_NAME "MindDoc", which matches exactly.
DEFAULT_SEARCH_FIELDS = "TRADE_NAME,DEVICE_NAME,BASIC_UDI,PRIMARY_DI,MF_SRN"

OPERATIONS = {
    "/udi": UDI_PARAMS,
    "/actors": ACTOR_PARAMS,
    "/reference": REFERENCE_PARAMS,
}

# The spec documents no pagination parameters for any operation. Recorded here
# so the absence reads as a finding rather than an oversight.
SUPPORTS_PAGINATION = False


def default_base():
    return os.environ.get(BASE_ENV) or DEFAULT_BASE


def default_key():
    return os.environ.get(KEY_ENV) or ""
