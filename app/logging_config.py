"""Central logging setup for the app and the discovery CLI.

Until this module existed, nothing configured the root logger and uvicorn
only attached handlers to its own loggers -- so any `logging.getLogger(...)`
call elsewhere was swallowed by `logging.lastResort` (WARNING+ to stderr,
unformatted). `configure_logging()` fixes that once, at startup.

Two things about *this* app shape the config and must not be "cleaned up":

1. It writes to stdout/stderr only -- never to a file. Under launchd (see
   scripts/com.assetmgt.app.plist) stdout/stderr are already captured into
   logs/app.log / logs/app.error.log, and scripts/rotate-logs.sh rotates
   those by copy-then-truncate *because* launchd holds an open O_APPEND fd on
   their inodes. A Python FileHandler/RotatingFileHandler pointed at either
   path would rename-rotate it and silently break launchd's writer (see
   AGENTS.md "Log rotation"). So: StreamHandler, nothing else.

2. WARNING+ goes to stderr, everything below to stdout. The README/AGENTS
   troubleshooting steps all say "check logs/app.error.log" for problems, and
   uncaught tracebacks already land there -- keeping the split preserves that,
   and keeps routine INFO/access lines out of the error log.
"""
import logging
import logging.config
import os

# `or "INFO"`, not `.get("LOG_LEVEL", "INFO")`: python-dotenv loads a bare
# `LOG_LEVEL=` line as "" (present, not absent), which slips past the default
# and would crash dictConfig with "Unable to configure root logger" at startup
# -- a boot failure from an empty optional var. An unknown level (a typo like
# "INFOO") crashes it the same way, so fall back for that too and record it, so
# a bad value degrades to INFO with a breadcrumb instead of taking the app down.
# (assistant.py's ANTHROPIC_MODEL already uses this `or` idiom for the same
# reason.)
_VALID_LEVELS = {"CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"}
_requested_level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
LOG_LEVEL = _requested_level if _requested_level in _VALID_LEVELS else "INFO"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


class MaxLevelFilter(logging.Filter):
    """Passes only records strictly below `level`. The stdlib has a minimum
    level on handlers but no maximum, so without this the stdout handler would
    also emit WARNING+ and every warning/error would be logged twice (once on
    stdout, once on stderr). Configured via dictConfig's `()` factory key."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.level


def configure_logging() -> None:
    """Install the app-wide logging config. Idempotent -- safe to call from
    both the FastAPI startup hook and the CLI callback."""
    logging.config.dictConfig(
        {
            "version": 1,
            # uvicorn's loggers already exist by the time this runs; the
            # dictConfig default of True would disable them and kill the
            # access log.
            "disable_existing_loggers": False,
            "filters": {
                "below_warning": {
                    "()": "app.logging_config.MaxLevelFilter",
                    "level": logging.WARNING,
                },
            },
            "formatters": {"plain": {"format": _FORMAT}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "plain",
                    "filters": ["below_warning"],
                },
                "stderr": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "plain",
                    "level": "WARNING",
                },
            },
            # LOG_LEVEL is already validated above (never empty, never unknown),
            # so dictConfig can't reject it.
            "root": {"handlers": ["stdout", "stderr"], "level": LOG_LEVEL},
            "loggers": {
                # Route uvicorn's own records through root so they pick up our
                # timestamped format and the stdout/stderr split. Empty
                # handler list + propagate=True means "let root handle it".
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": True},
                # Noise floors: at LOG_LEVEL=DEBUG we want app + httpx detail,
                # but SQLAlchemy's engine echo and httpcore's frame logging are
                # a firehose that drowns it. Pin them to WARNING regardless.
                "sqlalchemy.engine": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
            },
        }
    )
    # A bare `LOG_LEVEL=` degrades silently to INFO (that's the intended
    # "unset" behaviour), but a non-empty typo is worth surfacing now that
    # logging is up -- it lands in stderr / logs/app.error.log.
    if _requested_level not in _VALID_LEVELS:
        logging.getLogger(__name__).warning(
            "LOG_LEVEL=%r is not a recognised level; using INFO", _requested_level
        )
