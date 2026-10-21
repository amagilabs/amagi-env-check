"""Compare env sources by key name."""

import fnmatch
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .parsers import Entry, Parsed

CATEGORIES = ("missing", "extra", "empty", "duplicate")

# Variables the platforms inject on their own. Dropped from the live source
# unless the example file lists them.
PLATFORM_DEFAULT_IGNORE = (
    "VERCEL",
    "VERCEL_*",
    "TURBO_*",
    "NX_DAEMON",
    "RAILWAY_*",
    "FLY_*",
)


@dataclass
class Source:
    name: str
    kind: str  # "example" | "local" | "live"
    parsed: Parsed
    path: Optional[str] = None

    @property
    def values_known(self) -> bool:
        return self.parsed.values_known

    def by_key(self) -> Dict[str, List[Entry]]:
        out: Dict[str, List[Entry]] = {}
        for e in self.parsed.entries:
            out.setdefault(e.key, []).append(e)
        return out

    def last(self) -> Dict[str, Entry]:
        return {k: v[-1] for k, v in self.by_key().items()}


@dataclass
class Finding:
    category: str
    source: str
    key: str
    lines: List[int] = field(default_factory=list)


@dataclass
class Result:
    sources: List[Source]
    findings: List[Finding]
    reference: Optional[str]
    keys: List[str]
    matrix: Dict[str, Dict[str, str]]

    def counts(self) -> Dict[str, int]:
        c = {cat: 0 for cat in CATEGORIES}
        for f in self.findings:
            c[f.category] += 1
        return c


def _matches(key: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(key, p) for p in patterns)


def apply_ignores(sources: List[Source], ignore: Iterable[str], default_ignore: bool = True) -> None:
    """Drop ignored keys in place. Platform defaults apply to live sources only."""
    ignore = list(ignore)
    example_keys = set()
    for s in sources:
        if s.kind == "example":
            example_keys |= {e.key for e in s.parsed.entries}
    for s in sources:
        kept = []
        for e in s.parsed.entries:
            if _matches(e.key, ignore):
                continue
            if (default_ignore and s.kind == "live" and e.key not in example_keys
                    and _matches(e.key, PLATFORM_DEFAULT_IGNORE)):
                continue
            kept.append(e)
        s.parsed.entries = kept


def _state(source: Source, entry: Optional[Entry]) -> str:
    if entry is None:
        return "-"
    if source.kind == "example":
        return "optional" if entry.optional else "listed"
    if entry.empty is None:
        return "present"
    return "empty" if entry.empty else "set"


def compare(sources: List[Source]) -> Result:
    """Compare sources. The example file is the reference; without one, the local .env is."""
    example = next((s for s in sources if s.kind == "example"), None)
    local = next((s for s in sources if s.kind == "local"), None)
    reference = example or local
    findings: List[Finding] = []

    last = {s.name: s.last() for s in sources}
    ref_entries = last[reference.name] if reference else {}
    required = sorted(k for k, e in ref_entries.items() if not e.optional)
    documented = set(ref_entries)

    targets = [s for s in sources if s is not reference]
    # Order: live problems first, they are the ones that break a deploy.
    targets.sort(key=lambda s: 0 if s.kind == "live" else 1)

    for t in targets:
        have = last[t.name]
        for key in required:
            if key not in have:
                findings.append(Finding("missing", t.name, key))
    if example is not None:
        for t in targets:
            for key in sorted(last[t.name]):
                if key not in documented:
                    findings.append(Finding("extra", t.name, key, _lines(last[t.name][key])))
    elif local is not None:
        # No example: the local file is the reference, so live keys it lacks are "missing locally".
        for t in targets:
            for key in sorted(last[t.name]):
                if key not in documented:
                    findings.append(Finding("missing", local.name, key))

    for s in sorted(sources, key=lambda s: 0 if s.kind == "live" else 1):
        if s.kind == "example":
            continue
        for key, e in sorted(last[s.name].items()):
            ref = ref_entries.get(key)
            if e.empty and not (ref is not None and ref.optional):
                findings.append(Finding("empty", s.name, key, _lines(e)))

    for s in sources:
        for key, entries in sorted(s.by_key().items()):
            if len(entries) > 1:
                findings.append(Finding("duplicate", s.name, key,
                                        [e.line for e in entries if e.line is not None]))

    all_keys = sorted({k for s in sources for k in last[s.name]})
    matrix = {k: {s.name: _state(s, last[s.name].get(k)) for s in sources} for k in all_keys}
    return Result(sources, findings, reference.name if reference else None, all_keys, matrix)


def _lines(e: Entry) -> List[int]:
    return [e.line] if e.line is not None else []
