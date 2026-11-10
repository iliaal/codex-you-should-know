import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any

SHORTCUTS: dict[tuple[str, ...], Any] = {
    ("tui", "keymap", "chat", "open_presets"): "alt-y",
    ("tui", "presets", "1"): {"input": "ysk 1", "label": "YSK: Tell me more"},
    ("tui", "presets", "2"): {"input": "ysk 2", "label": "YSK: Already knew that"},
    ("tui", "presets", "3"): {"input": "ysk 3", "label": "YSK: Dismiss"},
    ("tui", "presets", "4"): {"input": "ysk 4", "label": "YSK: Chat in main session"},
}
BEGIN = "# you-should-know shortcut v1"
END = "# end you-should-know shortcut"
FRAGMENT = re.compile(
    rf"^{re.escape(BEGIN)} ([\w.]+) ([0-9a-f]{{64}}) ([01])\r?\n"
    rf"(.*?)^{re.escape(END)}\r?\n",
    re.MULTILINE | re.DOTALL,
)


def _get(settings: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = settings
    for index, part in enumerate(path):
        if not isinstance(value, dict):
            raise ValueError(f"Shortcut conflict: {'.'.join(path[:index])} must be a TOML table")
        if part not in value:
            return None
        value = value[part]
    return value


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + " }"
    return json.dumps(value)


def _uses_alt_y(value: Any, *, prefix: bool = False) -> bool:
    bindings = value if isinstance(value, list) else [value]
    for binding in bindings:
        if not isinstance(binding, str) or not binding.split():
            continue
        stroke = binding.split()[0] if prefix else binding.strip()
        parts = [part for part in stroke.lower().split("-") if part]
        if len(parts) == 2 and parts[0] in {"alt", "option"} and parts[1] == "y":
            return True
    return False


def _compatible(existing: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        return _uses_alt_y(existing)
    return (
        isinstance(existing, dict)
        and existing.get("input") == expected["input"]
        and set(existing) <= {"input", "label"}
        and isinstance(existing.get("label", ""), str)
    )


def _matches(text: str, settings: dict[str, Any]) -> bool:
    try:
        actual = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return json.dumps(actual, sort_keys=True, default=str) == json.dumps(
        settings, sort_keys=True, default=str
    )


def _header_path(line: str) -> tuple[str, ...] | None:
    if not line.lstrip().startswith("["):
        return None
    try:
        value: Any = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        path.append(key)
    return tuple(path) if path and value == {} else None


def _insert(text: str, path: tuple[str, ...], value: Any) -> str:
    expected = tomllib.loads(text)
    parent = expected
    for part in path[:-1]:
        parent = parent.setdefault(part, {})
    parent[path[-1]] = value
    newline = "\r\n" if "\r\n" in text else "\n"
    positions: list[tuple[int, tuple[str, ...]]] = [(0, ())]
    offset = 0
    for line in text.splitlines(keepends=True):
        offset += len(line)
        context = _header_path(line)
        if context and path[: len(context)] == context and len(context) < len(path):
            positions.append((offset, context))
    for offset, context in positions:
        key = ".".join(json.dumps(part) for part in path[len(context) :])
        body = f"{key} = {_toml(value)}{newline}"
        digest = hashlib.sha256(body.encode()).hexdigest()
        joined = bool(offset and not text[:offset].endswith("\n"))
        block = (
            (newline if joined else "")
            + f"{BEGIN} {'.'.join(path)} {digest} {int(joined)}{newline}"
            + body
            + END
            + newline
        )
        candidate = text[:offset] + block + text[offset:]
        if _matches(candidate, expected):
            return candidate
    raise ValueError(
        f"Cannot safely add {'.'.join(path)} to this TOML layout; "
        "expand inline target tables into [tui] sections and retry. No settings were changed."
    )


def _remove(text: str) -> tuple[str, list[str], list[str]]:
    removed = []
    kept = []
    for path, value in reversed(SHORTCUTS.items()):
        name = ".".join(path)
        fragments = [match for match in FRAGMENT.finditer(text) if match[1] == name]
        if not fragments:
            continue
        match = fragments[0]
        expected = tomllib.loads(text)
        if (
            len(fragments) != 1
            or hashlib.sha256(match[4].encode()).hexdigest() != match[2]
            or _get(expected, path) != value
        ):
            kept.append(name)
            continue
        parent = expected
        for part in path[:-1]:
            parent = parent[part]
        del parent[path[-1]]
        starts = [match.start()]
        if match[3] == "1" and match.start() and text[match.start() - 1] == "\n":
            length = 2 if text[: match.start()].endswith("\r\n") else 1
            starts.insert(0, match.start() - length)
        candidate = None
        for start in starts:
            possible = text[:start] + text[match.end() :]
            comparison = copy.deepcopy(expected)
            for depth in range(len(path) - 1, -1, -1):
                if _matches(possible, comparison):
                    candidate = possible
                    break
                if depth == 0 or _get(comparison, path[:depth]) != {}:
                    break
                parent = comparison
                for part in path[: depth - 1]:
                    parent = parent[part]
                del parent[path[depth - 1]]
            if candidate is not None:
                break
        if candidate is None:
            kept.append(name)
        else:
            text = candidate
            removed.append(name)
    return text, removed, kept


def _read(path: Path) -> tuple[bytes, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b"", 0o600
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Shortcut config must be a regular file, not a symlink: {path}")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def _write(path: Path, original: bytes, updated: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".ysk-shortcuts-", delete=False
        ) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(), mode)
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        if _read(path)[0] != original:
            raise ValueError("Codex config changed during shortcut setup; retry the command")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _prepare_shortcuts(action: str, text: str) -> tuple[str, dict[str, Any]]:
    settings = tomllib.loads(text)
    if action == "install":
        conflicts = [
            ".".join(key)
            for key, value in SHORTCUTS.items()
            if _get(settings, key) is not None and not _compatible(_get(settings, key), value)
        ]
        for context in (
            "global",
            "chat",
            "composer",
            "editor",
            "vim_normal",
            "vim_operator",
            "vim_search",
            "vim_text_object",
        ):
            bindings = _get(settings, ("tui", "keymap", context))
            if isinstance(bindings, dict):
                conflicts.extend(
                    f"tui.keymap.{context}.{action}"
                    for action, binding in bindings.items()
                    if (context, action) != ("chat", "open_presets")
                    and _uses_alt_y(binding, prefix=True)
                )
        if conflicts:
            raise ValueError("Shortcut conflict; no settings changed: " + ", ".join(conflicts))
        added = []
        for key, value in SHORTCUTS.items():
            if _get(settings, key) is None:
                text = _insert(text, key, value)
                added.append(".".join(key))
        result = {"status": "installed" if added else "already_configured", "added": added}
    else:
        text, removed, kept = _remove(text)
        result = {
            "status": "removed" if removed else "unchanged",
            "removed": removed,
            "kept_modified": kept,
        }
    return text, result


def configure_shortcuts(action: str, config_path: Path | None = None) -> dict[str, Any]:
    path = (
        (
            config_path
            or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        )
        .expanduser()
        .absolute()
    )
    original, mode = _read(path)
    text = original.decode("utf-8")
    try:
        text, result = _prepare_shortcuts(action, text)
    except RecursionError as error:
        raise ValueError(f"Cannot process {path}: TOML nesting exceeds Python limits") from error
    updated = text.encode("utf-8")
    if updated != original:
        _write(path, original, updated, mode)
    return {**result, "config_path": str(path), "restart_required": updated != original}
