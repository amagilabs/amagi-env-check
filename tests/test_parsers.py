import json

import pytest

from amagi_env_diff.parsers import ParseError, parse_dotenv, parse_ignore_file, parse_live


def keys(parsed):
    return [e.key for e in parsed.entries]


def by_key(parsed):
    return {e.key: e for e in parsed.entries}


# ---------- dotenv ----------

def test_dotenv_basic_set_and_empty():
    p = parse_dotenv("A=1\nB=\nC=  \nD=\"\"\nE=''\n")
    e = by_key(p)
    assert keys(p) == ["A", "B", "C", "D", "E"]
    assert e["A"].empty is False
    assert all(e[k].empty for k in "BCDE")


def test_dotenv_line_numbers_skip_blanks_and_comments():
    p = parse_dotenv("# header\n\nA=1\n  # indented comment\nB=2\n")
    assert [(x.key, x.line) for x in p.entries] == [("A", 3), ("B", 5)]


def test_dotenv_export_prefix():
    p = parse_dotenv("export A=1\nexport\tB=\n")
    assert keys(p) == ["A", "B"]
    assert by_key(p)["B"].empty is True


def test_dotenv_inline_comment_on_unquoted_value():
    p = parse_dotenv("A= # only a comment\nB=x # note\nC=pass#word\n")
    e = by_key(p)
    assert e["A"].empty is True
    assert e["B"].empty is False
    assert e["C"].empty is False


def test_dotenv_hash_inside_quotes_is_a_value():
    p = parse_dotenv('A="#not-a-comment"\nB=" # still value"\n')
    assert by_key(p)["A"].empty is False
    assert by_key(p)["B"].empty is False


def test_dotenv_multiline_double_quoted_value():
    text = 'KEY="-----BEGIN-----\nline two\n-----END-----"\nNEXT=1\n'
    p = parse_dotenv(text)
    assert [(x.key, x.line) for x in p.entries] == [("KEY", 1), ("NEXT", 4)]
    assert by_key(p)["KEY"].empty is False
    assert not p.warnings


def test_dotenv_escaped_quote_does_not_close():
    p = parse_dotenv('A="say \\"hi\\""\nB=1\n')
    assert keys(p) == ["A", "B"]
    assert not p.warnings


def test_dotenv_unterminated_quote_warns_without_value():
    p = parse_dotenv('A="never closed\nB=1\n')
    assert keys(p) == ["A"]
    assert p.warnings == ["line 1: A has an unterminated quote"]
    assert "never closed" not in p.warnings[0]


def test_dotenv_bare_key_is_empty():
    p = parse_dotenv("JUST_A_KEY\n")
    assert by_key(p)["JUST_A_KEY"].empty is True


def test_dotenv_invalid_line_warns_with_line_number_only():
    p = parse_dotenv("A=1\nthis is not valid secret-ish text\n9BAD=x\n")
    assert keys(p) == ["A"]
    assert p.warnings == ["line 2: not a KEY=VALUE line, skipped",
                          "line 3: not a KEY=VALUE line, skipped"]
    assert not any("secret" in w for w in p.warnings)


def test_dotenv_crlf_and_bom():
    p = parse_dotenv("﻿A=1\r\nB=\r\n")
    assert keys(p) == ["A", "B"]
    assert by_key(p)["B"].empty is True


def test_dotenv_duplicates_are_kept_in_order():
    p = parse_dotenv("A=1\nB=2\nA=3\n")
    assert [(x.key, x.line) for x in p.entries] == [("A", 1), ("B", 2), ("A", 3)]


def test_dotenv_commented_keys_only_when_allowed():
    text = "A=\n# B=\n#export C=x\n# just prose = here\n"
    assert keys(parse_dotenv(text)) == ["A"]
    p = parse_dotenv(text, allow_commented=True)
    e = by_key(p)
    assert keys(p) == ["A", "B", "C"]
    assert e["B"].optional and e["C"].optional
    assert not e["A"].optional


def test_dotenv_railway_sealed_comment_is_present_unknown():
    p = parse_dotenv("A=1\n# TOKEN is sealed; its value cannot be read\n")
    assert by_key(p)["TOKEN"].empty is None


def test_entries_never_hold_values():
    p = parse_dotenv("SECRET=hunter2-sentinel\n")
    assert "hunter2-sentinel" not in repr(p)


# ---------- Vercel ----------

# Layout copied from a real `vercel env ls production` run (Vercel CLI 56.5.0).
VERCEL_TABLE = (
    "\n"
    " name               value               environments        created    \n"
    " STRIPE_API_KEY     Encrypted           Production          5d ago     \n"
    " DATABASE_URL       Encrypted           Production, Preview 9d ago     \n"
    " PREVIEW_ONLY       Encrypted           Preview             1d ago     \n"
    "\n"
)


def test_vercel_table_autodetect():
    p = parse_live(VERCEL_TABLE)
    assert p.platform == "vercel"
    assert p.values_known is False
    assert keys(p) == ["STRIPE_API_KEY", "DATABASE_URL", "PREVIEW_ONLY"]
    assert all(e.empty is None for e in p.entries)


