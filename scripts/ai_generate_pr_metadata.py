#!/usr/bin/env python3
"""Generate GitHub PR metadata for the upstream sync workflow.

The body is rendered from ONE fixed template so consecutive syncs look identical
in shape. The template is filled from git (commit range, diffstat) and from the
resolver's `conflict_details` output; NanoGPT only supplies prose (the summary
line and the per-conflict wording). When the model call fails, the exact same
skeleton is emitted with the model-provided fields marked `n/a`, so the
structure never depends on the model call succeeding.

Emits two GitHub Actions outputs: `title` and `body`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from functools import lru_cache


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"WARNING: {name}={raw!r} is not an integer; using {default}", file=sys.stderr)
        return default


API_BASE = os.environ.get("NANOGPT_BASE_URL", "https://nano-gpt.com/api/v1").rstrip("/")
API_KEY = os.environ.get("NANOGPT_API_KEY", "").strip()
MODEL = os.environ.get("NANOGPT_MODEL", "").strip()
TIMEOUT = env_int("PR_METADATA_API_TIMEOUT", 45)
# Total attempt count (not retries-after-first); the warning logs read
# "attempt {n}/{RETRIES}". Floor of 1 so RETRIES=0 can't silently disable
# NanoGPT and fall straight through to the deterministic fallback.
RETRIES = max(1, env_int("PR_METADATA_API_RETRIES", 2))
MAX_OUTPUT_TOKENS = env_int("PR_METADATA_MAX_OUTPUT_TOKENS", 2000)

UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main").strip() or "main"
TARGET_BRANCH = os.environ.get("TARGET_BRANCH", "main").strip() or "main"
BEHIND = os.environ.get("BEHIND", "0").strip() or "0"
CONFLICTS = os.environ.get("CONFLICTS", "false").strip().lower() == "true"
RESOLVED = os.environ.get("RESOLVED", "0").strip() or "0"
FAILED = os.environ.get("FAILED", "0").strip() or "0"
FAILED_FILES = os.environ.get("FAILED_FILES", "").strip()
CONFLICT_DETAILS = os.environ.get("CONFLICT_DETAILS", "").strip()
UPSTREAM_SHA = os.environ.get("UPSTREAM_SHA", "").strip()

UPSTREAM_REF = f"upstream/{UPSTREAM_BRANCH}"

# GitHub rejects a pull request body over 65536 characters. Cap below that so the
# body always posts; the old 1800-char cap predates this template and would slice
# the middle out of it. Only the collapsed <details> sections can approach this.
MAX_BODY_CHARS = 60000
MAX_TITLE_CHARS = 100

# How much per-conflict diff context the model gets. Enough to describe the
# disagreement, small enough that a dozen conflicts still fit one request.
CONFLICT_DIFF_MAX_CHARS = 2000
CONFLICT_DIFF_MAX_LINES = 60
# Conflicts beyond this are still listed in the table (from git data); they just
# do not get model prose. Keeps the request bounded on a pathological merge.
MAX_PROMPTED_CONFLICTS = 12

# Covers the standard emoji blocks and symbol ranges where GitHub-style emoji
# glyphs usually appear. Keep normal ASCII Markdown punctuation intact.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"  # variation selectors VS1-VS16 (e.g. U+FE0F glue)
    "\U0000200D"  # zero-width joiner that binds composed (ZWJ) emoji
    "]+",
    flags=re.UNICODE,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f\x7f]")
ISSUE_CLOSER_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d+)",
    flags=re.IGNORECASE,
)
ISSUE_CLOSER_URL_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)",
    flags=re.IGNORECASE,
)

NA = "n/a"

SYSTEM_PROMPT = """You write factual prose for an automated upstream-sync pull request.
The pull request body is rendered from a fixed template; you only supply text for
a few slots. Return ONLY a strict JSON object with these keys:

  "title"    string, at most 80 characters, sentence case, describes the sync.
  "summary"  string, one or two plain sentences on what the upstream range
             actually contains. No headings, no lists, no line breaks.
  "conflicts" array (may be empty) of objects, one per conflicted file you were
             given, each with:
               "path"       string, copied verbatim from the input.
               "resolution" string, at most 100 characters, one line, what the
                            resolution did (this lands in a table cell).
               "upstream"   string, one sentence: what upstream changed there.
               "fork"       string, one sentence: what the fork had there.

