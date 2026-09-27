"""plan-drift: check that the code written after a Plan-mode plan matches the plan, using TypeSafe Jev.

Subcommands (hooks feed the hook JSON on stdin):
  inject    UserPromptSubmit  - in plan mode, tell Claude to end the plan with a plan-checks block
  gate      PreToolUse        - deny ExitPlanMode when the plan has no valid plan-checks block
  snapshot  PostToolUse       - save the plan checks and a snapshot of the working tree
  check     /plan-drift:check - diff against the snapshot, ask Jev, print the report
  check --hook  Stop          - same, but only a one-line systemMessage and only when the diff changed,
                and only in the session that owns the plan
  triage REF=CLASS ...        - record Claude's classification of the drift items (from /plan-drift:check)
  stats     /plan-drift:stats - aggregate history.jsonl over all plans into stats.html
"""
import concurrent.futures
import fnmatch
import glob
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = os.environ.get("PLAN_DRIFT_MODEL", "jev-1.13.0")  # pinned: thresholds below are tuned against it
STATE_DIR = ".claude/plan-drift"
MAX_FILE_CHARS = 12_000
MAX_STATE_CHARS = 60_000  # well under Jev's 64k-token input limit
# ponytail: fixed thresholds, make them env-configurable if they need per-repo tuning
OK_SCORE, MISSING_SCORE, CONTRADICT, LOW_CONFIDENCE = 1.5, 0.5, 0.5, 0.6
TRIAGE_CLASSES = ("unintended", "intentional", "misjudged")

CHECKS_FORMAT = """End the plan with a fenced `plan-checks` block that lists what must be true of the code after implementation, one verifiable item per line:

```plan-checks
- [C1] src/api/client.py :: fetch() retries with exponential backoff, at most 3 attempts
- [C2] src/api/client.py, tests/test_client.py :: RetryError is raised after the last attempt and a test covers it
- [C3] - :: No public function signature in src/api/ changes
```

Rules: `[ID]` unique; files are comma-separated paths, directories or globs relative to the repo root, or `-` for the whole diff; the text after `::` must be judgeable from the diff alone, literal and specific (state negations and scope explicitly). Constraints (things that must NOT change) use `-`."""

BLOCK_RE = re.compile(r"```plan-checks[^\n]*\n(.*?)```", re.S)
LINE_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.*?)\s*::\s*(.+?)\s*$")


# ---------- plan parsing ----------

def parse_checks(plan):
    m = BLOCK_RE.search(plan or "")
    if not m:
        return []
    checks, seen = [], set()
    for line in m.group(1).splitlines():
        lm = LINE_RE.match(line)
        if not lm or lm.group(1) in seen:
            continue
        cid, files, expect = lm.groups()
        seen.add(cid)
        files = [] if files.strip() in ("", "-") else [norm(f) for f in files.split(",") if f.strip()]
        checks.append({"id": cid, "files": files, "expect": expect})
    return checks[:254]  # Jev Choice allows 255 options, one is "unplanned"


def norm(path):
    path = path.strip().strip("`").replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def matches(pattern, path):
    return path == pattern or path.startswith(pattern.rstrip("/") + "/") or fnmatch.fnmatch(path, pattern)


def plan_text(hook):
    """ExitPlanMode's tool_input may carry the plan inline or only point at the plan file."""
    ti = hook.get("tool_input") or {}
    if ti.get("plan"):
        return ti["plan"]
    path = ti.get("planFilePath") or ti.get("plan_file_path")
    if not path:
        files = glob.glob(os.path.expanduser("~/.claude/plans/*.md"))
        path = max(files, key=os.path.getmtime) if files else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


# ---------- git ----------

def git(root, *args, env=None):
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, env=env,
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")


def repo_root(cwd):
    try:
        return git(cwd, "rev-parse", "--show-toplevel").strip()
    except (subprocess.CalledProcessError, OSError):  # not a repo, bad cwd, or git missing
        return None


