"""Run: python tests/test_plan_drift.py  (or pytest). Jev is mocked; git must be installed."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import plan_drift as pd

PLAN = """# Plan
text

```plan-checks
- [C1] src/a.py :: add retry
- [C2] ./src/b.py, docs\\ :: write docs
- [C3] - :: public API unchanged
- [C1] dup.py :: duplicate id is ignored
not a check line
```
"""


def test_parse_checks():
    checks = pd.parse_checks(PLAN)
    assert [c["id"] for c in checks] == ["C1", "C2", "C3"]
    assert checks[1]["files"] == ["src/b.py", "docs/"]
    assert checks[2]["files"] == []
    assert pd.parse_checks("no block here") == []


def test_matches():
    assert pd.matches("src/a.py", "src/a.py")
    assert pd.matches("docs/", "docs/x.md")
    assert pd.matches("src/*.py", "src/a.py")
    assert not pd.matches("src/a.py", "src/a.pyc")


def test_verdict():
    ans = lambda s, c: {"implemented": {"score": s}, "contradicts": {"noul": c}}
    assert pd.verdict(ans(2.0, 0.0)) == "OK"
    assert pd.verdict(ans(1.0, 0.0)) == "PARTIAL"
    assert pd.verdict(ans(0.1, 0.0)) == "MISSING"
    assert pd.verdict(ans(2.0, 0.9)) == "CONTRADICTS"


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def write(root, path, text):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


def test_end_to_end_with_mocked_jev():
    with tempfile.TemporaryDirectory() as root:
        git(root, "init", "-q")
        write(root, "src/a.py", "x = 1\n")
        write(root, "untracked_before.txt", "keep\n")
        base = pd.snapshot_tree(root)

        write(root, "src/a.py", "x = 2  # retry\n")  # planned change
        write(root, "other.py", "print('hi')\n")      # unplanned new file
        write(root, pd.STATE_DIR + "/report.md", "must be excluded\n")
        diff = pd.git(root, "diff", "--no-color", "--no-renames", base, pd.snapshot_tree(root))
        diffs = pd.split_diff(diff)
        assert sorted(diffs) == ["other.py", "src/a.py"], diffs
        # the real index is untouched
        assert pd.git(root, "status", "--porcelain").count("??") >= 1

        def fake_jev(state, questions):
            if "covered_by" in questions:
                unplanned = "print('hi')" in state["diff"]
                return {"covered_by": {"choice": "unplanned" if unplanned else "C1", "confidence": 0.9,
                                       "probabilities": {"unplanned": 0.95 if unplanned else 0.02}}}
            return {"implemented": {"score": 2.0, "confidence": 0.9}, "contradicts": {"noul": 0.0}}
        pd.jev = fake_jev

        results, files = pd.evaluate(pd.parse_checks(PLAN), diffs)
        by_id = {r["id"]: r for r in results}
        assert by_id["C1"]["verdict"] == "OK"
        assert by_id["C2"]["verdict"] == "MISSING" and by_id["C2"]["untouched"] == ["src/b.py", "docs/"]
        assert by_id["C3"]["verdict"] == "OK"
        report = pd.render(results, files)
        assert "`other.py` (p=0.95, not listed in any item)" in report
        assert "- `docs/`" in report
        assert pd.summary(results, files).startswith("plan-drift: 2/3 OK, 1 MISSING, 1 unplanned file(s)")


def test_history_and_stats():
    import json
    with tempfile.TemporaryDirectory() as root:
        git(root, "init", "-q")
        os.makedirs(os.path.join(root, pd.STATE_DIR))
        ev = lambda pid, checks, unplanned: {"type": "eval", "plan_id": pid, "session_id": "sess1234abcd",
                                             "checks": checks, "unplanned": unplanned, "untouched": []}
        pd.append_history(root, ev("2026-01-01T00:00:00+00:00", {"C1": "MISSING"}, []))  # superseded
        pd.append_history(root, ev("2026-01-01T00:00:00+00:00", {"C1": "OK", "C2": "PARTIAL"}, ["x.py"]))
        pd.append_history(root, ev("2026-01-02T00:00:00+00:00", {"C1": "OK", "C2": "OK"}, []))
        with open(os.path.join(root, pd.STATE_DIR, "current.json"), "w") as f:
            json.dump({"created_at": "2026-01-01T00:00:00+00:00"}, f)
        cwd = os.getcwd()
        os.chdir(root)
        try:
            pd.cmd_triage(["C2=misjudged", "file:x.py=unintended"])
            try:
                pd.cmd_triage(["C1=bogus"])
                assert False, "invalid class must be rejected"
            except SystemExit:
                pass
            plans = pd.load_history(root)
            assert [len(p["eval"]["checks"]) for p in plans] == [2, 2]
            m = pd.plan_metrics(plans[0])
            assert (m["drift"], m["misjudged"], m["triaged"]) == (2, 1, 2), m
            pd.cmd_stats()
        finally:
            os.chdir(cwd)
        with open(os.path.join(root, pd.STATE_DIR, "stats.html"), encoding="utf-8") as f:
            page = f.read()
        assert "<svg" in page and "<td>100%</td>" in page and "<b>50%</b>" in page  # plan 1 rate; 2 drift / 4 items


def test_stop_hook_only_in_owning_session():
    import contextlib
    import io
    import json
    with tempfile.TemporaryDirectory() as root:
        git(root, "init", "-q")
        write(root, "src/a.py", "x = 1\n")
        pd.cmd_snapshot({"cwd": root, "session_id": "A", "tool_input": {"plan": PLAN}})
        write(root, "src/a.py", "x = 2  # retry\n")
        os.environ["TYPESAFE_API_KEY"] = "test"
        pd.jev = lambda state, questions: (
            {"covered_by": {"choice": "C1", "probabilities": {}}} if "covered_by" in questions
            else {"implemented": {"score": 2.0}, "contradicts": {"noul": 0.0}})

        def stop(transcript):
            path = os.path.join(root, "transcript.jsonl")
            write(root, "transcript.jsonl", transcript)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                pd.cmd_check({"cwd": root, "session_id": "B", "transcript_path": path}, True)
            with open(pd.state_path(root), encoding="utf-8") as f:
                return out.getvalue(), json.load(f)

        out, data = stop('{"type":"user","message":"unrelated task"}\n')
        assert out == "" and data["session_id"] == "A" and "last_diff_hash" not in data
        out, data = stop('{"type":"user","message":"Implement the following plan: ... add retry ..."}\n')
        assert "plan-drift:" in out and data["session_id"] == "B"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
