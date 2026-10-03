"""Persistence for MazCro: macro JSON files and a rotating error log.

Macros live in ``~/macros`` (created on demand) as one JSON file per macro.
Errors are appended to ``~/macro_errors.log`` and rotated so that only the five
most recent copies are kept, as required by the specification.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from macro_model import Macro, now_iso, safe_filename

#: Number of rotated log files to retain.
LOG_BACKUP_COUNT = 5
#: Size in bytes at which the log rotates.
LOG_MAX_BYTES = 2 * 1024 * 1024

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def macros_dir() -> Path:
    """Return the macro directory, creating it if necessary.

    Uses ``MAZCRO_MACROS_DIR`` when set so tests and portable installs can
    redirect storage without touching the user's home directory.
    """
    override = os.environ.get("MAZCRO_MACROS_DIR")
    directory = Path(override).expanduser() if override else Path.home() / "macros"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to the current working directory rather than failing hard.
        fallback = Path.cwd() / "macros"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return directory


def error_log_path() -> Path:
    """Return the path of the error log file."""
    override = os.environ.get("MAZCRO_LOG_FILE")
    return Path(override).expanduser() if override else Path.home() / "macro_errors.log"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return the shared MazCro logger, configuring it on first use.

    Reconfigures if the target path changed underneath us, so changing
    ``MAZCRO_LOG_FILE`` (tests, portable installs) actually takes effect
    instead of silently writing to the original location.
    """
    global _logger
    with _LOCK:
        logger = logging.getLogger("mazcro")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        wanted = str(error_log_path())
        if _logger is not None and getattr(_logger, "_mazcro_path", None) == wanted:
            return _logger

        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            handler: logging.Handler = RotatingFileHandler(
                wanted,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            )
            logger.addHandler(handler)
        except OSError:
            # Logging must never be the reason the app fails to start.
            logger.addHandler(logging.NullHandler())
        else:
            # Also mirror to stderr for interactive debugging.
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            logger.addHandler(console)

        logger._mazcro_path = wanted  # type: ignore[attr-defined]
        _logger = logger
        return logger


def log_info(message: str) -> None:
    get_logger().info(message)


def log_warning(message: str) -> None:
    get_logger().warning(message)


def log_error(message: str) -> None:
    get_logger().error(message)


def log_exception(message: str) -> None:
    """Log ``message`` together with the active exception traceback."""
    get_logger().exception(message)


# ---------------------------------------------------------------------------
# Macro storage
# ---------------------------------------------------------------------------
def macro_path(name: str) -> Path:
    """Return the file path a macro with ``name`` would be stored at."""
    return macros_dir() / f"{safe_filename(name)}.json"


def save_macro(macro: Macro) -> Path:
    """Write ``macro`` to disk atomically and return its path.

    The write goes to a temporary file first and is then moved into place, so
    an interrupted save can never leave a truncated macro behind.
    """
    if not macro.name.strip():
        raise ValueError("macro name cannot be empty")

    macro.last_modified = now_iso()
    target = macro_path(macro.name)
    temp = target.with_suffix(".json.tmp")

    with _LOCK:
        try:
            payload = macro.to_dict()
            temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, target)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            log_exception(f"failed to save macro {macro.name!r}")
            raise RuntimeError(f"could not save macro: {exc}") from exc

    log_info(f"saved macro {macro.name!r} -> {target}")
    return target


def load_macro(name: str) -> Macro:
    """Load a macro by name.

    Raises FileNotFoundError if the macro does not exist and ValueError if the
    file contains malformed JSON.
    """
    path = macro_path(name)
    if not path.exists():
        raise FileNotFoundError(f"no macro named {name!r} in {macros_dir()}")

    with _LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log_exception(f"macro file {path.name} contains invalid JSON")
            raise ValueError(f"macro file is corrupt: {exc}") from exc
        except OSError as exc:
            log_exception(f"could not read macro file {path}")
            raise RuntimeError(f"could not read macro: {exc}") from exc

    try:
        macro = Macro.from_dict(raw)
    except (ValueError, TypeError) as exc:
        log_exception(f"macro file {path.name} failed validation")
        raise ValueError(str(exc)) from exc

    log_info(f"loaded macro {macro.name!r} ({len(macro.actions)} actions)")
    return macro


def delete_macro(name: str) -> bool:
    """Delete a macro file. Returns True if a file was removed."""
    path = macro_path(name)
    with _LOCK:
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            log_exception(f"could not delete macro {name!r}")
            raise RuntimeError(f"could not delete macro: {exc}") from exc
    log_info(f"deleted macro {name!r}")
    return True


def rename_macro(old_name: str, macro: Macro) -> Path:
    """Persist ``macro`` under its current name and delete the old file."""
    save_macro(macro)
    if macro_path(old_name) != macro_path(macro.name):
        delete_macro(old_name)
    return macro_path(macro.name)


def list_macros() -> list[str]:
    """Return the names of every saved macro, sorted alphabetically."""
    directory = macros_dir()
    try:
        entries: list[Path] = sorted(directory.glob("*.json"))
    except OSError:
        log_exception("could not enumerate the macro directory")
        return []

    names: list[str] = []
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name")
            names.append(str(name) if name else path.stem)
        except (json.JSONDecodeError, OSError, AttributeError):
            # A corrupt file should not hide the healthy macros beside it.
            log_warning(f"skipping unreadable macro file {path.name}")
            names.append(path.stem)
    return sorted(set(names))


def iter_macros() -> Iterator[Macro]:
    """Yield every loadable macro, logging and skipping broken files."""
    for name in list_macros():
        try:
            yield load_macro(name)
        except (ValueError, RuntimeError, FileNotFoundError):
            continue


def export_macro(macro: Macro, destination: str | os.PathLike[str]) -> Path:
    """Write a macro to an arbitrary path (used by the GUI Save As)."""
    path = Path(destination).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(macro.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log_exception(f"could not export macro {macro.name!r}")
        raise RuntimeError(f"could not export macro: {exc}") from exc
    log_info(f"exported macro {macro.name!r} -> {path}")
    return path


def import_macro(source: str | os.PathLike[str], name: str | None = None) -> Macro:
    """Load a macro from an arbitrary path and save it into the macro store."""
    path = Path(source).expanduser()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a valid macro file: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc

    macro = Macro.from_dict(raw)
    if name:
        macro.name = name
    save_macro(macro)
    return macro


def macro_exists(name: str) -> bool:
    """Return True if a macro with ``name`` is saved."""
    return macro_path(name).exists()


def stats() -> dict[str, Any]:
    """Return a small summary used by the GUI status area."""
    names = list_macros()
    return {
        "count": len(names),
        "directory": str(macros_dir()),
        "log": str(error_log_path()),
        "names": names,
        "logged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
