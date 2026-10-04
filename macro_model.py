"""Data structures for MazCro macros.

Defines the :class:`Action`, :class:`Variable` and :class:`Macro` models plus the
JSON (de)serialisation used by :mod:`macro_store`.

Every model is a plain dataclass so it can be converted to and from ``dict``
without pulling in a serialisation dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Matches "${name}" with an optional surrounding whitespace inside the braces.
VARIABLE_PATTERN = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")


def now_iso() -> str:
    """Return the current local time as an ISO-8601 string."""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Variable:
    """A named value that can be substituted into ``type`` actions."""

    name: str
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Variable":
        return cls(name=str(raw.get("name", "")), value=str(raw.get("value", "")))


@dataclass
class Action:
    """A single macro step.

    ``kind`` is one of:

    ``wait``         -- pause for ``duration`` seconds
    ``click``        -- click at (x, y); ``button`` is left/right/middle
    ``mouse_click``  -- alias of ``click`` used by the manual Add Action panel
    ``double_click`` -- two clicks at (x, y)
    ``move``         -- move the pointer to (x, y) over ``duration`` seconds
    ``type``         -- type ``text`` (with ``${var}`` substitution)
    ``text_input``   -- type ``text`` with ``delay_per_char`` ms between chars
    ``key``          -- tap ``key`` once
    ``hold_key``     -- hold ``key`` down for ``hold_time`` milliseconds
    ``key_press``    -- press ``key`` with ``modifiers`` held for ``hold_time``
    ``scroll``       -- scroll ``amount`` clicks vertically at (x, y)
    ``screenshot``  -- capture the screen to ``path``

    ``delay`` is the recorded gap *before* this action, in seconds. Playback
    divides it by the speed factor, so a 2.0x macro runs twice as fast.
    """

    kind: str
    delay: float = 0.0
    x: int = 0
    y: int = 0
    button: str = "left"
    text: str = ""
    key: str = ""
    duration: float = 0.0
    hold_time: int = 50
    amount: int = 0
    path: str = ""
    #: Modifier keys held down around a ``key_press`` (ctrl/shift/alt/win).
    modifiers: list[str] = field(default_factory=list)
    #: Milliseconds between characters for ``text_input``.
    delay_per_char: int = 50

    #: Kinds that move the pointer and therefore need bounds checking.
    POSITIONAL: tuple[str, ...] = field(
        default=("click", "mouse_click", "double_click", "move", "scroll"),
        init=False,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dict, omitting fields that are unused.

        Keeping the payload minimal matters because it makes saved macros
        readable and diff-friendly in git.
        """
        data: dict[str, Any] = {"type": self.kind, "delay": round(self.delay, 4)}

        if self.kind in self.POSITIONAL or self.kind == "screenshot":
            data["x"] = self.x
            data["y"] = self.y
        if self.kind in ("click", "mouse_click", "double_click"):
            data["button"] = self.button
        if self.kind in ("click", "mouse_click", "key", "hold_key", "key_press", "double_click"):
            data["hold_time"] = int(self.hold_time)
        if self.kind in ("move", "wait"):
            data["duration"] = round(float(self.duration), 4)
        if self.kind in ("type", "text_input"):
            data["text"] = self.text
        if self.kind in ("key", "hold_key", "key_press"):
            data["key"] = self.key
        if self.kind == "key_press":
            data["modifiers"] = list(self.modifiers)
        if self.kind == "text_input":
            data["delay_per_char"] = int(self.delay_per_char)
        if self.kind == "scroll":
            data["amount"] = int(self.amount)
        if self.kind == "screenshot":
            data["path"] = self.path
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Action":
        """Build an Action from a JSON dict, tolerating unknown keys."""
        kind = str(raw.get("type", raw.get("kind", ""))).strip().lower()
        if not kind:
            raise ValueError("action is missing a 'type' field")
        # "delay" is MazCro's own unit (seconds). "sleep_before" is the unit used
        # by the hand-authored schema, where every delay is milliseconds. Accept
        # both so macros written by hand load without conversion.
        if raw.get("sleep_before") is not None:
            delay = float(raw.get("sleep_before", 0) or 0) / 1000.0
        else:
            delay = float(raw.get("delay", 0.0) or 0.0)

        raw_modifiers = raw.get("modifiers", []) or []
        if isinstance(raw_modifiers, str):
            raw_modifiers = [raw_modifiers]
        modifiers = [str(m).strip().lower() for m in raw_modifiers if str(m).strip()]

        return cls(
            kind=kind,
            delay=delay,
            x=int(raw.get("x", 0) or 0),
            y=int(raw.get("y", 0) or 0),
            button=str(raw.get("button", "left") or "left"),
            text=str(raw.get("text", "") or ""),
            key=str(raw.get("key", "") or ""),
            duration=float(raw.get("duration", 0.0) or 0.0),
            hold_time=int(raw.get("hold_time", 50) or 50),
            amount=int(raw.get("amount", 0) or 0),
            path=str(raw.get("path", "") or ""),
            modifiers=modifiers,
            delay_per_char=int(raw.get("delay_per_char", 50) or 50),
        )

    def substitute(self, variables: dict[str, str]) -> "Action":
        """Return a copy with ``${var}`` placeholders replaced.

        Unknown placeholders are left untouched so the user can see what is
        missing rather than silently losing text.
        """
        if self.kind not in ("type", "text_input") or "${" not in self.text:
            return self
        self.text = VARIABLE_PATTERN.sub(
            lambda m: variables.get(m.group(1), m.group(0)), self.text
        )
        return self

    def placeholders(self) -> list[str]:
        """Return the ``${name}`` names this action refers to, in order."""
        if self.kind not in ("type", "text_input") or "${" not in self.text:
            return []
        return [m.group(1) for m in VARIABLE_PATTERN.finditer(self.text)]

    def describe(self) -> str:
        """Return a short human-readable summary for the GUI list."""
        if self.kind == "wait":
            return f"Wait {self.duration:.2f}s"
        if self.kind in ("click", "mouse_click"):
            return f"Click {self.button} ({self.x}, {self.y}) hold {self.hold_time}ms"
        if self.kind == "double_click":
            return f"Double click ({self.x}, {self.y})"
        if self.kind == "move":
            return f"Move to ({self.x}, {self.y}) over {self.duration:.2f}s"
        if self.kind == "type":
            preview = self.text if len(self.text) <= 40 else self.text[:37] + "..."
            return f"Type {preview!r}"
        if self.kind == "text_input":
            preview = self.text if len(self.text) <= 32 else self.text[:29] + "..."
            return f"Text {preview!r} ({self.delay_per_char}ms/char)"
        if self.kind == "key":
            return f"Key {self.key}"
        if self.kind == "hold_key":
            return f"Hold {self.key} for {self.hold_time}ms"
        if self.kind == "key_press":
            combo = "+".join([*self.modifiers, self.key])
            return f"Key {combo} hold {self.hold_time}ms"
        if self.kind == "scroll":
            return f"Scroll {self.amount} at ({self.x}, {self.y})"
        if self.kind == "screenshot":
            return f"Screenshot -> {self.path or '(auto)'}"
        return self.kind


