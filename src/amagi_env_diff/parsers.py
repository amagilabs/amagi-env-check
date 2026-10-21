"""Parsers for dotenv files and deploy-platform CLI output.

Every parser reads values only long enough to decide whether they are empty,
then drops them. An Entry never holds a value, so nothing downstream can
print one by accident.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
COMMENTED_KEY_RE = re.compile(r"^#\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=")
SEALED_RE = re.compile(r"^#\s*([A-Za-z_][A-Za-z0-9_.\-]*) is sealed\b")
VERCEL_HEADER_RE = re.compile(r"^\s*name\s+value\s+environments\b", re.IGNORECASE)
FLY_HEADER_RE = re.compile(r"^\s*name\s+digest\b", re.IGNORECASE)

FORMATS = ("auto", "dotenv", "json", "vercel", "railway", "fly")


class ParseError(ValueError):
    """Raised when input cannot be read in the requested format."""


@dataclass
class Entry:
    key: str
    line: Optional[int] = None
    # True: value is empty. False: value is set. None: value not visible.
    empty: Optional[bool] = None
    # Commented-out key in an example file (`# KEY=`): documented, not required.
    optional: bool = False


@dataclass
class Parsed:
    entries: List[Entry] = field(default_factory=list)
    platform: str = "live"
    values_known: bool = True
    warnings: List[str] = field(default_factory=list)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _clean(text: str) -> str:
    return strip_ansi(text.lstrip("\ufeff")).replace("\r\n", "\n").replace("\r", "\n")


def _find_close(body: str, quote: str) -> Tuple[str, bool]:
    """Return (value, closed) for the text after an opening quote."""
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and quote == '"' and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(out), True
        out.append(ch)
        i += 1
    return "".join(out), False


def parse_dotenv(text: str, allow_commented: bool = False) -> Parsed:
    """Parse a dotenv file.

    Handles `export KEY=`, single/double/backtick quotes (double quotes may span
    lines), inline ` # comments` on unquoted values, and bare `KEY` lines (empty).
    With allow_commented, `# KEY=...` lines become optional entries; this is how
    example files mark a key as documented but not required. Railway's
    `# KEY is sealed` comment from `--kv` output becomes a set-but-hidden entry.
    """
    parsed = Parsed(platform="dotenv")
    lines = _clean(text).split("\n")
    i = 0
    n = len(lines)
    while i < n:
        lineno = i + 1
        s = lines[i].strip()
        i += 1
        if not s:
            continue
        if s.startswith("#"):
            m = SEALED_RE.match(s)
            if m:
                parsed.entries.append(Entry(m.group(1), lineno, None))
                continue
            if allow_commented:
                m = COMMENTED_KEY_RE.match(s)
                if m:
                    parsed.entries.append(Entry(m.group(1), lineno, None, optional=True))
            continue
        if s.startswith("export ") or s.startswith("export\t"):
            s = s[7:].lstrip()
        if "=" not in s:
            if KEY_RE.match(s):
                parsed.entries.append(Entry(s, lineno, True))
            else:
                parsed.warnings.append(f"line {lineno}: not a KEY=VALUE line, skipped")
            continue
        key, _, rest = s.partition("=")
        key = key.strip()
        if not KEY_RE.match(key):
            parsed.warnings.append(f"line {lineno}: not a KEY=VALUE line, skipped")
            continue
        rest = rest.strip()
        if rest[:1] in ('"', "'", "`"):
            quote = rest[0]
            body = rest[1:]
            value, closed = _find_close(body, quote)
            while not closed and i < n:
                body += "\n" + lines[i]
                i += 1
                value, closed = _find_close(body, quote)
            if not closed:
                parsed.warnings.append(f"line {lineno}: {key} has an unterminated quote")
        else:
            value = re.split(r"\s+#", rest, maxsplit=1)[0]
        parsed.entries.append(Entry(key, lineno, value.strip() == ""))
    return parsed


def _vercel_rows(text: str, target: Optional[str]) -> Optional[Parsed]:
    lines = _clean(text).split("\n")
    for idx, line in enumerate(lines):
        if not VERCEL_HEADER_RE.match(line):
            continue
        low = line.lower()
        env_col = low.index("environments")
        created_col = low.find("created", env_col)
        parsed = Parsed(platform="vercel", values_known=False)
        for row in lines[idx + 1:]:
            if not row.strip():
                if parsed.entries:
                    break
                continue
            key = row.split()[0]
            if not KEY_RE.match(key):
                continue
            envs = row[env_col:created_col if created_col > 0 else None].strip()
            if target and target.lower() not in envs.lower():
                continue
            parsed.entries.append(Entry(key, None, None))
        return parsed
    return None


def _vercel_json(data: dict, target: Optional[str]) -> Parsed:
    parsed = Parsed(platform="vercel", values_known=False)
    for env in data.get("envs") or []:
        if not isinstance(env, dict):
            continue
        key = env.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            continue
        targets = env.get("target") or []
        if isinstance(targets, str):
            targets = [targets]
        if target and target.lower() not in [str(t).lower() for t in targets]:
            continue
        parsed.entries.append(Entry(key, None, None))
    return parsed


def _railway_table(text: str) -> Optional[Parsed]:
    parsed = Parsed(platform="railway")
    seen_row = False
    for line in _clean(text).split("\n"):
        s = line.strip()
        if not (s.startswith("\u2551") and "\u2502" in s):
            continue
        seen_row = True
        inner = s.strip("\u2551")
        key, _, val = inner.partition("\u2502")
        key = key.strip()
        if not key:
            continue  # wrapped continuation of a long value
        if not KEY_RE.match(key):
            continue
        val = val.strip()
        parsed.entries.append(Entry(key, None, None if val == "<sealed>" else val == ""))
    return parsed if seen_row else None


def _fly_table(text: str) -> Optional[Parsed]:
    lines = _clean(text).split("\n")
    for idx, line in enumerate(lines):
        if not FLY_HEADER_RE.match(line):
            continue
        parsed = Parsed(platform="fly", values_known=False)
        for row in lines[idx + 1:]:
            parts = row.split()
            if not parts:
                if parsed.entries:
                    break
                continue
            if KEY_RE.match(parts[0]):
                parsed.entries.append(Entry(parts[0], None, None))
        return parsed
    return None


def _json_any(data, target: Optional[str]) -> Parsed:
    if isinstance(data, dict) and isinstance(data.get("envs"), list):
        return _vercel_json(data, target)
    if isinstance(data, dict):
        # A flat {"KEY": "value"} object, as printed by `railway variable list --json`.
        parsed = Parsed(platform="live")
        for key, value in data.items():
            if not isinstance(key, str) or not KEY_RE.match(key):
                continue
            if value is None:
                empty = None  # Railway sealed variable
            elif isinstance(value, str):
                empty = value.strip() == ""
            else:
                empty = False
            parsed.entries.append(Entry(key, None, empty))
        if any(e.empty is None for e in parsed.entries):
            parsed.warnings.append("sealed values are hidden, so they are not checked for emptiness")
        return parsed
    if isinstance(data, list):
        # A list of {"name": ...} objects, as printed by `fly secrets list --json`.
        is_fly = any(isinstance(i, dict) and ("digest" in i or "Digest" in i) for i in data)
        parsed = Parsed(platform="fly" if is_fly else "live", values_known=False)
        for item in data:
            if not isinstance(item, dict):
                continue
            key = item.get("name", item.get("Name", item.get("key", item.get("Key"))))
            if isinstance(key, str) and KEY_RE.match(key):
                parsed.entries.append(Entry(key, None, None))
        return parsed
    raise ParseError("JSON input must be an object or a list")


def parse_live(text: str, fmt: str = "auto", target: Optional[str] = None) -> Parsed:
    """Parse the output of a deploy platform CLI, a JSON dump, or a pulled dotenv file."""
    if fmt not in FORMATS:
        raise ParseError(f"unknown format {fmt!r}")
    clean = _clean(text)
    stripped = clean.lstrip()
    looks_json = stripped[:1] in ("{", "[")

    if fmt in ("auto", "json", "vercel", "railway", "fly") and looks_json:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if fmt == "json":
                raise ParseError(f"invalid JSON at line {exc.lineno}") from None
        else:
            parsed = _json_any(data, target)
            if fmt in ("vercel", "railway", "fly"):
                parsed.platform = fmt
            return parsed
    if fmt == "json":
        raise ParseError("input is not JSON")

    if fmt in ("auto", "vercel"):
        parsed = _vercel_rows(clean, target)
        if parsed is not None:
            return parsed
        if fmt == "vercel":
            raise ParseError("no `vercel env ls` table header (name / value / environments) found")
    if fmt in ("auto", "railway"):
        parsed = _railway_table(clean)
        if parsed is not None:
            return parsed
    if fmt in ("auto", "fly"):
        parsed = _fly_table(clean)
        if parsed is not None:
            return parsed
        if fmt == "fly":
            raise ParseError("no `fly secrets list` table header (NAME / DIGEST) found")

    parsed = parse_dotenv(clean)
    parsed.platform = "railway" if fmt == "railway" else "live"
    return parsed


def parse_ignore_file(text: str) -> List[str]:
    patterns = []
    for line in _clean(text).split("\n"):
        s = line.strip()
        if s and not s.startswith("#"):
            patterns.append(s)
    return patterns


def entries_by_key(entries: List[Entry]) -> Dict[str, List[Entry]]:
    out: Dict[str, List[Entry]] = {}
    for e in entries:
        out.setdefault(e.key, []).append(e)
    return out
