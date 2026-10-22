from amagi_env_check.compare import Source, apply_ignores, compare
from amagi_env_check.parsers import parse_dotenv, parse_live


def example(text):
    return Source(".env.example", "example", parse_dotenv(text, allow_commented=True))


def local(text, name=".env"):
    return Source(name, "local", parse_dotenv(text))


def live(text, name=None, fmt="auto"):
    p = parse_live(text, fmt)
    return Source(name or p.platform, "live", p)


def triples(result):
    return [(f.category, f.source, f.key) for f in result.findings]


def test_clean_three_way_match():
    r = compare([example("A=\nB=\n"), local("A=1\nB=2\n"), live("A=x\nB=y\n", "prod")])
    assert r.findings == []
    assert r.reference == ".env.example"
    assert r.keys == ["A", "B"]


def test_missing_in_live_and_local():
    r = compare([example("A=\nB=\nC=\n"), local("A=1\nB=2\n"), live("A=1\n", "prod")])
    assert triples(r) == [
        ("missing", "prod", "B"),
        ("missing", "prod", "C"),
        ("missing", ".env", "C"),
    ]


def test_extra_keys_against_example():
    r = compare([example("A=\n"), local("A=1\nLOCAL_ONLY=1\n"), live("A=1\nLIVE_ONLY=1\n", "prod")])
    assert ("extra", "prod", "LIVE_ONLY") in triples(r)
    assert ("extra", ".env", "LOCAL_ONLY") in triples(r)
    local_extra = [f for f in r.findings if f.key == "LOCAL_ONLY"][0]
    assert local_extra.lines == [2]


def test_optional_example_keys_are_neither_missing_nor_extra():
    r = compare([example("A=\n# SENTRY_DSN=\n"), local("A=1\n"), live("A=1\nSENTRY_DSN=x\n", "prod")])
    assert r.findings == []
    assert r.matrix["SENTRY_DSN"] == {".env.example": "optional", ".env": "-", "prod": "set"}


def test_empty_values_in_local_and_live_not_example():
    r = compare([example("A=\nB=\n"), local("A=\nB=1\n"), live("A=1\nB=\n", "prod")])
    assert ("empty", ".env", "A") in triples(r)
    assert ("empty", "prod", "B") in triples(r)
    assert not any(f.source == ".env.example" for f in r.findings)


def test_empty_optional_key_is_not_reported():
    r = compare([example("# DEBUG=\n"), local("DEBUG=\n")])
    assert r.findings == []


def test_names_only_live_source_cannot_be_empty():
    vercel = (" name   value      environments   created\n"
              " A      Encrypted  Production     1d ago\n")
    r = compare([example("A=\n"), local("A=1\n"), live(vercel)])
    assert r.findings == []
    assert r.matrix["A"]["vercel"] == "present"


def test_duplicates_reported_with_all_lines():
    r = compare([example("A=\nA=\n"), local("A=1\nB=\nA=2\n")])
    dups = [(f.source, f.key, f.lines) for f in r.findings if f.category == "duplicate"]
    assert (".env", "A", [1, 3]) in dups
    assert (".env.example", "A", [1, 2]) in dups


def test_last_duplicate_wins_for_empty_check():
    r = compare([example("A=\n"), local("A=\nA=set\n")])
    assert not any(f.category == "empty" for f in r.findings)


def test_no_example_uses_local_as_reference():
    r = compare([local("A=1\nB=1\n"), live("A=1\nC=1\n", "prod")])
    assert r.reference == ".env"
    assert triples(r) == [("missing", "prod", "B"), ("missing", ".env", "C")]


def test_example_and_live_only():
    r = compare([example("A=\nB=\n"), live('{"A": "1"}', "prod")])
    assert triples(r) == [("missing", "prod", "B")]


def test_user_ignore_patterns_apply_everywhere():
    srcs = [example("A=\nDEBUG_X=\n"), local("A=1\nDEBUG_Y=1\n"), live("A=1\n", "prod")]
    apply_ignores(srcs, ["DEBUG_*"])
    assert compare(srcs).findings == []


def test_platform_keys_dropped_from_local_and_live():
    pulled = 'VERCEL="1"\nVERCEL_ENV="production"\nTURBO_CACHE="x"\nNX_DAEMON="false"\nA="1"\n'
    srcs = [example("A=\n"), local('A=1\nVERCEL_OIDC_TOKEN="x"\nRAILWAY_TOKEN=x\n'), live(pulled, "prod")]
    apply_ignores(srcs, [])
    assert compare(srcs).findings == []


def test_platform_ignore_never_touches_example():
    srcs = [example("A=\nFLY_REGION=\n"), local("A=1\n")]
    apply_ignores(srcs, [])
    assert triples(compare(srcs)) == [("missing", ".env", "FLY_REGION")]


def test_platform_key_kept_when_example_lists_it():
    srcs = [example("A=\nVERCEL_URL=\n"), live('A="1"\nVERCEL_URL="x"\n', "prod")]
    apply_ignores(srcs, [])
    assert compare(srcs).findings == []


def test_no_default_ignore_keeps_platform_keys():
    srcs = [example("A=\n"), live('A="1"\nVERCEL_ENV="production"\n', "prod")]
    apply_ignores(srcs, [], default_ignore=False)
    assert triples(compare(srcs)) == [("extra", "prod", "VERCEL_ENV")]


def test_ignore_is_case_sensitive():
    srcs = [example("A=\n"), local("A=1\ndebug=1\n")]
    apply_ignores(srcs, ["DEBUG"])
    assert triples(compare(srcs)) == [("extra", ".env", "debug")]


def test_counts():
    r = compare([example("A=\nB=\n"), local("A=\nA=\nC=1\n")])
    assert r.counts() == {"missing": 1, "extra": 1, "empty": 1, "duplicate": 1}