def snapshot_tree(root):
    """Tree object of the whole working tree (untracked included), without touching HEAD or the real index."""
    fd, tmp_index = tempfile.mkstemp(prefix="plan-drift-index-")
    os.close(fd)
    try:
        real_index = os.path.join(root, git(root, "rev-parse", "--git-path", "index").strip())
        if os.path.exists(real_index):  # reuse its stat cache so unchanged files aren't rehashed
            with open(real_index, "rb") as src, open(tmp_index, "wb") as dst:
                dst.write(src.read())
        else:
            os.remove(tmp_index)
        env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
        git(root, "add", "-A", "--", ".", f":(exclude){STATE_DIR}", env=env)
        return git(root, "write-tree", env=env).strip()
    finally:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)


def split_diff(text):
    """{path: diff} from `git diff` output, each file's diff capped at MAX_FILE_CHARS."""
    out = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        if not chunk.startswith("diff --git "):
            continue
        header = chunk.split("\n", 1)[0]
        path = header[header.rindex(" b/") + 3:]
        out[path] = chunk if len(chunk) <= MAX_FILE_CHARS else chunk[:MAX_FILE_CHARS] + "\n... (truncated)\n"
    return out


def join_capped(diffs):
    text = "".join(diffs)
    return text if len(text) <= MAX_STATE_CHARS else text[:MAX_STATE_CHARS] + "\n... (truncated)\n"


# ---------- Jev ----------

