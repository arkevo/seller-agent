#!/usr/bin/env python3
"""Stage stamps for the batch workflow commands and the pipeline driver.

Contract: ~/.claude/docs/workflow-stage-runs.md. This script is the only
writer of `stage_runs` entries.

  stage_stamp.py start (--epic ID | --key KEY) --stage STAGE
  stage_stamp.py end --epic ID [--key KEY] --stage STAGE --done IDS --left IDS
                     [--owner-pending IDS] [--sequence-file PATH]

`start` records the start time in
<git common dir>/workflow-stage-start/<epic or key>--<stage>.txt.
`end` appends one verified entry to the `stage_runs` list of the epic's
newest planning-sequence machine block, removes the start markers it read,
and prints `file: <path>` and, last, a `commit-ok: ...` line.

IDS are comma- and/or whitespace-separated; an empty string is no ids.
Exit codes: 0 stamped, 1 could not stamp (file left untouched), 2 usage.
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

STAGES = ("planning-sequence", "writing-plans", "execute-spikes",
          "execution-sequence", "execute-plans", "ship-epic")
MARKER_DIR = "workflow-stage-start"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FENCE = re.compile(r"(?ms)^(```+)json[ \t]*\n(.*?)^\1[ \t]*$")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
NOT_RECORDED = "start not recorded"


class StampError(Exception):
    pass


# ---- git -----------------------------------------------------------------

def git(*args, cwd=None):
    """stdout of a git command, or None when it fails."""
    try:
        out = subprocess.run(["git"] + list(args), cwd=cwd,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def repo_root():
    root = git("rev-parse", "--show-toplevel")
    if not root:
        raise StampError("not inside a git checkout")
    return pathlib.Path(root).resolve()


def common_dir(root):
    common = git("rev-parse", "--git-common-dir", cwd=root)
    if not common:
        raise StampError("cannot read the git common directory")
    return (root / common).resolve()


def default_branch(root):
    ref = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=root)
    if ref:
        return ref.rsplit("refs/remotes/origin/", 1)[-1]
    for name in ("master", "main"):
        if git("rev-parse", "-q", "--verify", "refs/heads/" + name,
               cwd=root) is not None:
            return name
    return "master"


def commit_ok(root, rel):
    """The contract's commit-ok line, judged before the file is written."""
    branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=root)
    if not branch:
        return "commit-ok: no (detached HEAD)"
    if branch == default_branch(root):
        return "commit-ok: no (on the default branch)"
    status = git("status", "--porcelain", "--", rel, cwd=root)
    if status is None:
        return "commit-ok: no (git status failed)"
    if not status:
        return "commit-ok: yes"
    if status.startswith("??"):
        return "commit-ok: yes (new file)"
    return "commit-ok: no (file had uncommitted changes)"


# ---- ids and times -------------------------------------------------------

def parse_ids(value):
    if value is None:
        return []
    ids = sorted({i for i in re.split(r"[\s,]+", value) if i})
    for i in ids:
        if not NAME_RE.fullmatch(i):
            raise StampError("bad id %r" % i)
    return ids


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime(TS_FORMAT)


def valid_ts(text):
    try:
        dt.datetime.strptime(text, TS_FORMAT)
        return True
    except (TypeError, ValueError):
        return False


def marker_path(common, name, stage):
    return common / MARKER_DIR / ("%s--%s.txt" % (name, stage))


# ---- the machine block ---------------------------------------------------

def machine_block(text, epic):
    """(start, end, data) of the first fenced json block with `tasks` whose
    `epic` is the epic; None when there is none."""
    for match in FENCE.finditer(text):
        try:
            data = json.loads(match.group(2))
        except ValueError:
            continue
        if (isinstance(data, dict) and "tasks" in data
                and data.get("epic") == epic):
            return match.start(2), match.end(2), data
    return None


def file_names_epic(path, epic):
    try:
        return machine_block(path.read_text(encoding="utf-8"), epic) is not None
    except (OSError, UnicodeDecodeError):
        return False


def newest(paths):
    def key(p):
        m = DATE_RE.match(p.name)
        return (m.group(1) if m else "", str(p))
    return max(paths, key=key) if paths else None


def find_sequence_file(root, epic, key, given):
    if given:
        path = (root / given).resolve()
        if not path.is_file():
            raise StampError("no such sequence file: %s" % given)
        if not file_names_epic(path, epic):
            raise StampError("%s has no machine block for %s" % (given, epic))
        return path
    plans = root / "docs" / "plans"
    if key:
        folder = plans / key
        found = [p for p in folder.glob("*-planning-sequence.md")
                 if p.is_file() and file_names_epic(p, epic)]
        if found:
            return newest(found)
    found = [p for p in plans.glob("**/*-planning-sequence.md")
             if p.is_file() and file_names_epic(p, epic)]
    if not found:
        raise StampError("no planning-sequence file names epic %s" % epic)
    return newest(found)


def _scan_string(s, i):
    """Index just past the JSON string starting at s[i] == '"'."""
    i += 1
    while True:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        else:
            i += 1


def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _scan_value(s, i):
    """Index just past the JSON value starting at s[i]."""
    c = s[i]
    if c == '"':
        return _scan_string(s, i)
    if c in "[{":
        depth = 0
        while True:
            c = s[i]
            if c == '"':
                i = _scan_string(s, i)
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    while i < len(s) and s[i] not in ",]} \t\r\n":
        i += 1
    return i