@dataclass
class WindowTarget:
    """Identifies the window a macro belongs to.

    ``title_pattern`` is treated as a regular expression when resolving a
    window, which lets a macro match several windows of the same application
    (for example every "Untitled - Notepad" document).
    """

    process_name: str = ""
    title: str = ""
    title_pattern: str = ""
    handle: int = 0

    def matches_title(self, candidate: str) -> bool:
        """Return True if ``candidate`` satisfies this target's title rule.

        Falls back to a substring test when the pattern is not valid regex,
        so a literal title always works even if it contains regex
        metacharacters.
        """
        if not self.title_pattern:
            if not self.title:
                return True
            return self.title.lower() in candidate.lower()
        try:
            return re.search(self.title_pattern, candidate, re.IGNORECASE) is not None
        except re.error:
            return self.title_pattern.lower() in candidate.lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_name": self.process_name,
            "title": self.title,
            "title_pattern": self.title_pattern,
            "handle": self.handle,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "WindowTarget":
        raw = raw or {}
        return cls(
            process_name=str(raw.get("process_name", "") or ""),
            title=str(raw.get("title", "") or ""),
            title_pattern=str(raw.get("title_pattern", "") or ""),
            handle=int(raw.get("handle", 0) or 0),
        )


@dataclass
class Macro:
    """A named, persistable sequence of actions bound to a target window."""

    name: str = "Untitled macro"
    target: WindowTarget = field(default_factory=WindowTarget)
    #: Additional accepted target windows. Playback is allowed if ANY of
    #: these (or the primary ``target``) is open and matches.
    extra_targets: list[WindowTarget] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    created: str = field(default_factory=now_iso)
    last_modified: str = field(default_factory=now_iso)
    playback_speed: float = 1.0

    # ---------------------------------------------------------------- helpers
    def variable_map(self) -> dict[str, str]:
        """Return ``{name: value}`` for every variable."""
        return {v.name: v.value for v in self.variables}

    def set_variable(self, name: str, value: str) -> None:
        """Create or update a variable, preserving declaration order."""
        for existing in self.variables:
            if existing.name == name:
                existing.value = value
                return
        self.variables.append(Variable(name=name, value=value))
        self.last_modified = now_iso()

    def remove_variable(self, name: str) -> None:
        self.variables = [v for v in self.variables if v.name != name]
        self.last_modified = now_iso()

    def add_action(self, action: Action) -> None:
        self.actions.append(action)
        self.last_modified = now_iso()

    def clear_actions(self) -> None:
        self.actions.clear()
        self.last_modified = now_iso()

    def expected_duration(self, speed: float | None = None) -> float:
        """Estimate total runtime in seconds, honouring the speed factor."""
        factor = speed if speed and speed > 0 else max(self.playback_speed, 0.01)
        total = 0.0
        for action in self.actions:
            total += action.delay
            if action.kind == "wait":
                total += action.duration
            elif action.kind == "move":
                total += action.duration
            elif action.kind in ("hold_key", "key_press"):
                total += action.hold_time / 1000.0
            elif action.kind == "text_input":
                total += len(action.text) * action.delay_per_char / 1000.0
            elif action.kind in ("type", "click", "mouse_click"):
                # Rough but bounded estimate for per-character / per-click cost.
                total += len(action.text) * 0.01 if action.kind == "type" else 0.05
        return total / factor

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target_app": self.target.process_name,
            "target_window_title": self.target.title,
            "created": self.created,
            "last_modified": self.last_modified,
            "playback_speed": round(self.playback_speed, 3),
            "window": self.target.to_dict(),
            "extra_windows": [w.to_dict() for w in self.extra_targets],
            "variables": {v.name: v.value for v in self.variables},
            "variable_list": [v.to_dict() for v in self.variables],
            "actions": [a.to_dict() for a in self.actions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Macro":
        """Rebuild a Macro from JSON, raising ValueError on malformed input."""
        name = str(raw.get("name", "") or "").strip()
        if not name:
            raise ValueError("macro is missing a 'name' field")

        # Both the documented flat schema (target_app / target_window_title)
        # and a nested "window" object are accepted.
        target_raw = raw.get("window")

        actions: list[Action] = []
        raw_actions = raw.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("macro 'actions' must be a list")
        for entry in raw_actions:
            if isinstance(entry, dict):
                actions.append(Action.from_dict(entry))

        variables: list[Variable] = []
        variable_map = raw.get("variables")
        if isinstance(variable_map, dict):
            variables = [Variable(name=str(k), value=str(v)) for k, v in variable_map.items()]
        elif isinstance(variable_map, list):
            variables = [Variable.from_dict(v) for v in variable_map if isinstance(v, dict)]
        for entry in raw.get("variable_list", []) or []:
            if isinstance(entry, dict):
                var = Variable.from_dict(entry)
                if var.name and not any(v.name == var.name for v in variables):
                    variables.append(var)

        target_app = str(raw.get("target_app", "") or "")
        target_title = str(raw.get("target_window_title", "") or "")
        target_pattern = str(raw.get("target_window_title_pattern", "") or "")

        extras: list[WindowTarget] = []
        for entry in raw.get("extra_windows", []) or []:
            if isinstance(entry, dict):
                extras.append(WindowTarget.from_dict(entry))

        return cls(
            name=name,
            target=WindowTarget(
                process_name=target_app,
                title=target_title,
                title_pattern=target_pattern,
            ),
            extra_targets=extras,
            actions=actions,
            variables=variables,
            created=str(raw.get("created", "") or now_iso()),
            last_modified=str(raw.get("last_modified", "") or now_iso()),
            playback_speed=float(raw.get("playback_speed", 1.0) or 1.0),
        )

    def all_targets(self) -> list[WindowTarget]:
        """Return the primary target plus any extra accepted windows."""
        targets = [self.target]
        targets.extend(t for t in self.extra_targets if t is not self.target)
        return targets

    def missing_variables(self, known: set[str] | None = None) -> list[str]:
        """Return the ``${name}`` placeholders with no matching variable.

        Playback calls this before starting so an unresolved placeholder is
        reported to the user instead of being typed literally into a form.
        """
        available = set(known) if known is not None else set(self.variable_map())
        missing: list[str] = []
        for action in self.actions:
            for name in action.placeholders():
                if name not in available and name not in missing:
                    missing.append(name)
        return missing

    def resolved_actions(self, overrides: dict[str, str] | None = None) -> list[Action]:
        """Return copies of the actions with variables substituted.

        ``overrides`` takes precedence over the macro's stored values, which is
        what the runtime prompt in the GUI supplies.
        """
        merged = self.variable_map()
        merged.update(overrides or {})
        return [Action.from_dict(a.to_dict()).substitute(merged) for a in self.actions]

    def summarise(self) -> str:
        target = self.target.process_name or "(any)"
        return f"{self.name}  [{target}]  {len(self.actions)} actions"


def actions_from_iterable(raw_actions: Iterable[Any]) -> list[Action]:
    """Convert an iterable of dicts into Actions, skipping bad entries."""
    result: list[Action] = []
    for entry in raw_actions:
        try:
            result.append(Action.from_dict(entry))
        except (ValueError, TypeError):
            continue
    return result


def safe_filename(name: str) -> str:
    """Turn a macro name into a filesystem-safe stem."""
    cleaned = re.sub(r"[^\w\-. ]", "", name, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "macro")[:80]


def expand_user_path(path: str | Path) -> Path:
    """Expand ``~`` and environment variables in a path."""
    return Path(path).expanduser()