def jev(state, questions):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
        "Content-Type": "application/json",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["answers"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 529) or attempt == 3:
                raise RuntimeError(f"Jev HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from e
        time.sleep(2 ** attempt)


def ask_check(check, diff):
    return jev({"plan_item": check["expect"], "diff": diff}, {
        "implemented": {
            "type": "score",
            "instructions": "How completely does the code change in `diff` accomplish what `plan_item` describes?",
            "criteria": [
                "The diff does not do what `plan_item` describes",
                "The diff does only part of what `plan_item` describes",
                "The diff fully does what `plan_item` describes, or `plan_item` is a constraint that the diff respects",
            ],
        },
        "contradicts": {
            "type": "noul",
            "instructions": "The code change in `diff` does the opposite of `plan_item`, or violates a constraint stated in `plan_item`.",
        },
    })


def ask_file(checks, diff):
    criteria = {c["id"]: c["expect"] for c in checks}
    criteria["unplanned"] = "None of the other options: the change is not described by any of them"
    return jev({"diff": diff}, {
        "covered_by": {
            "type": "choice",
            "instructions": "Which planned change does the code change in `diff` implement?",
            "criteria": criteria,
        },
    })


def verdict(ans):
    score = ans["implemented"]["score"]
    if ans["contradicts"]["noul"] >= CONTRADICT:
        return "CONTRADICTS"
    if score >= OK_SCORE:
        return "OK"
    return "PARTIAL" if score >= MISSING_SCORE else "MISSING"


def evaluate(checks, diffs):
    """Returns (check results, file results). All counting/aggregation stays in code; Jev only judges."""
    results, jobs = [], {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for c in checks:
            if c["files"]:
                rel = [d for p, d in diffs.items() if any(matches(f, p) for f in c["files"])]
            else:
                rel = list(diffs.values())
            r = {**c, "untouched": [f for f in c["files"] if not any(matches(f, p) for p in diffs)]}
            results.append(r)
            if rel:
                jobs[pool.submit(ask_check, c, join_capped(rel))] = r
            else:
                r.update(verdict="MISSING", score=0.0, confidence=1.0, note="no diff in listed files")
        files = {p: {"path": p, "listed": any(matches(f, p) for c in checks for f in c["files"])} for p in diffs}
        for p, d in diffs.items():
            jobs[pool.submit(ask_file, checks, d)] = files[p]
        for fut in concurrent.futures.as_completed(jobs):
            target, ans = jobs[fut], fut.result()
            if "covered_by" in ans:
                cb = ans["covered_by"]
                target.update(choice=cb["choice"], p_unplanned=cb["probabilities"].get("unplanned", 0.0),
                              confidence=cb.get("confidence", 1.0))
            else:
                target.update(verdict=verdict(ans), score=ans["implemented"]["score"],
                              contradicts=ans["contradicts"]["noul"],
                              confidence=ans["implemented"].get("confidence", 1.0))
    return results, list(files.values())


# ---------- report ----------

ICON = {"OK": "✅", "PARTIAL": "⚠️", "MISSING": "❌", "CONTRADICTS": "❌"}


def render(results, files):
    lines = ["# Plan drift report", "", f"_{datetime.now().astimezone():%Y-%m-%d %H:%M} · {MODEL}_", "",
             "## Plan items", "", "| ID | Item | Verdict | implemented (0-2) | confidence |", "|---|---|---|---|---|"]
    for r in results:
        low = " 👀" if r["confidence"] < LOW_CONFIDENCE else ""
        note = f" ({r['note']})" if r.get("note") else ""
        item = r["expect"].replace("|", r"\|")
        lines.append(f"| {r['id']} | {item} | {ICON[r['verdict']]} {r['verdict']}{note}{low} "
                     f"| {r['score']:.2f} | {r['confidence']:.2f} |")
    unplanned = [f for f in files if f["choice"] == "unplanned"]
    lines += ["", "## Unplanned changes", ""]
    lines += [f"- `{f['path']}` (p={f['p_unplanned']:.2f}{'' if f['listed'] else ', not listed in any item'})"
              for f in unplanned] or ["- none"]
    untouched = sorted({f for r in results for f in r["untouched"]})
    lines += ["", "## Planned files with no changes", ""]
    lines += [f"- `{f}`" for f in untouched] or ["- none"]
    lines += ["", "👀 = low confidence, check by eye."]
    return "\n".join(lines) + "\n"


def summary(results, files):
    counts = {v: sum(r["verdict"] == v for r in results) for v in ICON}
    parts = [f"{counts['OK']}/{len(results)} OK"] + [f"{n} {v}" for v, n in counts.items() if n and v != "OK"]
    n_unplanned = sum(f["choice"] == "unplanned" for f in files)
    if n_unplanned:
        parts.append(f"{n_unplanned} unplanned file(s)")
    return "plan-drift: " + ", ".join(parts) + " — /plan-drift:check for details"


# ---------- subcommands ----------

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def cmd_inject(hook):
    if hook.get("permission_mode") == "plan":
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": CHECKS_FORMAT}})


def cmd_gate(hook):
    plan = plan_text(hook)
    if plan and not parse_checks(plan):  # no plan text found at all -> don't block the user
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
              "permissionDecisionReason": "plan-drift: the plan has no valid plan-checks block. " + CHECKS_FORMAT}})


def cmd_snapshot(hook):
    root = repo_root(hook.get("cwd") or os.getcwd())
    plan = plan_text(hook)
    checks = parse_checks(plan)
    if not root or not checks:
        return
    os.makedirs(os.path.join(root, STATE_DIR), exist_ok=True)
    with open(os.path.join(root, STATE_DIR, ".gitignore"), "w") as f:
        f.write("*\n")  # keep plan-drift state out of the user's git status
    save(root, {"session_id": hook.get("session_id"), "created_at": datetime.now(timezone.utc).isoformat(),
                "base_tree": snapshot_tree(root), "plan": plan, "checks": checks})


def state_path(root):
    return os.path.join(root, STATE_DIR, "current.json")


