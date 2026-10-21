"""Command-line entry point for amagi-env-diff."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import __version__
from .compare import CATEGORIES, Source, apply_ignores, compare
from .parsers import FORMATS, ParseError, parse_dotenv, parse_ignore_file, parse_live, strip_ansi
from .report import render_json, render_text

EXAMPLE_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist")
LOCAL_NAMES = (".env", ".env.local")
IGNORE_FILE = ".envdiffignore"
RUN_TIMEOUT = 120


class UsageError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amagi-env-diff",
        description=(
            "Compare .env, .env.example and a live deploy environment (Vercel, Railway, Fly) "
            "by key name. Reports missing, extra, empty and duplicate keys. Never prints a value."
        ),
        epilog=(
            "examples:\n"
            "  amagi-env-diff                          .env vs .env.example in this folder\n"
            "  amagi-env-diff --run vercel             also compare the Vercel production env\n"
            "  vercel env ls production | amagi-env-diff --live -\n"
            "  railway variable list --json > live.json && amagi-env-diff --live live.json\n"
            "  amagi-env-diff --run fly --run-args \"-a my-app\" --matrix\n\n"
            "exit codes: 0 no problems, 1 problems found, 2 usage or input error"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("dir", nargs="?", default=".", help="project folder (default: current folder)")
    p.add_argument("--example", metavar="PATH",
                   help="example file (default: first of %s)" % ", ".join(EXAMPLE_NAMES))
    p.add_argument("--env", metavar="PATH", dest="env_file",
                   help="local env file (default: first of %s)" % ", ".join(LOCAL_NAMES))
    p.add_argument("--no-example", action="store_true", help="do not read an example file")
    p.add_argument("--no-env", action="store_true", help="do not read a local env file")
    live = p.add_mutually_exclusive_group()
    live.add_argument("--live", metavar="PATH",
                      help="saved platform CLI output, JSON, or a pulled dotenv file; '-' reads stdin")
    live.add_argument("--run", choices=("vercel", "railway", "fly"),
                      help="run the platform CLI yourself and compare its output")
    p.add_argument("--run-args", metavar="ARGS", default="",
                   help="extra arguments for --run, e.g. \"--service api\" or \"-a my-app\"")
    p.add_argument("--live-format", choices=FORMATS, default="auto",
                   help="format of --live input (default: auto-detect)")
    p.add_argument("--live-name", metavar="NAME", help="label for the live source in the report")
    p.add_argument("--target", metavar="ENV",
                   help="Vercel environment to keep (production, preview, development); "
                        "--run vercel defaults to production")
    p.add_argument("--ignore", metavar="PATTERN", action="append", default=[],
                   help="glob of keys to ignore everywhere (repeatable); also read from .envdiffignore")
    p.add_argument("--no-default-ignore", action="store_true",
                   help="keep platform-injected keys (VERCEL_*, RAILWAY_*, FLY_*, TURBO_*) in .env and the live source")
    p.add_argument("--matrix", action="store_true", help="also print a key-by-source table")
    p.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    p.add_argument("--fail-on", metavar="LIST", default="any",
                   help="comma list of %s, or 'any' / 'none' (default: any)" % ",".join(CATEGORIES))
    p.add_argument("--version", action="version", version=f"amagi-env-diff {__version__}")
    return p


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from None


def _pick(base: Path, explicit: Optional[str], names: Sequence[str], what: str) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and not p.exists():
            p = base / explicit
        if not p.is_file():
            raise UsageError(f"{what} file not found: {explicit}")
        return p
    for name in names:
        p = base / name
        if p.is_file():
            return p
    return None


def _label(path: Path, base: Path) -> str:
    try:
        rel = path.resolve().relative_to(base.resolve())
        return rel.as_posix()
    except ValueError:
        return path.name


def _fail_on(value: str) -> Tuple[str, ...]:
    items = [v.strip().lower() for v in value.split(",") if v.strip()]
    if not items:
        raise UsageError("--fail-on needs at least one value")
    if "none" in items:
        return ()
    if "any" in items:
        return CATEGORIES
    bad = [v for v in items if v not in CATEGORIES]
    if bad:
        raise UsageError(f"--fail-on: unknown value {bad[0]!r} (use {', '.join(CATEGORIES)}, any, none)")
    return tuple(items)


def run_platform(platform: str, extra: List[str], target: Optional[str]) -> Tuple[str, str]:
    """Run a platform CLI and return (output, format). Raises UsageError on failure."""
    if platform == "vercel":
        env = target or "production"
        attempts = [(["vercel", "env", "ls", env, "--format", "json"], "vercel"),
                    (["vercel", "env", "ls", env], "vercel")]
    elif platform == "railway":
        attempts = [(["railway", "variable", "list", "--json"], "railway"),
                    (["railway", "variables", "--json"], "railway")]
    else:
        attempts = [(["fly", "secrets", "list", "--json"], "fly"),
                    (["flyctl", "secrets", "list", "--json"], "fly")]

    last_error = None
    for argv, fmt in attempts:
        exe = shutil.which(argv[0])
        if exe is None:
            last_error = f"{argv[0]} not found on PATH"
            continue
        try:
            proc = subprocess.run([exe] + argv[1:] + extra, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            last_error = f"{' '.join(argv[:3])} timed out after {RUN_TIMEOUT}s"
            continue
        if proc.returncode == 0:
            out = proc.stdout
            if not out.strip() and proc.stderr.strip():
                out = proc.stderr
            return out, fmt
        tail = [line for line in strip_ansi(proc.stderr).splitlines() if line.strip()][-3:]
        last_error = f"{' '.join(argv[:3])} exited {proc.returncode}" + (
            ": " + " / ".join(tail) if tail else "")
    raise UsageError(last_error or f"could not run {platform}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _main(args)
    except (UsageError, ParseError) as exc:
        print(f"amagi-env-diff: error: {exc}", file=sys.stderr)
        return 2


def _main(args: argparse.Namespace) -> int:
    fail_on = _fail_on(args.fail_on)
    base = Path(args.dir)
    if not base.is_dir():
        raise UsageError(f"not a folder: {args.dir}")

    sources: List[Source] = []
    example_path = None if args.no_example else _pick(base, args.example, EXAMPLE_NAMES, "example")
    if example_path:
        sources.append(Source(_label(example_path, base), "example",
                              parse_dotenv(_read(example_path), allow_commented=True), str(example_path)))
    local_path = None if args.no_env else _pick(base, args.env_file, LOCAL_NAMES, "env")
    if local_path:
        sources.append(Source(_label(local_path, base), "local",
                              parse_dotenv(_read(local_path)), str(local_path)))

    if args.live or args.run:
        if args.run:
            text, fmt = run_platform(args.run, shlex.split(args.run_args, posix=os.name != "nt"),
                                     args.target)
            live_path = None
        elif args.live == "-":
            text, fmt, live_path = sys.stdin.read(), args.live_format, "-"
        else:
            p = Path(args.live)
            if not p.is_file():
                raise UsageError(f"live file not found: {args.live}")
            text, fmt, live_path = _read(p), args.live_format, str(p)
        target = args.target if args.target else None
        parsed = parse_live(text, fmt, target)
        if not parsed.entries and not text.strip():
            raise UsageError("live input is empty")
        name = args.live_name or parsed.platform
        if name in {s.name for s in sources}:
            name = f"{name} (live)"
        sources.append(Source(name, "live", parsed, live_path))

    if len(sources) < 2:
        found = ", ".join(s.name for s in sources) or "nothing"
        raise UsageError(
            f"need at least two sources to compare, found {found} in {base}. "
            "Add --live FILE, --run vercel|railway|fly, --env or --example.")

    for s in sources:
        for w in s.parsed.warnings:
            print(f"amagi-env-diff: warning: {s.name}: {w}", file=sys.stderr)

    ignore = list(args.ignore)
    ignore_file = base / IGNORE_FILE
    if ignore_file.is_file():
        ignore += parse_ignore_file(_read(ignore_file))
    apply_ignores(sources, ignore, default_ignore=not args.no_default_ignore)

    result = compare(sources)
    if args.format == "json":
        sys.stdout.write(render_json(result, matrix=args.matrix))
    else:
        sys.stdout.write(render_text(result, matrix=args.matrix))
    return 1 if any(f.category in fail_on for f in result.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
