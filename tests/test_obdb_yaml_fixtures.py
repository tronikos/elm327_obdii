r"""Tests using OBDb's real per-vehicle YAML test cases as fixtures.

These are the strongest regression tests available — real captured ECU
responses with known-good decoded values, authored by the OBDb
community. The e-Golf fixtures are bundled offline (no network needed).

Each YAML file has this shape::

    command_id: 7E5.7ED.22028C   # hdr.rax.cmd
    test_cases:
    - expected_values:
        EGOLF_HVBAT_SOC: 33.33333
      response: 7ED0462028C55     # contiguous hex (no spaces, no \\r>)
    - expected_values:
        EGOLF_HVBAT_SOC: 94.11765
      response: 7ED0462028CF0

The test loads the vehicle's ``default.json`` (signal definitions with
``fmt``), imports it via :func:`import_obdb_profile`, then for each
YAML test case:
  1. Parses the contiguous-hex response into a clean payload.
  2. Looks up the signal's ``fmt`` by signal ID.
  3. Evaluates the fmt against the clean payload.
  4. Asserts the result matches the expected value.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from elm327_obdii._core.elm327_parsing import extract_clean_payload
from elm327_obdii._core.fmt_evaluator import evaluate_fmt
from elm327_obdii.profiles.obdb import import_obdb_profile

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "obdb_egolf"


def _load_yaml_simple(path: Path) -> dict[str, Any]:
    """Load an OBDb test-case YAML file."""
    with path.open() as f:
        return yaml.safe_load(f)


def _contiguous_hex_to_bytes_response(hex_str: str, mode: str) -> bytes:
    r"""Convert a contiguous-hex YAML response to ELM327 raw bytes.

    OBDb YAML test cases may contain multi-frame ISO-TP responses with
    newlines separating frames (e.g. ``7ED1013...\\n7ED21...\\n7ED22...``).
    A real ELM327 with AT CAF1 reassembles these into a single frame
    in hardware. This helper simulates that reassembly by:

      1. Splitting on newlines (one frame per line).
      2. For each frame, stripping the 3-char CAN header.
      3. If the first frame's PCI is 0x10-0x1F (First Frame),
         concatenating subsequent frames' data (skipping their PCI
         byte 0x21, 0x22, ...) into a single payload.
      4. Formatting the result as a single ELM327 line with the header
         and a trailing prompt.

    For single-frame responses (PCI 0x00-0x0F), the response is passed
    through with just the prompt added.
    """
    lines = [ln.strip() for ln in hex_str.splitlines() if ln.strip()]

    if len(lines) == 1:
        # Single line — could be single-frame or the test case doesn't
        # use newlines. Pass through as-is.
        return hex_str.encode() + b"\r>"

    # Multi-line: reassemble ISO-TP.
    # First line: HDR + PCI + data. Subsequent: HDR + seq + data.
    parts: list[str] = []
    for i, line in enumerate(lines):
        if len(line) < 3:
            continue
        hdr = line[:3]
        rest = line[3:]
        if i == 0:
            # First frame: PCI is first byte (2 hex chars).
            # For First Frame (0x10-0x1F), skip PCI + length byte.
            if len(rest) >= 4:
                first_pci = int(rest[:2], 16)
                if 0x10 <= first_pci <= 0x1F:
                    # First Frame: skip 2-byte PCI+length, keep 6 data bytes
                    parts.append(rest[4:])
                else:
                    # Single Frame (0x0X): skip 1-byte PCI, keep rest
                    parts.append(rest[2:])
            else:
                parts.append(rest)
        # Consecutive frame: skip 1-byte sequence (0x21, 0x22, ...)
        elif len(rest) >= 2:
            parts.append(rest[2:])
        else:
            parts.append(rest)

    # Reassemble: header + first PCI (0x10 for First Frame) + all data
    # Actually, for clean payload extraction, we want the mode echo +
    # PID echo + data. The reassembled payload from OBDb has:
    #   [mode_echo, pid_hi, pid_lo, data...]
    # But our extract_clean_payload expects the dirty array format:
    #   [PCI, mode_echo, pid_hi, pid_lo, data...]
    # So we prepend a synthetic single-frame PCI.
    reassembled_data = "".join(parts)
    # Build a synthetic single-frame response: header + PCI + data
    # The PCI length = len(reassembled_data) / 2 bytes
    data_bytes = len(reassembled_data) // 2
    pci = f"{data_bytes:02X}"
    hdr = lines[0][:3] if lines else "7E0"
    synthetic = hdr + pci + reassembled_data
    return synthetic.encode() + b"\r>"


def _parse_command_id(command_id: str) -> tuple[str, str, str, str]:
    """Parse a command_id like ``7E5.7ED.22028C`` or ``7E0.0104``.

    Returns ``(hdr, rax, mode, query)``. The mode is the first 2 chars
    of the command; the query is the rest. Suffixes like ``|fc=1`` are
    stripped. Mode 01 PIDs use 2-part IDs (``hdr.cmd``); Mode 22 uses
    3-part IDs (``hdr.rax.cmd``).
    """
    base = command_id.split("|", maxsplit=1)[0]
    parts = base.split(".")
    if len(parts) == 2:
        # Mode 01 format: hdr.cmd (e.g. 7E0.0104)
        hdr, cmd = parts[0], parts[1]
        rax = ""
    elif len(parts) >= 3:
        # Mode 22 format: hdr.rax.cmd (e.g. 7E5.7ED.22028C)
        hdr, rax, cmd = parts[0], parts[1], parts[2]
    else:
        return ("", "", "", "")
    mode = cmd[:2]
    query = cmd[2:]
    return (hdr, rax, mode, query)


@pytest.fixture(scope="module")
def egolf_profile():
    """Load the e-Golf default.json and import it as a ProfileConfig."""
    default_json = json.loads((_FIXTURES_DIR / "default.json").read_text())
    matrix_signals = []
    for cmd in default_json.get("commands", []):
        for sig in cmd.get("signals", []):
            # Augment with cmd/hdr from the parent command.
            sig_copy = dict(sig)
            sig_copy["cmd"] = cmd["cmd"]
            sig_copy["hdr"] = cmd.get("hdr", "")
            sig_copy["make"] = "Volkswagen"
            sig_copy["model"] = "e-Golf"
            matrix_signals.append(sig_copy)
    return import_obdb_profile(matrix_signals, repo_default=default_json)


@pytest.fixture(scope="module")
def egolf_pid_by_id(egolf_profile):
    """Build a lookup of custom PIDs by signal ID."""
    return {p.id: p for p in egolf_profile.custom_pids}


class TestEgolfYamlTestCases:
    """Run the e-Golf YAML test cases through the fmt evaluator.

    These are real captured ECU responses with known-good expected
    values. If the fmt evaluator produces a different value, either
    the bit extraction, scaling, or clean-payload extraction is wrong.
    """

    @pytest.mark.parametrize(
        "yaml_filename",
        sorted(f.name for f in _FIXTURES_DIR.glob("*.yaml")),
    )
    def test_yaml_test_case(self, egolf_pid_by_id, yaml_filename):
        """Each YAML test case must evaluate to its expected value.

        Mode 01 test cases (7E0.01XX) are skipped — they test standard
        SAE J1979 PIDs handled by py-obdii, not our custom fmt evaluator.
        """
        yaml_data = _load_yaml_simple(_FIXTURES_DIR / yaml_filename)
        command_id = yaml_data.get("command_id", "")
        _hdr, _rax, mode, _query = _parse_command_id(command_id)

        # Skip Mode 01 — those are standard PIDs, not custom fmt.
        if mode == "01":
            pytest.skip("Mode 01 standard PID — handled by py-obdii, not fmt evaluator")

        test_cases = yaml_data.get("test_cases", [])
        if not test_cases:
            pytest.skip(f"No test cases in {yaml_filename}")

        failures = []
        for tc in test_cases:
            expected_values = tc.get("expected_values", {})
            response_hex = tc.get("response", "")
            if not response_hex or not expected_values:
                continue

            raw_response = _contiguous_hex_to_bytes_response(response_hex, mode)
            clean_payload = extract_clean_payload(raw_response, mode)

            for signal_id, expected in expected_values.items():
                pid = egolf_pid_by_id.get(signal_id)
                if pid is None:
                    failures.append(f"{signal_id}: not found in imported profile")
                    continue

                result = evaluate_fmt(clean_payload, pid.fmt)
                if result is None:
                    failures.append(
                        f"{signal_id}: evaluator returned None "
                        f"(clean={clean_payload}, fmt={pid.fmt})"
                    )
                elif isinstance(result, str):
                    if result != str(expected):
                        failures.append(
                            f"{signal_id}: got {result!r}, expected {expected!r}"
                        )
                elif abs(result - float(expected)) > 0.01:
                    failures.append(
                        f"{signal_id}: got {result}, expected {expected} "
                        f"(clean={clean_payload}, fmt={pid.fmt})"
                    )

        if failures:
            pytest.fail(
                f"{yaml_filename} had {len(failures)} failure(s):\n  "
                + "\n  ".join(failures)
            )


class TestEgolfSpecificSignals:
    """Spot-check specific signals with known values."""

    def test_hvbat_soc(self, egolf_pid_by_id):
        """EGOLF_HVBAT_SOC: 0x55 → 33.33%, 0xF0 → 94.12%."""
        pid = egolf_pid_by_id["EGOLF_HVBAT_SOC"]
        # Response 7ED0462028C55 → clean payload [0x55]
        clean = extract_clean_payload(b"7ED0462028C55\r>", "22")
        result = evaluate_fmt(clean, pid.fmt)
        assert pytest.approx(result, abs=0.01) == 33.33333

    def test_hvbat_volts(self, egolf_pid_by_id):
        """EGOLF_HVBAT_VOLTS: 0x0531 → 332.25V."""
        pid = egolf_pid_by_id["EGOLF_HVBAT_VOLTS"]
        clean = extract_clean_payload(b"7ED05621E3B0531\r>", "22")
        result = evaluate_fmt(clean, pid.fmt)
        assert pytest.approx(result, abs=0.01) == 332.25

    def test_hvbat_current(self, egolf_pid_by_id):
        """EGOLF_HVBAT_CURRENT: 0x07EB → -4.25A."""
        pid = egolf_pid_by_id["EGOLF_HVBAT_CURRENT"]
        clean = extract_clean_payload(b"7ED05621E3D07EB\r>", "22")
        result = evaluate_fmt(clean, pid.fmt)
        assert pytest.approx(result, abs=0.01) == -4.25

    def test_hvbat_capacity(self, egolf_pid_by_id):
        """EGOLF_HVBAT_CAP: 0x011E0400 → 14.30 kWh."""
        pid = egolf_pid_by_id["EGOLF_HVBAT_CAP"]
        clean = extract_clean_payload(b"77A07622AB2011E0400\r>", "22")
        result = evaluate_fmt(clean, pid.fmt)
        assert pytest.approx(result, abs=0.01) == 14.30024

    def test_odometer(self, egolf_pid_by_id):
        """EGOLF_ODO: 24-bit value from PID 2203."""
        pid = egolf_pid_by_id["EGOLF_ODO"]
        # Need a test case with a known odometer value — check the YAML
        yaml_data = _load_yaml_simple(_FIXTURES_DIR / "714.77E.222203.yaml")
        tc = yaml_data["test_cases"][0]
        clean = extract_clean_payload(tc["response"].encode() + b"\r>", "22")
        result = evaluate_fmt(clean, pid.fmt)
        expected = tc["expected_values"]["EGOLF_ODO"]
        assert pytest.approx(result, abs=1) == expected
