"""Pytest configuration for the elm327_obdii library tests."""


def pytest_addoption(parser):
    """Add a --run-network flag to enable live network tests."""
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="Run tests that require network access (e.g. live profile fetch)",
    )


def pytest_configure(config):
    """Register the 'network' marker."""
    config.addinivalue_line(
        "markers",
        "network: tests that require network access (deselected by default)",
    )
