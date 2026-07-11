# OBDb e-Golf Test Fixtures

These fixture files are sourced from the [OBDb Volkswagen e-Golf repository](https://github.com/OBDb/Volkswagen-e-Golf)
and are licensed under [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

## Attribution

- Source: <https://github.com/OBDb/Volkswagen-e-Golf>
- License: CC-BY-SA 4.0 International
- Files:
  - `default.json` — signal definitions (from `signalsets/v3/default.json`)
  - `*.yaml` — test cases with real ECU responses and expected values (from `tests/test_cases/2016/commands/`)

These files are used for offline regression testing of the OBDb profile importer
and fmt evaluator. They are not used at runtime by the library.
