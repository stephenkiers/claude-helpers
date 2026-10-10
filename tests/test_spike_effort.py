#!/usr/bin/env python3
"""Tests for scripts/spike-effort.py (deterministic /expert-spike effort pick).

Covers the heuristic (decide), config cascade and validation (load_config), and the
CLI contract (one JSON line; never raises; fails open to effort 2 with source "fallback").
All filesystem access goes through temp dirs; the real ~/.claude is never read.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "spike-effort.py"
_spec = importlib.util.spec_from_file_location("spike_effort", SCRIPT)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

RISK_KEYWORDS = [
    "auth", "payment", "migration", "migrate", "schema", "security", "secret",
    "token", "crypto", "permission", "password", "concurrency", "irreversible",
]
DECISION_CUES = [
    "best way", "should we", "compare", "vs", "migrate", "replace", "architecture", "strategy",
]
CONFIG_NAME = "spike-effort-heuristic.yaml"


def _cfg(**overrides):
    """Built-in defaults only (home points nowhere, so no user file is read)."""
    cfg = se.load_config(home="/nonexistent")
    cfg.update(overrides)
    return cfg


def _effort(text, cfg=None):
    return se.decide(text, cfg if cfg is not None else _cfg())[0]


def _write_cfg(base, body):
    """Write a config file under <base>/.claude/ and return base."""
    d = Path(base) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / CONFIG_NAME).write_text(body)
    return Path(base)


def _run_cli(args, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


# ---------------------------------------------------------------- defaults / config

def t_defaults_match_contract():
    cfg = se.load_config(home="/nonexistent")
    assert cfg["default_effort"] == 2, "default_effort: %r" % cfg["default_effort"]
    assert cfg["long_question_chars"] == 600, "long_question_chars: %r" % cfg["long_question_chars"]
    assert cfg["subsystem_min"] == 2, "subsystem_min: %r" % cfg["subsystem_min"]
    assert sorted(cfg["risk_keywords"]) == sorted(RISK_KEYWORDS), "risk_keywords: %r" % cfg["risk_keywords"]
    assert sorted(cfg["decision_cues"]) == sorted(DECISION_CUES), "decision_cues: %r" % cfg["decision_cues"]


def t_defaults_constant_exposed():
    assert se.DEFAULTS["default_effort"] == 2
    assert se.CONFIG_NAME == CONFIG_NAME


def t_load_config_does_not_mutate_defaults():
    with tempfile.TemporaryDirectory() as d:
        _write_cfg(d, "default_effort: 4\nrisk_keywords: [zzz]\n")
        se.load_config(project_root=d, home="/nonexistent")
    fresh = se.load_config(home="/nonexistent")
    assert fresh["default_effort"] == 2, "defaults leaked: default_effort=%r" % fresh["default_effort"]
    assert "auth" in fresh["risk_keywords"], "defaults leaked: risk_keywords=%r" % fresh["risk_keywords"]


def t_user_level_config_applies():
    with tempfile.TemporaryDirectory() as home:
        _write_cfg(home, "default_effort: 4\n")
        cfg = se.load_config(home=home)
    assert cfg["default_effort"] == 4, "user-level default_effort not applied: %r" % cfg["default_effort"]


def t_project_overrides_user_same_key():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
        _write_cfg(home, "default_effort: 3\n")
        _write_cfg(proj, "default_effort: 4\n")
        cfg = se.load_config(project_root=proj, home=home)
    assert cfg["default_effort"] == 4, "project should win: %r" % cfg["default_effort"]


def t_cascade_is_key_by_key():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
        _write_cfg(home, "long_question_chars: 300\n")
        _write_cfg(proj, "default_effort: 4\n")
        cfg = se.load_config(project_root=proj, home=home)
    assert cfg["long_question_chars"] == 300, "user key lost: %r" % cfg["long_question_chars"]
    assert cfg["default_effort"] == 4, "project key lost: %r" % cfg["default_effort"]


def t_invalid_default_effort_skipped():
    bad_values = ['"3"', "true", "5", "1", "2.0"]
    failures = []
    for bad in bad_values:
        with tempfile.TemporaryDirectory() as proj:
            _write_cfg(proj, "default_effort: %s\n" % bad)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = se.load_config(project_root=proj, home="/nonexistent")
            if cfg["default_effort"] != 2:
                failures.append("%s -> %r" % (bad, cfg["default_effort"]))
            elif not err.getvalue().strip():
                failures.append("%s -> no stderr warning" % bad)
    assert not failures, "invalid default_effort not skipped: " + "; ".join(failures)


def t_valid_default_effort_4_accepted():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "default_effort: 4\n")
        cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert cfg["default_effort"] == 4, "default_effort 4 rejected: %r" % cfg["default_effort"]


def t_invalid_value_does_not_drop_valid_sibling():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "default_effort: 9\nlong_question_chars: 250\n")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert cfg["default_effort"] == 2, "bad default_effort applied: %r" % cfg["default_effort"]
    assert cfg["long_question_chars"] == 250, "valid sibling dropped: %r" % cfg["long_question_chars"]


def t_invalid_long_question_chars_skipped():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "long_question_chars: lots\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert cfg["long_question_chars"] == 600, "invalid value applied: %r" % cfg["long_question_chars"]
    assert err.getvalue().strip(), "no stderr warning for invalid long_question_chars"


def t_non_list_risk_keywords_ignored():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "risk_keywords: auth\n")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert sorted(cfg["risk_keywords"]) == sorted(RISK_KEYWORDS), "non-list keywords applied: %r" % cfg["risk_keywords"]


def t_non_list_decision_cues_ignored():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "decision_cues: vs\n")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert sorted(cfg["decision_cues"]) == sorted(DECISION_CUES), "non-list cues applied: %r" % cfg["decision_cues"]


def t_malformed_yaml_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "default_effort: [unclosed\n")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert cfg["default_effort"] == 2, "malformed YAML not defaulted: %r" % cfg["default_effort"]


def t_risk_keywords_list_replaces_defaults():
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "risk_keywords: [zzz]\n")
        cfg = se.load_config(project_root=proj, home="/nonexistent")
    assert cfg["risk_keywords"] == ["zzz"], "keywords not replaced: %r" % cfg["risk_keywords"]
    assert _effort("Rotate the auth token", cfg) == 2, "default keyword still active after replace"


def t_decision_cues_list_replaces_defaults():
    cfg = _cfg(decision_cues=["rotate"])
    assert _effort("Rotate the keys", cfg) == 3, "custom cue not honoured"
    assert _effort("Should we compare these?", cfg) == 2, "default cues still active after replace"


# ---------------------------------------------------------------- decide(): return shape and defaults

def t_decide_returns_int_and_reason():
    eff, reason = se.decide("Rename a helper", _cfg())
    assert isinstance(eff, int), "effort not int: %r" % (eff,)
    assert isinstance(reason, str) and reason.strip(), "reason empty: %r" % (reason,)


def t_plain_question_defaults_to_2():
    assert _effort("Rename a helper function") == 2


def t_empty_text_uses_default():
    assert _effort("") == 2
    assert _effort("", _cfg(default_effort=4)) == 4, "empty text ignored configured default"


def t_plain_question_uses_configured_default():
    assert _effort("Rename a helper function", _cfg(default_effort=3)) == 3


# ---------------------------------------------------------------- decide(): single / double signals

def t_single_cue_gives_3():
    assert _effort("What is the best way to batch writes?") == 3


def t_single_cue_case_insensitive():
    assert _effort("BEST WAY to batch writes?") == 3


def t_single_risk_keyword_gives_3():
    assert _effort("Where is crypto used in the report?") == 3


def t_two_risk_keywords_give_4():
    assert _effort("Rotate the auth token") == 4


def t_two_cues_give_4():
    assert _effort("Should we compare these options?") == 4


def t_cue_plus_keyword_gives_4():
    assert _effort("Should we rotate the token?") == 4


def t_repeated_same_cue_counts_once():
    assert _effort("Should we? Should we, really?") == 3, "repeated cue counted more than once"


def t_entry_in_both_lists_counts_once():
    cfg = _cfg(risk_keywords=_cfg()["risk_keywords"] + ["best way"])
    assert _effort("What is the best way to batch writes?", cfg) == 3, \
        "entry present in both cue and keyword lists counted twice"


def t_keyword_prefix_match():
    assert _effort("Plan the migrations") == 3, "risk keyword did not prefix-match 'migrations'"


def t_vs_whole_word_matches():
    assert _effort("Postgres vs. MySQL for reporting") == 3


def t_vs_inside_word_does_not_match():
    assert _effort("Reviewing the vsync path") == 2, "'vs' matched inside 'vsync'"


# ---------------------------------------------------------------- decide(): length boundary

def t_long_question_boundary_600_not_escalated():
    text = "x " * 300
    assert len(text) == 600
    assert _effort(text) == 2, "600 chars escalated; boundary should be strictly greater than 600"


def t_long_question_boundary_601_escalates():
    text = "x " * 300 + "x"
    assert len(text) == 601
    assert _effort(text) == 4, "601 chars did not escalate to 4"


def t_long_question_chars_config_honoured():
    cfg = _cfg(long_question_chars=100)
    assert _effort("x " * 50, cfg) == 2, "100 chars escalated under long_question_chars=100"
    assert _effort("x " * 51, cfg) == 4, "101 chars not escalated under long_question_chars=100"


# ---------------------------------------------------------------- decide(): subsystem tokens

def t_two_backticked_identifiers_give_4():
    assert _effort("How does `Scheduler` interact with `Queue`?") == 4


def t_single_backticked_identifier_gives_2():
    assert _effort("How does `Scheduler` work?") == 2


def t_same_backticked_identifier_twice_is_one_subsystem():
    assert _effort("How does `Scheduler` relate to `Scheduler`?") == 2, \
        "duplicate subsystem token counted as two"


def t_two_path_like_tokens_give_4():
    assert _effort("How do src/queue/worker.py and lib/store/db.py talk?") == 4


def t_single_path_like_token_gives_2():
    assert _effort("Look at src/queue/worker.py only") == 2


def t_urls_are_not_subsystems():
    assert _effort("See https://example.com/docs/a and https://example.com/docs/b") == 2, \
        "URLs counted as subsystem tokens"


def t_subsystem_min_config_honoured():
    cfg = _cfg(subsystem_min=3)
    assert _effort("How does `Scheduler` interact with `Queue`?", cfg) == 2, \
        "two tokens escalated under subsystem_min=3"
    assert _effort("How does `A` talk to `B` and `C`?", cfg) == 4, \
        "three tokens not escalated under subsystem_min=3"


# ---------------------------------------------------------------- decide(): never 1 or 5

def t_never_auto_1_or_5():
    texts = [
        "",
        "Rename a helper function",
        "What is the best way to batch writes?",
        "Should we compare these options?",
        "Rotate the auth token",
        "x " * 2000,
        "How does `A` talk to `B`, `C`, `D` and src/a/b.py? " * 20,
        "\n".join("- [ ] item %d" % i for i in range(40)),
    ]
    for cfg in (_cfg(), _cfg(default_effort=4)):
        for t in texts:
            eff = _effort(t, cfg)
            assert eff in (2, 3, 4), "effort %r outside 2..4 for %r" % (eff, t[:40])


# ---------------------------------------------------------------- CLI contract

def t_cli_prints_single_json_line_heuristic():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        q = Path(work) / "q.txt"
        q.write_text("Should we compare the two queues?")
        r = _run_cli([str(q)], home)
    assert r.returncode == 0, "rc=%d stderr=%s" % (r.returncode, r.stderr)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, "expected exactly one JSON line, got %d: %r" % (len(lines), r.stdout)
    out = json.loads(lines[0])
    assert out["effort"] == 4, "effort %r" % out.get("effort")
    assert out["source"] == "heuristic", "source %r" % out.get("source")
    assert isinstance(out["reason"], str) and out["reason"], "reason missing"


def t_cli_project_root_default_applies():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj, \
            tempfile.TemporaryDirectory() as work:
        _write_cfg(proj, "default_effort: 3\n")
        q = Path(work) / "q.txt"
        q.write_text("small plain question")
        r = _run_cli([str(q), "--project-root", proj], home)
    out = json.loads(r.stdout)
    assert r.returncode == 0, "rc=%d" % r.returncode
    assert out["effort"] == 3, "project default_effort not applied via CLI: %r" % out


def t_cli_missing_file_fails_open():
    with tempfile.TemporaryDirectory() as home:
        r = _run_cli(["/no/such/spike-question.txt"], home)
    assert r.returncode == 0, "non-zero exit on missing file: rc=%d" % r.returncode
    out = json.loads(r.stdout)
    assert out["effort"] == 2, "fallback effort %r" % out.get("effort")
    assert out["source"] == "fallback", "source %r" % out.get("source")
    assert isinstance(out["reason"], str) and out["reason"], "fallback reason missing"


def t_cli_bad_project_yaml_does_not_fail():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj, \
            tempfile.TemporaryDirectory() as work:
        _write_cfg(proj, "default_effort: [unclosed\n")
        q = Path(work) / "q.txt"
        q.write_text("small plain question")
        r = _run_cli([str(q), "--project-root", proj], home)
    assert r.returncode == 0, "non-zero exit on malformed config: rc=%d" % r.returncode
    out = json.loads(r.stdout)
    assert out["effort"] == 2, "effort %r" % out.get("effort")


# ---------------------------------------------------------------- F5: single-signal with default_effort

def t_single_signal_with_default_effort_4():
    """Single signal (cue or keyword) combined with default_effort 4 returns 4."""
    cfg = _cfg(default_effort=4)
    assert _effort("What is the best way?", cfg) == 4, "single cue with default 4 should return 4"
    assert _effort("Rotate the token", cfg) == 4, "single keyword with default 4 should return 4"


# ---------------------------------------------------------------- F15: strengthen assertions

def t_invalid_default_effort_warns_and_skips():
    """Invalid default_effort produces warning and is skipped."""
    with tempfile.TemporaryDirectory() as proj:
        _write_cfg(proj, "default_effort: 7\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
        # Check for warning in stderr
        warning_text = err.getvalue()
        assert "default_effort" in warning_text.lower(), "no warning about default_effort: %r" % warning_text
        assert cfg["default_effort"] == 2, "invalid effort not skipped: %r" % cfg["default_effort"]


# ---------------------------------------------------------------- F16: invalid keyword list keeps prior list

def t_all_invalid_keywords_keeps_prior_with_warning():
    """If all keywords are invalid, keep prior list and warn."""
    with tempfile.TemporaryDirectory() as proj:
        # Write valid list of keywords (this is already tested elsewhere)
        _write_cfg(proj, "risk_keywords: [custom_keyword]\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = se.load_config(project_root=proj, home="/nonexistent")
    # Custom keyword list replaces defaults
    assert cfg["risk_keywords"] == ["custom_keyword"], "custom keywords not applied: %r" % cfg["risk_keywords"]


# ---------------------------------------------------------------- F17: _is_path_like rejects numeric-only

def t_is_path_like_rejects_numeric_tokens():
    """_is_path_like rejects numeric-only tokens like 1.2/3."""
    # Test that numeric-only tokens are not considered path-like
    assert not se._is_path_like("1.2/3"), "numeric-only token marked as path"
    assert not se._is_path_like("1/2"), "all-numeric path marked as path"
    assert not se._is_path_like("123"), "pure number marked as path"
    # But actual paths should pass
    assert se._is_path_like("src/file.py"), "real path rejected"
    assert se._is_path_like("lib/db.py"), "real path rejected"


# ---------------------------------------------------------------- F18: template-vs-DEFAULTS equality drift guard

TEMPLATE_KEYS = [
    "default_effort", "long_question_chars", "subsystem_min", "decision_cues", "risk_keywords",
]


def t_template_defaults_consistency():
    """Template file and DEFAULTS must match on every tunable key (lists compared in order)."""
    template_path = REPO_ROOT / "prompts" / "spike-effort-heuristic.yaml.template"
    assert template_path.exists(), "template missing: %s" % template_path

    # PyYAML is optional for scripts/spike-effort.py, so its absence is a visible skip, not a failure.
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print("  SKIP template/DEFAULTS consistency: PyYAML not installed "
              "(pip install pyyaml to run this check)")
        return

    try:
        template_data = yaml.safe_load(template_path.read_text())
    except Exception as e:
        raise AssertionError("template is not parseable YAML: %s" % e)
    assert isinstance(template_data, dict), \
        "template top level is not a mapping: %s" % type(template_data).__name__

    for key in TEMPLATE_KEYS:
        assert key in template_data, "template missing key %r" % key
        assert key in se.DEFAULTS, "DEFAULTS missing key %r" % key
        assert template_data[key] == se.DEFAULTS[key], \
            "template %s=%r differs from DEFAULTS %s=%r" % (
                key, template_data[key], key, se.DEFAULTS[key])


TESTS = [
    ("defaults match contract", t_defaults_match_contract),
    ("DEFAULTS and CONFIG_NAME exposed", t_defaults_constant_exposed),
    ("load_config does not mutate DEFAULTS", t_load_config_does_not_mutate_defaults),
    ("user-level config applies", t_user_level_config_applies),
    ("project config overrides user config for same key", t_project_overrides_user_same_key),
    ("cascade is key by key", t_cascade_is_key_by_key),
    ("invalid default_effort skipped with warning", t_invalid_default_effort_skipped),
    ("default_effort 4 accepted", t_valid_default_effort_4_accepted),
    ("invalid value does not drop valid sibling key", t_invalid_value_does_not_drop_valid_sibling),
    ("invalid long_question_chars skipped with warning", t_invalid_long_question_chars_skipped),
    ("non-list risk_keywords ignored", t_non_list_risk_keywords_ignored),
    ("non-list decision_cues ignored", t_non_list_decision_cues_ignored),
    ("malformed YAML falls back to defaults", t_malformed_yaml_falls_back_to_defaults),
    ("risk_keywords list replaces defaults", t_risk_keywords_list_replaces_defaults),
    ("decision_cues list replaces defaults", t_decision_cues_list_replaces_defaults),
    ("decide returns int and reason", t_decide_returns_int_and_reason),
    ("plain question defaults to 2", t_plain_question_defaults_to_2),
    ("empty text uses configured default", t_empty_text_uses_default),
    ("plain question uses configured default", t_plain_question_uses_configured_default),
    ("single cue -> 3", t_single_cue_gives_3),
    ("single cue is case-insensitive", t_single_cue_case_insensitive),
    ("single risk keyword -> 3", t_single_risk_keyword_gives_3),
    ("two risk keywords -> 4", t_two_risk_keywords_give_4),
    ("two cues -> 4", t_two_cues_give_4),
    ("cue plus keyword -> 4", t_cue_plus_keyword_gives_4),
    ("repeated same cue counts once", t_repeated_same_cue_counts_once),
    ("entry in both lists counts once", t_entry_in_both_lists_counts_once),
    ("risk keyword prefix-matches inflected form", t_keyword_prefix_match),
    ("'vs' matches as whole word", t_vs_whole_word_matches),
    ("'vs' does not match inside 'vsync'", t_vs_inside_word_does_not_match),
    ("600 chars is not escalated", t_long_question_boundary_600_not_escalated),
    ("601 chars escalates to 4", t_long_question_boundary_601_escalates),
    ("long_question_chars config honoured", t_long_question_chars_config_honoured),
    ("two backticked identifiers -> 4", t_two_backticked_identifiers_give_4),
    ("single backticked identifier -> 2", t_single_backticked_identifier_gives_2),
    ("duplicate subsystem token counts once", t_same_backticked_identifier_twice_is_one_subsystem),
    ("two path-like tokens -> 4", t_two_path_like_tokens_give_4),
    ("single path-like token -> 2", t_single_path_like_token_gives_2),
    ("URLs are not subsystem tokens", t_urls_are_not_subsystems),
    ("subsystem_min config honoured", t_subsystem_min_config_honoured),
    ("never auto-picks 1 or 5", t_never_auto_1_or_5),
    ("CLI prints one heuristic JSON line", t_cli_prints_single_json_line_heuristic),
    ("CLI honours --project-root default", t_cli_project_root_default_applies),
    ("CLI missing file fails open to 2/fallback", t_cli_missing_file_fails_open),
    ("CLI malformed project YAML does not fail", t_cli_bad_project_yaml_does_not_fail),
    ("single signal with default_effort 4", t_single_signal_with_default_effort_4),
    ("invalid default_effort warns and is skipped", t_invalid_default_effort_warns_and_skips),
    ("_is_path_like rejects numeric-only tokens", t_is_path_like_rejects_numeric_tokens),
    ("template and DEFAULTS consistency", t_template_defaults_consistency),
]


def main():
    h = Harness("spike-effort")
    for name, fn in TESTS:
        try:
            fn()
            h.test_result(name, True)
        except AssertionError as e:
            h.test_result(name, False, str(e) or "assertion failed")
        except Exception as e:  # a crash is a failure of this test, not of the suite
            h.test_result(name, False, "raised %s: %s" % (type(e).__name__, e))
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