def save(root, data):
    with open(state_path(root), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def carries_plan(transcript_path, checks):
    """True when the session was started from the approved plan (ExitPlanMode's "clear context" option)."""
    if not transcript_path or not checks or not os.path.exists(transcript_path):
        return False
    marker = json.dumps(checks[0]["expect"], ensure_ascii=False)[1:-1]  # as it appears inside the JSONL
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        return any(marker in line for line in f)


def cmd_check(hook, as_hook):
    root = repo_root(hook.get("cwd") or os.getcwd())
    if not root or not os.path.exists(state_path(root)):
        if not as_hook:
            print("plan-drift: no plan snapshot. Approve a plan (ExitPlanMode) in a git repo first.")
        return
    if not os.environ.get("TYPESAFE_API_KEY"):
        if not as_hook:
            print("plan-drift: TYPESAFE_API_KEY is not set.")
        return
    with open(state_path(root), encoding="utf-8") as f:
        data = json.load(f)
    sid = hook.get("session_id")
    if as_hook and sid != data.get("session_id"):
        if not carries_plan(hook.get("transcript_path"), data["checks"]):
            return  # snapshot belongs to another, finished session
        data["session_id"] = sid  # plan handed off via "clear context": adopt it
    diff = git(root, "diff", "--no-color", "--no-renames", data["base_tree"], snapshot_tree(root))
    digest = hashlib.sha256(diff.encode()).hexdigest()
    if as_hook and digest == data.get("last_diff_hash"):
        return
    data["last_diff_hash"] = digest
    save(root, data)
    if not diff.strip():
        if not as_hook:
            print("plan-drift: no changes since the plan was approved.")
        return
    try:
        results, files = evaluate(data["checks"], split_diff(diff))
    except Exception as e:  # network/API failure must never break the session
        if as_hook:
            emit({"systemMessage": f"plan-drift: Jev call failed: {e}"})
        else:
            print(f"plan-drift: Jev call failed: {e}")
        return
    append_history(root, {
        "type": "eval", "plan_id": data["created_at"], "session_id": data.get("session_id"),
        "at": datetime.now(timezone.utc).isoformat(), "checks": {r["id"]: r["verdict"] for r in results},
        "n_files": len(files), "unplanned": [f["path"] for f in files if f["choice"] == "unplanned"],
        "untouched": sorted({f for r in results for f in r["untouched"]})})
    report = render(results, files)
    with open(os.path.join(root, STATE_DIR, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    if as_hook:
        emit({"systemMessage": summary(results, files)})
    else:
        print(report)
        print(f'Record your classification with: python "{os.path.abspath(__file__)}" triage '
              f'<ID|file:PATH>=<{"|".join(TRIAGE_CLASSES)}> ...')


# ---------- history & stats ----------

def append_history(root, rec):
    with open(os.path.join(root, STATE_DIR, "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cmd_triage(args):
    root = repo_root(os.getcwd())
    if not root or not os.path.exists(state_path(root)):
        sys.exit("plan-drift: no plan snapshot.")
    items = {}
    for a in args:
        ref, _, cls = a.rpartition("=")
        if not ref or cls not in TRIAGE_CLASSES:
            sys.exit(f"plan-drift: bad triage item {a!r}, expected REF=<{'|'.join(TRIAGE_CLASSES)}>")
        items[ref] = cls
    with open(state_path(root), encoding="utf-8") as f:
        plan_id = json.load(f)["created_at"]
    append_history(root, {"type": "triage", "plan_id": plan_id,
                          "at": datetime.now(timezone.utc).isoformat(), "items": items})
    print(f"plan-drift: recorded {len(items)} triage item(s).")


def load_history(root):
    """Plans in chronological order, each with its latest eval and its merged triage."""
    path = os.path.join(root, STATE_DIR, "history.jsonl")
    if not os.path.exists(path):
        return []
    plans = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:  # a torn line from an interrupted write
                continue
            p = plans.setdefault(rec["plan_id"], {"plan_id": rec["plan_id"], "eval": None, "triage": {}})
            if rec["type"] == "eval":
                p["eval"] = rec
            else:
                p["triage"].update(rec["items"])
    return sorted((p for p in plans.values() if p["eval"]), key=lambda p: p["plan_id"])


def plan_metrics(p):
    e = p["eval"]
    m = {v: sum(x == v for x in e["checks"].values()) for v in ICON}
    m.update({c: sum(x == c for x in p["triage"].values()) for c in TRIAGE_CLASSES})
    m.update(checks=len(e["checks"]), unplanned=len(e["unplanned"]), untouched=len(e["untouched"]),
             triaged=sum(m[c] for c in TRIAGE_CLASSES))
    m["drift"] = m["checks"] - m["OK"] + m["unplanned"]
    # only misjudged items that are still drift in the latest eval, so stale triage can't over-subtract
    flagged = {i for i, v in e["checks"].items() if v != "OK"} | {"file:" + f for f in e["unplanned"]}
    mis = [r for r, c in p["triage"].items() if c == "misjudged" and r in flagged]
    m["adj_drift"] = m["drift"] - len(mis)
    for k in ("PARTIAL", "MISSING", "CONTRADICTS"):  # misjudged share of each drift bar segment
        m["mis_" + k] = sum(e["checks"].get(r) == k for r in mis)
    m["mis_unplanned"] = sum(r.startswith("file:") for r in mis)
    return m


def ratio(a, b):
    return a / b if b else None


def pct(r):
    return "–" if r is None else f"{r:.0%}"


# chart geometry and series: verdicts use the fixed status palette, unplanned a categorical slot
W, H, PL, PR, PT, PB = 720, 220, 40, 8, 8, 24
BARS = [("OK", "good"), ("PARTIAL", "warning"), ("MISSING", "serious"), ("CONTRADICTS", "critical"),
        ("unplanned", "unplanned")]
LINES = [("drift rate", "s1"), ("Jev misjudged rate", "s2"), ("adjusted drift rate", "s3")]
MAX_CHART_PLANS = 50  # ponytail: charts show only the last 50 plans (bars get too thin); paginate if needed


def svg_frame(labels, ymax, fmt):
    pw, ph = W - PL - PR, H - PT - PB
    out = []
    for i in range(5):
        y = PT + ph - ph * i / 4
        out.append(f'<line x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}" class="{"base" if i == 0 else "grid"}"/>'
                   f'<text x="{PL - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{fmt(ymax * i / 4)}</text>')
    step, every = pw / len(labels), math.ceil(len(labels) / 12)
    for i, lab in enumerate(labels):
        if i % every == 0:
            out.append(f'<text x="{PL + step * (i + .5):.1f}" y="{H - 6}" class="ax" text-anchor="middle">{lab}</text>')
    return out, step, ph


def hit(i, step, tip):
    """A full-height transparent column: a hover target larger than the mark, with a native tooltip."""
    return (f'<rect x="{PL + step * i:.1f}" y="{PT}" width="{step:.1f}" height="{H - PT - PB}" class="hit">'
            f'<title>{html.escape(tip)}</title></rect>')


def bar_chart(rows):
    ymax = max(4, math.ceil(max(sum(m[k] for k, _ in BARS) for _, m in rows) / 4) * 4)
    out, step, ph = svg_frame([f"#{n}" for n, _ in rows], ymax, lambda v: f"{v:.0f}")
    out.insert(0, '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
                  'patternTransform="rotate(45)"><path d="M0 0V6" class="hatch-line"/></pattern></defs>')
    out += [hit(i, step, f"#{n}: " + ", ".join(f"{k} {m[k]}" for k, _ in BARS)
                + f", misjudged {m['drift'] - m['adj_drift']}") for i, (n, m) in enumerate(rows)]
    bw = min(28, step * 0.6)
    for i, (n, m) in enumerate(rows):
        x, y = PL + step * (i + .5) - bw / 2, PT + ph
        for key, cls in BARS:
            h = ph * m[key] / ymax
            if h:
                y -= h
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" class="f-{cls}"/>')
                mh = ph * m.get("mis_" + key, 0) / ymax  # hatch the misjudged part at the segment's bottom
                if mh:
                    out.append(f'<rect x="{x:.1f}" y="{y + h - mh:.1f}" width="{bw:.1f}" height="{mh:.1f}" '
                               'class="f-hatch"/>')
    return svg(out, "Plan items by verdict and unplanned files per plan; hatched = triaged as Jev misjudged")


def line_chart(rows):
    series = [[ratio(m["drift"], m["checks"]) for _, m in rows], [ratio(m["misjudged"], m["triaged"]) for _, m in rows],
              [ratio(m["adj_drift"], m["checks"]) for _, m in rows]]
    top = max([r for s in series for r in s if r is not None] + [1])
    ymax = math.ceil(top)  # whole multiples of 100% keep ticks at round 25% steps
    out, step, ph = svg_frame([f"#{n}" for n, _ in rows], ymax, lambda v: f"{v:.0%}")
    out += [hit(i, step, f"#{n}: " + ", ".join(f"{lab} {pct(s[i])}" for s, (lab, _) in zip(series, LINES)))
            for i, (n, _) in enumerate(rows)]
    for vals, (_, cls) in zip(series, LINES):
        pts = [(PL + step * (i + .5), PT + ph - ph * r / ymax) for i, r in enumerate(vals) if r is not None]
        if len(pts) > 1:
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}" class="l-{cls}"/>')
        out += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="d-{cls}"/>' for x, y in pts]
    return svg(out, "Drift rate and Jev misjudged rate per plan")


def svg(parts, label):
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{label}">{"".join(parts)}</svg>'


def legend(items):
    return '<div class="legend">' + "".join(f'<span><i class="f-{c}"></i>{k}</span>' for k, c in items) + "</div>"


STATS_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--base:#c3c2b7;--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;
--unplanned:#4a3aa7;--s1:#2a78d6;--s2:#eb6834;--s3:#1a9e8f}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;
--ink2:#c3c2b7;--grid:#2c2c2a;--base:#383835;--unplanned:#9085e9;--s1:#3987e5;--s2:#d95926;--s3:#2bb5a4}}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}.sub{color:var(--ink2);margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:20px}
.tile,.card{background:var(--surface);border:1px solid var(--grid);border-radius:8px;padding:12px 16px}
.tile b{display:block;font-size:26px;font-variant-numeric:tabular-nums}.tile span{color:var(--ink2);font-size:12px}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--grid)}.base{stroke:var(--base)}
.ax{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
rect[class^=f-]{stroke:var(--surface);stroke-width:2}.hit{fill:transparent}.hit:hover{fill:var(--grid);fill-opacity:.4}
polyline{fill:none;stroke-width:2}polyline,circle,rect[class^=f-]{pointer-events:none}circle{stroke:var(--surface);stroke-width:2}
.f-good{fill:var(--good);background:var(--good)}.f-warning{fill:var(--warning);background:var(--warning)}
.f-serious{fill:var(--serious);background:var(--serious)}.f-critical{fill:var(--critical);background:var(--critical)}
.f-unplanned{fill:var(--unplanned);background:var(--unplanned)}.f-s1{background:var(--s1)}.f-s2{background:var(--s2)}
.f-s3{background:var(--s3)}.l-s1{stroke:var(--s1)}.l-s2{stroke:var(--s2)}.l-s3{stroke:var(--s3)}
.d-s1{fill:var(--s1)}.d-s2{fill:var(--s2)}.d-s3{fill:var(--s3)}
.hatch-line{stroke:var(--surface);stroke-width:3}.f-hatch{fill:url(#hatch);
background:repeating-linear-gradient(45deg,var(--surface) 0 2px,var(--muted) 2px 5px)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;color:var(--ink2);font-size:12px;margin-bottom:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:6px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{color:var(--ink2);font-weight:600;font-size:12px}th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}
"""


def render_stats(plans):
    rows = [(i + 1, plan_metrics(p)) for i, p in enumerate(plans)]
    tot = {k: sum(m[k] for _, m in rows) for k in rows[0][1]} if rows else {}
    tiles = [("plans", len(rows)), ("plan items", tot.get("checks", 0)), ("drift detected", tot.get("drift", 0)),
             ("drift rate (drift / items)", pct(ratio(tot.get("drift", 0), tot.get("checks", 0)))),
             ("adjusted drift rate ((drift − misjudged) / items)",
              pct(ratio(tot.get("adj_drift", 0), tot.get("checks", 0)))),
             ("Jev misjudged rate", pct(ratio(tot.get("misjudged", 0), tot.get("triaged", 0)))
              + f' <span>({tot.get("misjudged", 0)}/{tot.get("triaged", 0)} triaged)</span>')]
    head = ["#", "plan approved", "session", "items", "OK", "PARTIAL", "MISSING", "CONTRADICTS", "unplanned",
            "untouched", "drift rate", "adj. drift rate", "unintended", "intentional", "misjudged"]
    body = []
    for (n, m), p in zip(rows, plans):
        when = datetime.fromisoformat(p["plan_id"]).astimezone().strftime("%Y-%m-%d %H:%M")
        cells = [n, when, (p["eval"].get("session_id") or "")[:8], m["checks"], m["OK"], m["PARTIAL"], m["MISSING"],
                 m["CONTRADICTS"], m["unplanned"], m["untouched"], pct(ratio(m["drift"], m["checks"])),
                 pct(ratio(m["adj_drift"], m["checks"])), m["unintended"], m["intentional"], m["misjudged"]]
        body.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells) + "</tr>")
    recent = rows[-MAX_CHART_PLANS:]
    charts = "" if not rows else (
        f'<h2>Drift per plan</h2><div class="card">{legend(BARS + [("misjudged (not drift)", "hatch")])}'
        f'{bar_chart(recent)}</div>'
        f'<h2>Rates per plan</h2><div class="card">{legend(LINES)}{line_chart(recent)}</div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plan drift stats</title>
<style>{STATS_CSS}</style></head><body><main>
<h1>Plan drift stats</h1><p class="sub">{datetime.now().astimezone():%Y-%m-%d %H:%M} · latest check per plan ·
drift = non-OK items + unplanned files · adjusted = drift minus items triaged misjudged · hover a column for values</p>
<div class="tiles">{"".join(f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for k, v in tiles)}</div>
{charts}<h2>Plans</h2><div class="card scroll"><table><thead><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr></thead>
<tbody>{"".join(body) or '<tr><td colspan="15">no history yet</td></tr>'}</tbody></table></div>
</main></body></html>
"""


def cmd_stats():
    root = repo_root(os.getcwd())
    if not root:
        print("plan-drift: not a git repository.")
        return
    plans = load_history(root)
    os.makedirs(os.path.join(root, STATE_DIR), exist_ok=True)
    out = os.path.join(root, STATE_DIR, "stats.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_stats(plans))
    ms = [plan_metrics(p) for p in plans]
    tot = {k: sum(m[k] for m in ms) for k in ("checks", "drift", "adj_drift", "misjudged", "triaged")}
    print(f"plan-drift stats: {len(plans)} plan(s), {tot['checks']} item(s), {tot['drift']} drift detected "
          f"(rate {pct(ratio(tot['drift'], tot['checks']))}, adjusted drift rate "
          f"{pct(ratio(tot['adj_drift'], tot['checks']))}), Jev misjudged {tot['misjudged']}/{tot['triaged']} "
          f"triaged ({pct(ratio(tot['misjudged'], tot['triaged']))})")
    print(f"report: {out}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    sub = args[0] if args else ""
    if sub == "triage":
        return cmd_triage(args[1:])
    if sub == "stats":
        return cmd_stats()
    as_hook = sub != "check" or "--hook" in args
    hook = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}") if as_hook else {}
    if sub == "check":
        cmd_check(hook, as_hook)
    elif sub in ("inject", "gate", "snapshot"):
        globals()["cmd_" + sub](hook)
    else:
        sys.exit(f"unknown subcommand: {sub!r}")


if __name__ == "__main__":
    main()
