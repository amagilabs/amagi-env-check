"""Render a comparison as text or JSON. Only key names ever reach the output."""

import json
from typing import Dict, List, Tuple

from . import __version__
from .compare import CATEGORIES, Finding, Result

BLURB = {
    "missing": "listed in {ref}, not in {src}",
    "extra": "in {src}, not listed in {ref}",
    "empty": "present with an empty value",
    "duplicate": "defined more than once; the last one wins",
}


def _groups(result: Result) -> List[Tuple[Tuple[str, str], List[Finding]]]:
    groups: Dict[Tuple[str, str], List[Finding]] = {}
    for f in result.findings:
        groups.setdefault((f.category, f.source), []).append(f)
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(groups.items(), key=lambda kv: order[kv[0][0]])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _source_line(result: Result) -> str:
    parts = []
    for s in result.sources:
        n = len({e.key for e in s.parsed.entries})
        note = "" if s.kind == "example" or s.values_known else ", names only"
        parts.append(f"{s.name} ({_plural(n, 'key')}{note})")
    return f"Compared {len(result.sources)} sources: " + ", ".join(parts)


def _missing_ref(result: Result, f: Finding) -> str:
    # Without an example file, a key missing from the local file comes from the live source.
    if f.source == result.reference:
        return next((s.name for s in result.sources if s.kind == "live"), "the other source")
    return result.reference or "the reference"


def render_text(result: Result, matrix: bool = False) -> str:
    out = [_source_line(result), ""]
    for (cat, src), items in _groups(result):
        ref = _missing_ref(result, items[0]) if cat == "missing" else result.reference
        title = f"{cat.upper()} IN {src}"
        out.append(f"{title}  {BLURB[cat].format(ref=ref, src=src)}")
        width = max(len(f.key) for f in items)
        for f in items:
            if f.lines:
                label = "line" if len(f.lines) == 1 else "lines"
                out.append(f"  {f.key.ljust(width)}  {label} {', '.join(map(str, f.lines))}")
            else:
                out.append(f"  {f.key}")
        out.append("")

    if matrix and result.keys:
        out.extend(render_matrix(result))
        out.append("")

    counts = result.counts()
    total = sum(counts.values())
    nkeys = _plural(len(result.keys), "key")
    if total:
        detail = ", ".join(f"{n} {c}" for c, n in counts.items() if n)
        out.append(f"{_plural(total, 'problem')} ({detail}) across {nkeys}. No values were printed.")
    else:
        out.append(f"No differences across {nkeys}. No values were printed.")
    return "\n".join(out) + "\n"


def render_matrix(result: Result) -> List[str]:
    names = [s.name for s in result.sources]
    kw = max([len("KEY")] + [len(k) for k in result.keys])
    widths = [max(len(n), 8) for n in names]
    lines = ["KEY".ljust(kw) + "  " + "  ".join(n.ljust(w) for n, w in zip(names, widths))]
    for k in result.keys:
        row = result.matrix[k]
        lines.append(k.ljust(kw) + "  " + "  ".join(row[n].ljust(w) for n, w in zip(names, widths)))
    return [line.rstrip() for line in lines]


def render_json(result: Result, matrix: bool = False) -> str:
    data = {
        "tool": "amagi-env-diff",
        "version": __version__,
        "reference": result.reference,
        "sources": [
            {
                "name": s.name,
                "kind": s.kind,
                "path": s.path,
                "keys": len({e.key for e in s.parsed.entries}),
                "values_known": s.values_known,
            }
            for s in result.sources
        ],
        "findings": [
            {"category": f.category, "source": f.source, "key": f.key, "lines": f.lines}
            for f in result.findings
        ],
        "counts": result.counts(),
        "problems": len(result.findings),
    }
    if matrix:
        data["matrix"] = result.matrix
    return json.dumps(data, indent=2) + "\n"
