"""Profile importers for WiCAN and OBDb.

No built-in profiles are shipped — the integration fetches from
upstream repositories at runtime:

  - **WiCAN**: ``meatpiHQ/wican-fw/vehicle_profiles.json`` (GPL-3.0)
  - **OBDb**: ``obdb.community/data/matrix_data.json`` + per-vehicle
    repos (Creative Commons)

Nothing from the source JSON is persisted or redistributed; only the
resulting :class:`ProfileConfig` is stored in the user's HA config
entry.
"""
