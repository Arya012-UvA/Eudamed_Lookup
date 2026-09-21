# Legacy: UI-backend script

`eudamed_lookup.py` is the original single-file script. It does **not** use the
official EUDAMED Public API — it calls the undocumented internal backend of the
EUDAMED web UI (`ec.europa.eu/tools/eudamed/api`), with camelCase parameters and
`/devices/udiDiData` paths that appear in no published specification.

It is kept here because it needs no subscription key, which the official API
does. Be aware that it:

- can break without notice, since the endpoint is unversioned and undocumented;
- scores manufacturer-name matches as strongly as trade-name matches, so a
  device can be reported as `found` when only its *manufacturer* name matched
  (searching `MindDoc` reports the unrelated device `Moodpath` at 0.90, because
  the manufacturer is `MindDoc Health GmbH`). The replacement in `eudamed/`
  fixes this;
- reports a misleading `page=0 and page=1` error for every failure cause,
  including network and auth problems.

Prefer the `eudamed` package in the repository root. Run these tests with:

    pytest legacy/test_eudamed_lookup.py
