"""Covers app/logging_config.py's behavioural contracts -- the traps that a
config snapshot test wouldn't catch: the double-log filter, uvicorn loggers
surviving configuration, and LOG_LEVEL taking effect.
"""
import importlib
import logging

from app.logging_config import MaxLevelFilter, configure_logging


def _record(level: int) -> logging.LogRecord:
    return logging.LogRecord("x", level, __file__, 1, "msg", None, None)


def test_max_level_filter_excludes_at_and_above_boundary():
    """The stdout handler uses this to avoid emitting WARNING+ (which the
    stderr handler already owns) -- without it every warning/error is logged
    twice. Boundary is strict: WARNING itself must go to stderr only."""
    below = MaxLevelFilter(logging.WARNING)
    assert below.filter(_record(logging.INFO)) is True
    assert below.filter(_record(logging.DEBUG)) is True
    assert below.filter(_record(logging.WARNING)) is False
    assert below.filter(_record(logging.ERROR)) is False


def test_configure_logging_keeps_uvicorn_loggers_enabled():
    """dictConfig defaults disable_existing_loggers to True, which would
    silence uvicorn's access/error loggers (already created by the time we
    configure). We set it False and re-point them at root -- assert they stay
    enabled and propagating so their lines still reach our handlers."""
    # Force uvicorn.access to exist, as it would under a running server.
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.disabled = True  # simulate a disable, prove we undo it

    configure_logging()

    assert uvicorn_access.disabled is False
    assert uvicorn_access.propagate is True


def _reload_with_log_level(monkeypatch, value):
    """Reload the module with LOG_LEVEL set (or, if value is None, unset) so the
    import-time constant is recomputed. Returns the reloaded module."""
    import app.logging_config as lc

    if value is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", value)
    importlib.reload(lc)
    return lc


def _restore(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    import app.logging_config as lc

    importlib.reload(lc)
    lc.configure_logging()


def test_log_level_env_sets_root_level(monkeypatch):
    """LOG_LEVEL is read at import time into the module constant, so a change
    only takes effect after reload -- assert that path works, since it's how
    the Mini would turn on DEBUG via .env."""
    lc = _reload_with_log_level(monkeypatch, "DEBUG")
    try:
        lc.configure_logging()
        assert logging.getLogger().level == logging.DEBUG
    finally:
        _restore(monkeypatch)


def test_empty_log_level_falls_back_to_info_without_crashing(monkeypatch):
    """A bare `LOG_LEVEL=` in .env loads as "" (present, not absent), which the
    old `.get(key, "INFO")` let through into dictConfig -> ValueError at startup.
    An empty value must degrade to INFO, silently (empty == "use default")."""
    lc = _reload_with_log_level(monkeypatch, "")
    try:
        assert lc.LOG_LEVEL == "INFO"
        lc.configure_logging()  # must not raise
        assert logging.getLogger().level == logging.INFO
    finally:
        _restore(monkeypatch)


def test_unknown_log_level_falls_back_to_info(monkeypatch):
    """A typo'd level would crash dictConfig the same way an empty one does;
    it must fall back to INFO too (and be surfaced, tested separately)."""
    lc = _reload_with_log_level(monkeypatch, "INFOO")
    try:
        assert lc.LOG_LEVEL == "INFO"
        lc.configure_logging()  # must not raise
        assert logging.getLogger().level == logging.INFO
    finally:
        _restore(monkeypatch)


def test_lowercase_and_padded_log_level_is_normalised(monkeypatch):
    lc = _reload_with_log_level(monkeypatch, "  debug ")
    try:
        assert lc.LOG_LEVEL == "DEBUG"
        lc.configure_logging()
        assert logging.getLogger().level == logging.DEBUG
    finally:
        _restore(monkeypatch)


def test_unknown_log_level_is_surfaced_as_a_warning(monkeypatch, capfd):
    """capfd, not caplog: configure_logging() runs dictConfig, which replaces
    the root handlers (dropping caplog's), so the warning is only observable on
    the real stderr fd the app's own StreamHandler writes to."""
    lc = _reload_with_log_level(monkeypatch, "TRACE")
    try:
        lc.configure_logging()
        err = capfd.readouterr().err
        assert "TRACE" in err and "INFO" in err
    finally:
        _restore(monkeypatch)


def test_empty_log_level_does_not_warn(monkeypatch, capfd):
    """Empty is the intended 'unset' path, not a mistake -- no noise for it."""
    lc = _reload_with_log_level(monkeypatch, "")
    try:
        lc.configure_logging()
        err = capfd.readouterr().err
        assert "not a recognised level" not in err
    finally:
        _restore(monkeypatch)