def top_level_spans(body):
    """{key: (value_start, value_end)} of the block's top-level object,
    plus the index of its opening brace."""
    i = _skip_ws(body, 0)
    if body[i] != "{":
        raise StampError("machine block is not an object")
    brace = i
    spans = {}
    i = _skip_ws(body, i + 1)
    while body[i] != "}":
        end = _scan_string(body, i)
        name = json.loads(body[i:end])
        i = _skip_ws(body, end)
        i = _skip_ws(body, i + 1)  # past ':'
        vend = _scan_value(body, i)
        spans[name] = (i, vend)
        i = _skip_ws(body, vend)
        if body[i] == ",":
            i = _skip_ws(body, i + 1)
    return brace, spans


def insert_entry(body, entry_text):
    """The block text with the entry appended to `stage_runs` (created as
    the first key when absent). Existing text is never rewritten."""
    brace, spans = top_level_spans(body)
    if "stage_runs" in spans:
        start, end = spans["stage_runs"]
        if body[start] != "[":
            raise StampError("stage_runs is not a list")
        inner = body[start + 1:end - 1]
        line_start = body.rfind("\n", 0, start) + 1
        indent = re.match(r"[ \t]*", body[line_start:]).group(0)
        if inner.strip():
            # Append after the last element, before the closing bracket's
            # leading whitespace.
            close = end - 1
            last = close
            while body[last - 1] in " \t\r\n":
                last -= 1
            return (body[:last] + ",\n" + indent + "  " + entry_text
                    + body[last:])
        return (body[:start] + "[\n" + indent + "  " + entry_text + "\n"
                + indent + "]" + body[end:])
    multiline = "\n" in body[brace:]
    if multiline:
        after = body[brace + 1:]
        indent = re.match(r"\n([ \t]*)", after)
        indent = indent.group(1) if indent else "  "
        sep = "," if spans else ""
        new = ("\n" + indent + '"stage_runs": [\n' + indent + "  "
               + entry_text + "\n" + indent + "]" + sep)
    else:
        sep = ", " if spans else ""
        new = '"stage_runs": [\n  ' + entry_text + "\n]" + sep
    return body[:brace + 1] + new + body[brace + 1:]


# ---- commands --------------------------------------------------------------

def cmd_start(args):
    root = repo_root()
    name = args.epic or args.key
    path = marker_path(common_dir(root), name, args.stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    path.write_text(now + "\n", encoding="utf-8")
    print("started: %s %s %s" % (name, args.stage, now))
    return 0


def read_start(common, names, stage):
    """(started_at or None, the marker paths that exist)."""
    found, started = [], None
    for name in names:
        path = marker_path(common, name, stage)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        found.append(path)
        if started is None and valid_ts(text):
            started = text
    return started, found


def cmd_end(args):
    root = repo_root()
    common = common_dir(root)
    done, left = parse_ids(args.done), parse_ids(args.left)
    both = set(done) & set(left)
    if both:
        raise StampError("ids in both done and left: %s" % ", ".join(sorted(both)))
    pending = parse_ids(args.owner_pending) if args.owner_pending is not None else None
    if pending and not set(pending) <= set(left):
        raise StampError("owner-pending ids not in left: %s"
                         % ", ".join(sorted(set(pending) - set(left))))

    path = find_sequence_file(root, args.epic, args.key, args.sequence_file)
    rel = path.relative_to(root).as_posix()
    ok_line = commit_ok(root, rel)

    names = [args.epic] + ([args.key] if args.key else [])
    started, markers = read_start(common, names, args.stage)
    entry = {"stage": args.stage, "started_at": started,
             "ended_at": utc_now(), "complete": not left,
             "done": done, "left": left,
             "note": "" if started else NOT_RECORDED}
    if pending is not None:
        entry["owner_pending"] = pending
    entry_text = json.dumps(entry, ensure_ascii=False)

    text = path.read_text(encoding="utf-8")
    start, end, old = machine_block(text, args.epic)
    new_body = insert_entry(text[start:end], entry_text)
    new_text = text[:start] + new_body + text[end:]

    # Verify: the new block is the old block plus exactly this entry.
    got = machine_block(new_text, args.epic)
    expected = dict(old)
    runs = list(old.get("stage_runs") or [])
    expected["stage_runs"] = runs + [entry]
    first_ok = "stage_runs" in old or (got and list(got[2])[0] == "stage_runs")
    if got is None or got[2] != expected or not first_ok:
        raise StampError("verification failed; %s left unchanged" % rel)
    if text[:start] != new_text[:start] or text[end:] != new_text[
            len(new_text) - (len(text) - end):]:
        raise StampError("verification failed; %s left unchanged" % rel)

    path.write_text(new_text, encoding="utf-8")
    for marker in markers:
        try:
            marker.unlink()
        except OSError:
            pass
    print("file: %s" % rel)
    print("entry: %s" % entry_text)
    print(ok_line)
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    who = s.add_mutually_exclusive_group(required=True)
    who.add_argument("--epic")
    who.add_argument("--key")
    s.add_argument("--stage", required=True, choices=STAGES)
    e = sub.add_parser("end")
    e.add_argument("--epic", required=True)
    e.add_argument("--key")
    e.add_argument("--stage", required=True, choices=STAGES)
    e.add_argument("--done", required=True)
    e.add_argument("--left", required=True)
    e.add_argument("--owner-pending", dest="owner_pending")
    e.add_argument("--sequence-file", dest="sequence_file")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    for name in (args.epic, getattr(args, "key", None)):
        if name is not None and not NAME_RE.fullmatch(name):
            print("error: bad epic/key %r" % name, file=sys.stderr)
            return 2
    try:
        return cmd_start(args) if args.cmd == "start" else cmd_end(args)
    except StampError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