def test_vercel_table_target_filter():
    p = parse_live(VERCEL_TABLE, target="production")
    assert keys(p) == ["STRIPE_API_KEY", "DATABASE_URL"]


def test_vercel_table_with_ansi_and_stderr_preamble():
    text = ("Vercel CLI 56.5.0\n> Environment Variables found for team/app [186ms]\n"
            "\x1b[1m" + VERCEL_TABLE + "\x1b[22m")
    assert keys(parse_live(text)) == ["STRIPE_API_KEY", "DATABASE_URL", "PREVIEW_ONLY"]


def test_vercel_json():
    data = {"envs": [
        {"key": "A", "type": "sensitive", "target": ["production"], "createdAt": 1},
        {"key": "B", "type": "encrypted", "target": ["preview", "development"]},
        {"key": "C", "type": "plain", "target": "production"},
    ]}
    p = parse_live(json.dumps(data))
    assert p.platform == "vercel"
    assert keys(p) == ["A", "B", "C"]
    assert keys(parse_live(json.dumps(data), target="production")) == ["A", "C"]


def test_vercel_format_without_table_errors():
    with pytest.raises(ParseError):
        parse_live("A=1\n", fmt="vercel")


# ---------- Railway ----------

def test_railway_json_with_sealed_and_empty():
    data = {"DATABASE_URL": "postgres://x", "EMPTY": "", "SEALED": None,
            "RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"}
    p = parse_live(json.dumps(data), fmt="railway")
    e = by_key(p)
    assert p.platform == "railway"
    assert e["DATABASE_URL"].empty is False
    assert e["EMPTY"].empty is True
    assert e["SEALED"].empty is None
    assert p.warnings


def test_flat_json_autodetect_is_generic_live():
    p = parse_live('{"A": "1", "B": ""}')
    assert p.platform == "live"
    assert by_key(p)["B"].empty is True


# Mirrors railway's table.rs: double-line box, light vertical between columns.
RAILWAY_TABLE = (
    "\x1b[36m╔════ Variables for api ════╗\x1b[0m\n"
    "║ DATABASE_URL │ postgres://user:pw@host:5432/averyveryverylong ║\n"
    "║              │ databasename                                    ║\n"
    "║────────────║\n"
    "║ API_TOKEN    │ <sealed>                                        ║\n"
    "║────────────║\n"
    "║ BLANK        │                                                 ║\n"
    "╚════════════╝\n"
)


def test_railway_table():
    p = parse_live(RAILWAY_TABLE)
    e = by_key(p)
    assert p.platform == "railway"
    assert keys(p) == ["DATABASE_URL", "API_TOKEN", "BLANK"]
    assert e["DATABASE_URL"].empty is False
    assert e["API_TOKEN"].empty is None
    assert e["BLANK"].empty is True


def test_railway_kv_output():
    p = parse_live("A=1\nB=\n# C is sealed; its value cannot be read\n", fmt="railway")
    assert p.platform == "railway"
    assert [(x.key, x.empty) for x in p.entries] == [("A", False), ("B", True), ("C", None)]


# ---------- Fly ----------

def test_fly_json_current_and_old_field_names():
    new = [{"name": "DATABASE_URL", "digest": "abc", "status": "Deployed"}]
    old = [{"Name": "SECRET_KEY_BASE", "Digest": "def", "CreatedAt": "2024-01-01"}]
    p = parse_live(json.dumps(new))
    assert p.platform == "fly" and p.values_known is False
    assert keys(p) == ["DATABASE_URL"]
    assert keys(parse_live(json.dumps(old))) == ["SECRET_KEY_BASE"]


def test_fly_table():
    text = ("NAME           \tDIGEST          \tSTATUS\n"
            "DATABASE_URL   \t1a2b3c4d5e6f7a8b\tDeployed\n"
            "SECRET_KEY_BASE\t9f8e7d6c5b4a3210\tStaged\n"
            "\n"
            "Some secrets are staged. Run fly deploy.\n")
    p = parse_live(text)
    assert p.platform == "fly"
    assert keys(p) == ["DATABASE_URL", "SECRET_KEY_BASE"]


def test_fly_format_without_table_errors():
    with pytest.raises(ParseError):
        parse_live("nothing here", fmt="fly")


# ---------- generic ----------

def test_pulled_dotenv_is_live_with_values():
    p = parse_live('VERCEL="1"\nAPI_URL="https://example.com"\nEMPTY=""\n')
    assert p.platform == "live"
    assert p.values_known is True
    assert by_key(p)["EMPTY"].empty is True


def test_invalid_json_with_json_format():
    with pytest.raises(ParseError, match="invalid JSON"):
        parse_live("{not json", fmt="json")


def test_non_json_with_json_format():
    with pytest.raises(ParseError):
        parse_live("A=1", fmt="json")


def test_unknown_format():
    with pytest.raises(ParseError):
        parse_live("A=1", fmt="yaml")


def test_ignore_file():
    assert parse_ignore_file("# comment\n\nVERCEL_*\n  DEBUG \n") == ["VERCEL_*", "DEBUG"]
