"""The logosctl test suite.

While both CLIs ship, both get tested. This directory is the `logosctl`
half, driving the `logosctl` package; `tests/unit`, `tests/integration`
and `tests/docker_smoke` are the `logoscore` half and stay frozen against
`logoscore`'s surface.

The two are deliberately duplicates rather than one parameterised suite —
the same call the CLI repo made for `tests/test_cli_logoscore.cpp`. The
surfaces genuinely differ (no configuration flags, no `LOGOSCORE_CLIENT_*`
env family, grouped subcommands, a config document installed before the
daemon boots), so sharing would mean a pile of branches on which client is
being driven. Keeping them apart also means retiring either one is a
delete — this directory and `src/logosctl`, or those three and
`src/logoscore` — instead of an unpick.

So: do not refactor the two suites back together.
"""