Rules: no emojis, no Markdown code fences around the JSON, no marketing
language, no speculation beyond the diffs you are given, and never invent a file
path that was not in the input.
""".strip()


def git_output(args: list[str], default: str = "", *, strip: bool = True) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        # `--stat` output is column-aligned with a leading space per line, so the
        # callers that render it verbatim opt out of the left strip.
        return out.strip() if strip else out.rstrip("\n")
    except Exception as exc:  # noqa: BLE001 - metadata generation must never block PRs
        print(f"WARNING: git {' '.join(args)} failed: {exc}", file=sys.stderr)
        return default


def truncate(value: str, *, max_chars: int = 4000, max_lines: int = 80) -> str:
    lines = value.splitlines()[:max_lines]
    truncated = "\n".join(lines)
    if len(value.splitlines()) > max_lines:
        truncated += f"\n... truncated to {max_lines} lines"
    if len(truncated) > max_chars:
        truncated = truncated[:max_chars].rstrip() + "\n... truncated"
    return truncated


def plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


# --------------------------------------------------------------------------
# Sanitizing. Applied to every non-literal string that reaches the body --
# model prose AND upstream-authored text such as commit subjects.
# --------------------------------------------------------------------------


def strip_emoji_and_controls(value: str) -> str:
    return CONTROL_RE.sub("", EMOJI_RE.sub("", value)).strip()


def neutralize_issue_closers(value: str) -> str:
    # Keep the repo identity (owner/repo) when present, but drop the literal
    # "#"/issue-URL and interpose "issue" so no live auto-close reference
    # ("<keyword> #N", "<keyword> owner/repo#N", "<keyword> <issue-url>")
    # survives adjacent to the keyword. Double-safe by construction.
    def repl(match: re.Match[str]) -> str:
        repo = f"{match.group(2)} " if match.group(2) else ""
        return f"{match.group(1)} {repo}issue {match.group(3)}"

    value = ISSUE_CLOSER_RE.sub(repl, value)
    return ISSUE_CLOSER_URL_RE.sub(repl, value)


def sanitize_text(value: str) -> str:
    """Strip emoji/control characters and defuse issue-closing keywords.

    Unlike `strip_emoji_and_controls` this does NOT trim the ends, so multi-line
    blocks (a diffstat) keep their leading alignment.
    """
    return neutralize_issue_closers(CONTROL_RE.sub("", EMOJI_RE.sub("", value)))


# --------------------------------------------------------------------------
# Facts gathered from git and from the resolver. Everything here is
# deterministic: the template renders identically with or without the model.
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def upstream_sha() -> str:
    if UPSTREAM_SHA:
        return UPSTREAM_SHA
    return git_output(["rev-parse", "--short=7", UPSTREAM_REF]) or "unknown"


@lru_cache(maxsize=1)
def merge_base() -> str:
    return git_output(["merge-base", TARGET_BRANCH, UPSTREAM_REF])


@lru_cache(maxsize=1)
def merged_commits() -> tuple[tuple[str, str], ...]:
    """(short sha, subject) for each upstream commit this merge brings in.

    Preferred range is `<target>..upstream/<branch>` -- exactly the commits the
    merge pulled in, with no merge commit of our own in the way. If the upstream
    ref is not resolvable (e.g. the script is run outside the workflow) fall back
    to the branch's own non-merge commits.
    """
    raw = git_output(["log", "--no-decorate", "--pretty=format:%h%x00%s", f"{TARGET_BRANCH}..{UPSTREAM_REF}"])
    if not raw:
        raw = git_output(
            ["log", "--no-decorate", "--no-merges", "--pretty=format:%h%x00%s", f"{TARGET_BRANCH}..HEAD"]
        )
    commits = []
    for line in raw.splitlines():
        sha, _, subject = line.partition("\0")
        if sha.strip():
            # Upstream subjects land verbatim in the body, so they get the same
            # sanitizing as model text: an upstream "fix #12" would otherwise be
            # a live auto-close reference against THIS fork's issue tracker.
            commits.append((sha.strip(), sanitize_text(subject).strip()))
    return tuple(commits)


@lru_cache(maxsize=1)
def diffstat() -> tuple[int, int, int]:
    """(files changed, insertions, deletions) for the PR diff."""
    raw = git_output(["diff", "--numstat", f"{TARGET_BRANCH}...HEAD"])
    files = insertions = deletions = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # Binary files report "-" for both counts; count the file, skip the math.
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return files, insertions, deletions


@lru_cache(maxsize=1)
def changed_files_stat() -> str:
    return sanitize_text(truncate(
        git_output(["diff", "--stat=100,60", f"{TARGET_BRANCH}...HEAD"], strip=False),
        max_chars=12000,
        max_lines=200,
    ))


@lru_cache(maxsize=1)
def conflict_entries() -> tuple[dict[str, str], ...]:
    """Per-file conflict facts: path, type, status (resolved|manual), note.

    Sourced from the resolver's `conflict_details` output. Falls back to the flat
    `failed_files` list so a resolver that could not emit the richer payload still
    produces a complete table (types unknown, every listed path unresolved).
    """
    entries: list[dict[str, str]] = []
    if CONFLICT_DETAILS:
        try:
            parsed = json.loads(CONFLICT_DETAILS)
        except json.JSONDecodeError as exc:
            print(f"WARNING: CONFLICT_DETAILS was not valid JSON ({exc}); using failed_files.", file=sys.stderr)
            parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                entries.append({
                    "path": path,
                    "type": str(item.get("type", "unknown")).strip() or "unknown",
                    "status": "resolved" if item.get("status") == "resolved" else "manual",
                    "note": str(item.get("note", "")).strip(),
                })
    if entries:
        return tuple(entries)

    for path in [p.strip() for p in re.split(r"[,\n]", FAILED_FILES) if p.strip()]:
        entries.append({"path": path, "type": "unknown", "status": "manual", "note": ""})
    return tuple(entries)


@lru_cache(maxsize=MAX_PROMPTED_CONFLICTS)
def conflict_diffs(path: str) -> dict[str, str]:
    """The two sides of one conflict, as diffs against the merge base."""
    base = merge_base()
    if not base:
        return {}
    args = ["diff", "--unified=3", base]
    return {
        "upstream_change": truncate(
            git_output([*args, UPSTREAM_REF, "--", path]),
            max_chars=CONFLICT_DIFF_MAX_CHARS,
            max_lines=CONFLICT_DIFF_MAX_LINES,
        ),
        "fork_change": truncate(
            git_output([*args, TARGET_BRANCH, "--", path]),
            max_chars=CONFLICT_DIFF_MAX_CHARS,
            max_lines=CONFLICT_DIFF_MAX_LINES,
        ),
    }


# --------------------------------------------------------------------------
# Rendering. `render_body` is the ONLY place a body is produced; the fallback
# path calls it with an empty model payload.
# --------------------------------------------------------------------------


def cell(value: str) -> str:
    """Make a string safe for a Markdown table cell."""
    value = " ".join(value.split())
    return value.replace("|", "\\|")


@dataclass(frozen=True)
class Facts:
    """Everything the template needs, gathered once. Defaults render as an
    empty-but-complete body, which is what the last-resort path uses."""

    upstream_sha: str = "unknown"
    commits: tuple[tuple[str, str], ...] = ()
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    entries: tuple[dict[str, str], ...] = ()
    stat: str = ""


def collect_facts() -> Facts:
    """Gather the deterministic half of the body. Never raises: every git call
    already degrades to a default, and a PR must open regardless."""
    try:
        files, insertions, deletions = diffstat()
        return Facts(
            upstream_sha=upstream_sha(),
            commits=merged_commits(),
            files=files,
            insertions=insertions,
            deletions=deletions,
            entries=conflict_entries(),
            stat=changed_files_stat(),
        )
    except Exception as exc:  # noqa: BLE001 - render placeholders rather than fail
        print(f"WARNING: could not gather sync facts ({exc}); rendering placeholders.", file=sys.stderr)
        return Facts()


def render_body(prose: dict[str, object] | None, facts: Facts) -> str:
    prose = prose or {}
    summary = str(prose.get("summary", "")).strip()
    per_file = prose.get("conflicts")
    per_file = per_file if isinstance(per_file, dict) else {}

    entries = facts.entries
    commits = facts.commits
    files, insertions, deletions = facts.files, facts.insertions, facts.deletions

    if entries:
        resolved_count = sum(1 for e in entries if e["status"] == "resolved")
        manual_count = sum(1 for e in entries if e["status"] == "manual")
    else:
        # No per-file payload (resolver skipped or its output was lost). The
        # scalar counters still describe the merge, so the header stays truthful
        # even though the table below cannot be built.
        resolved_count = int(RESOLVED) if RESOLVED.isdigit() else 0
        manual_count = int(FAILED) if FAILED.isdigit() else 0
    total_conflicts = len(entries) or resolved_count + manual_count
    needs_manual = manual_count > 0

    commit_count = len(commits) or (int(BEHIND) if BEHIND.isdigit() else 0)
    conflict_cell = (
        f"{total_conflicts} ({resolved_count} auto-resolved, {manual_count} manual)"
        if total_conflicts
        else "0"
    )

    out: list[str] = []
    out.append(f"## Upstream sync: `{UPSTREAM_REF}@{facts.upstream_sha}`")
    out.append("")
    out.append("| Field | Value |")
    out.append("| --- | --- |")
    out.append(f"| Commits merged | {commit_count} |")
    out.append(f"| Files changed | {files} (+{insertions} / -{deletions}) |")
    out.append(f"| Conflicts | {conflict_cell} |")
    out.append(f"| Status | {'Needs manual merge' if needs_manual else 'Ready for review'} |")
    out.append("")
    out.append("### Summary")
    out.append("")
    out.append(summary or NA)
    out.append("")
    out.append("### Conflict resolution")
    out.append("")

    if not total_conflicts:
        out.append(f"No conflicts; `{UPSTREAM_REF}` merged into `{TARGET_BRANCH}` cleanly.")
        out.append("")
    elif not entries:
        out.append(
            f"{total_conflicts} {plural(total_conflicts, 'file')} conflicted, but the per-file "
            f"resolver report was not available to this step. See the {job_summary_link()}."
        )
        out.append("")
    else:
        out.append("| File | Type | Resolution |")
        out.append("| --- | --- | --- |")
        for entry in entries:
            if entry["status"] == "manual":
                resolution = "unresolved"
            else:
                detail = per_file.get(entry["path"])
                resolution = cell(str(detail.get("resolution", ""))) if isinstance(detail, dict) else ""
                resolution = resolution or NA
            out.append(f"| `{cell(entry['path'])}` | {cell(entry['type'])} | {resolution} |")
        out.append("")
        if needs_manual:
            verb = "needs" if manual_count == 1 else "need"
            out.append(
                f"{manual_count} {plural(manual_count, 'file')} {verb} manual resolution. "
                f"Per-file resolver notes are in the {job_summary_link()}."
            )
            out.append("")

        out.append("<details>")
        out.append("<summary>Per-file detail</summary>")
        out.append("")
        for entry in entries:
            detail = per_file.get(entry["path"], {})
            detail = detail if isinstance(detail, dict) else {}
            out.append(f"**`{entry['path']}`** \u2014 {entry['type']}")
            out.append(f"- Upstream: {str(detail.get('upstream', '')).strip() or NA}")
            out.append(f"- Fork: {str(detail.get('fork', '')).strip() or NA}")
            if entry["status"] == "manual":
                reason = entry["note"] or NA
                out.append(f"- Resolution: unresolved \u2014 {reason}")
            else:
                out.append(
                    f"- Resolution: {str(detail.get('resolution', '')).strip() or NA}"
                )
            out.append("")
        out.append("</details>")
        out.append("")

    out.append("<details>")
    out.append(f"<summary>Merged commits ({commit_count})</summary>")
    out.append("")
    if commits:
        for sha, subject in commits:
            out.append(f"- `{sha}` {subject}" if subject else f"- `{sha}`")
    else:
        out.append(f"- {NA}")
    out.append("")
    out.append("</details>")
    out.append("")

    stat = facts.stat
    out.append("<details>")
    out.append(f"<summary>Changed files ({files})</summary>")
    out.append("")
    # Four backticks: a path in the diffstat cannot close the fence early.
    out.append("````")
    out.append(stat if stat else NA)
    out.append("````")
    out.append("")
    out.append("</details>")
    out.append("")

    out.append("### Review checklist")
    out.append("")
    out.append("- [ ] Conflict resolutions preserve fork-specific behaviour")
    out.append("- [ ] `make build` and `make test` pass")

    return "\n".join(out).strip()


def job_summary_link() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if server and repo and run_id:
        return f"[job summary]({server}/{repo}/actions/runs/{run_id})"
    return "job summary of the sync workflow run"


def fallback_title() -> str:
    behind = int(BEHIND) if BEHIND.isdigit() else 0
    commit_word = plural(behind, "commit")
    if CONFLICTS and FAILED not in {"", "0"}:
        return f"Sync {UPSTREAM_REF}: {behind} {commit_word}, manual merge required"
    if CONFLICTS:
        return f"Sync {UPSTREAM_REF}: {behind} {commit_word} with resolved conflicts"
    return f"Sync {UPSTREAM_REF}: {behind} {commit_word}"


# --------------------------------------------------------------------------
# Model call. It only ever supplies prose for slots in the template above.
# --------------------------------------------------------------------------


def build_prompt() -> str:
    entries = conflict_entries()
    conflicts = []
    for entry in entries[:MAX_PROMPTED_CONFLICTS]:
        item = {
            "path": entry["path"],
            "type": entry["type"],
            "resolver_status": entry["status"],
            "resolver_note": entry["note"],
        }
        item.update(conflict_diffs(entry["path"]))
        conflicts.append(item)

    files, insertions, deletions = diffstat()
    context = {
        "upstream_branch": UPSTREAM_BRANCH,
        "target_branch": TARGET_BRANCH,
        "upstream_sha": upstream_sha(),
        "commits_behind": BEHIND,
        "files_changed": files,
        "insertions": insertions,
        "deletions": deletions,
        "conflicted_files": len(entries),
        "ai_resolved_files": RESOLVED,
        "manual_files_count": FAILED,
        "commits": truncate(
            "\n".join(f"{sha} {subject}" for sha, subject in merged_commits()),
            max_chars=4000,
            max_lines=40,
        ),
        "diff_stat": truncate(
            git_output(["diff", "--stat", f"{TARGET_BRANCH}...HEAD"]),
            max_chars=3000,
            max_lines=60,
        ),
        "conflicts": conflicts,
    }
    return (
        "Write the prose slots for an automated upstream-sync pull request. "
        "Describe only what the data below shows.\n\n"
        f"Context JSON:\n{json.dumps(context, indent=2)}"
    )


def response_content(data: object) -> str:
    if not isinstance(data, dict):
        raise ValueError("API response was not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("API response did not contain choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("API choices[0] was not an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("API choices[0].message was not an object")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("API message content was not a string")
    return content


def call_model() -> dict[str, object] | None:
    if not API_KEY or not MODEL:
        print("WARNING: NanoGPT PR metadata unavailable; using fallback text.", file=sys.stderr)
        return None

    request_body = json.dumps({
        "model": MODEL,
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt()},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=request_body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return parse_model_json(response_content(data))
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            detail = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = exc.read().decode("utf-8")[:500]
                except Exception:  # noqa: BLE001 - best-effort diagnostics only
                    pass
            print(f"WARNING: PR metadata attempt {attempt}/{RETRIES} failed: {exc} {detail}", file=sys.stderr)
            if attempt < RETRIES:
                time.sleep(2 * attempt)

    print(f"WARNING: using fallback PR metadata after NanoGPT failure: {last_err}", file=sys.stderr)
    return None


def extract_json_object(text: str) -> str:
    # Prefer a strict parse of the whole string; only fall back to extraction
    # when the model wraps the object in explanatory prose. Scanning for a
    # brace-balanced slice (tracking string/escape state) avoids being fooled
    # by stray prose braces after the object or braces inside string values.
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("model did not return a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("model did not return a JSON object")


def parse_model_json(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        raise ValueError("model returned a markdown code fence")
    data = json.loads(extract_json_object(text))
    if not isinstance(data, dict):
        raise ValueError("model JSON was not an object")
    title = data.get("title")
    summary = data.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        raise ValueError("model JSON must contain string title and summary")
    if not title.strip() or not summary.strip():
        raise ValueError("model returned empty title or summary")

    # Keyed by path and intersected with the paths git actually reported, so a
    # hallucinated file can never reach the body.
    known = {entry["path"] for entry in conflict_entries()}
    conflicts: dict[str, dict[str, str]] = {}
    raw_conflicts = data.get("conflicts")
    if isinstance(raw_conflicts, list):
        for item in raw_conflicts:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if path not in known:
                continue
            conflicts[path] = {
                key: str(item.get(key, "")).strip()
                for key in ("resolution", "upstream", "fork")
            }
    return {"title": title, "summary": summary, "conflicts": conflicts}


def sanitize_prose(prose: dict[str, object] | None) -> dict[str, object] | None:
    """Sanitize model text BEFORE it is rendered.

    Sanitizing the model's words rather than the finished body is what keeps the
    template intact: stripping control characters from the rendered Markdown
    would be indistinguishable from stripping them from a table row.
    """
    if not prose:
        return None

    def clean(value: object) -> str:
        # Every model slot is a single-line field (title, summary, table cell,
        # bullet). Collapsing whitespace here is what stops a multi-line answer
        # from splitting a table row or a list item across lines.
        collapsed = " ".join(str(value).split())
        return neutralize_issue_closers(strip_emoji_and_controls(collapsed))

    conflicts = prose.get("conflicts")
    conflicts = conflicts if isinstance(conflicts, dict) else {}
    return {
        "title": clean(prose.get("title", "")),
        "summary": clean(prose.get("summary", "")),
        "conflicts": {
            path: {key: clean(value) for key, value in fields.items()}
            for path, fields in conflicts.items()
        },
    }


def finalize(title: str, body: str) -> tuple[str, str]:
    title = " ".join(strip_emoji_and_controls(title).split())
    title = neutralize_issue_closers(title) or fallback_title()
    # Surface the blocking state in the title itself: the PR list is the only
    # place a reviewer sees before opening anything, and a model-written title
    # has no obligation to mention it.
    if FAILED not in {"", "0"} and "manual" not in title.lower():
        title = f"{title} (manual merge required)"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 3].rstrip() + "..."

    # The body is template-rendered and already sanitized slot by slot; this cap
    # only guards against a pathological commit list blowing GitHub's hard limit.
    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 3].rstrip() + "..."
    return title, body


def gh_out(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"{name}={value}")
        return
    delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    while delimiter in value:
        delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def main() -> None:
    try:
        prose = sanitize_prose(call_model())
    except Exception as exc:  # noqa: BLE001 - PR metadata must never suppress PR creation
        print(f"WARNING: unexpected PR metadata failure; using fallback text: {exc}", file=sys.stderr)
        prose = None

    facts = collect_facts()
    try:
        body = render_body(prose, facts)
    except Exception as exc:  # noqa: BLE001 - a PR must open even if rendering trips
        print(f"WARNING: PR body rendering failed; using the empty skeleton: {exc}", file=sys.stderr)
        prose = None
        body = render_body(None, Facts())

    title = str(prose.get("title", "")) if prose else ""
    title, body = finalize(title or fallback_title(), body)
    gh_out("title", title)
    gh_out("body", body)


if __name__ == "__main__":
    main()
