"""Profile importer interface.

Each upstream profile format (WiCAN JSON, future Torque CSV, future
RealDash XML) gets one importer module that knows the upstream schema
shape. Everything downstream works only with the
:class:`elm327_obdii.ProfileConfig` produced by the importer, never
with the raw upstream dict.
"""

from typing import Protocol

from .._core.schema import ProfileConfig


class ProfileImporter(Protocol):
    """An importer translates some external profile format into ProfileConfig.

    Implementations are responsible for:

      - **Reverse de-duplication**: any parameter that maps to a
        standard Mode 01 PID must be promoted to ``standard_pids`` and
        dropped from ``custom_pids``. The match is on address (mode +
        query hex), not on formula text - see the wican importer for
        a concrete implementation.

      - **Formula translation**: the upstream notation (e.g. WiCAN's
        ``[B5:B6]``) is converted to canonical notation (``B(5, 6)``).

      - **Header/init extraction**: the upstream's init-strings are
        parsed into the structured ``can_header`` / ``can_filter`` /
        ``init_extra`` fields on :class:`CustomPid`, so the scheduler
        can group on them as real values.
    """

    def can_handle(self, raw: object) -> bool:
        """Return True if this importer recognizes the input shape.

        Used by a registry/dispatcher that tries multiple importers
        against the same raw input. Cheap structural check only - do
        not validate the full payload here.
        """

    def import_profile(self, raw: object) -> ProfileConfig:
        """Translate ``raw`` into a :class:`ProfileConfig`.

        Raises :class:`ValueError` if ``raw`` is not a shape this
        importer can handle. Should never raise on a single bad PID -
        skip it and continue, so one malformed entry doesn't lose the
        whole profile.
        """
