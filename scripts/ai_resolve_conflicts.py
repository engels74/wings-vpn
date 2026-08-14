#!/usr/bin/env python3
"""Resolve git merge conflicts using NanoGPT's OpenAI-compatible API.

Run this AFTER a `git merge` that produced conflicts. It will:
  1. find conflicted files            (git diff --name-only --diff-filter=U)
  2. send each one to the model and ask for a fully resolved version
  3. validate the result (no leftover conflict markers) and write it back
  4. `git add` the files it successfully resolved
  5. report a summary to stdout, $GITHUB_STEP_SUMMARY and $GITHUB_OUTPUT

Outputs: `resolved`, `failed`, `failed_files`, and `conflict_details` (a JSON
array of {path, type, status, note} objects consumed by
scripts/ai_generate_pr_metadata.py to build the per-file conflict tables in the
pull request body).

Files it will NOT touch (and instead flags for a human):
  - binary / non-UTF-8 files
  - files larger than MAX_FILE_BYTES (avoids truncated model output)
  - delete/modify or rename conflicts (no inline <<<<<<< markers to resolve)
  - files where the model left conflict markers behind or returned nothing

Only stdlib is used, so the workflow needs no `pip install`.

Environment variables:
  NANOGPT_API_KEY    (required)  your NanoGPT key
  NANOGPT_MODEL      (required)  e.g. "moonshotai/kimi-k2-0905" -- see README
  NANOGPT_BASE_URL   (optional)  default https://nano-gpt.com/api/v1
  MAX_FILE_BYTES     (optional)  default 120000
  MAX_OUTPUT_TOKENS  (optional)  max_tokens cap sent to the API, default 64000
  API_TIMEOUT        (optional)  seconds, default 180
  API_RETRIES        (optional)  default 3
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def env_int(name, default):
    # Parse an int from the environment, falling back to the default on a
    # malformed value. These are evaluated at import time, before main() and
    # before any PR is opened: a bare int(...) here would raise ValueError on
    # e.g. MAX_FILE_BYTES=foo, crash the workflow step, and -- because the
    # downstream merge/push/PR steps are skipped when a step fails -- leave NO
    # pull request for the human. That is the worst outcome for a workflow whose
    # whole point is to always open a PR, so we degrade gracefully (matching the
    # `except Exception  # never crash the merge` ethos) while still emitting a
    # visible warning so genuine misconfiguration stays diagnosable.
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARNING: {name}={raw!r} is not a valid integer; "
            f"falling back to default {default}",
            file=sys.stderr,
        )
        return default


API_BASE = os.environ.get("NANOGPT_BASE_URL", "https://nano-gpt.com/api/v1").rstrip("/")
API_KEY = os.environ.get("NANOGPT_API_KEY", "").strip()
MODEL = os.environ.get("NANOGPT_MODEL", "").strip()
MAX_BYTES = env_int("MAX_FILE_BYTES", 120000)
# High cap so a file near MAX_FILE_BYTES is not silently truncated by a low
# model default. Tune down via env if a model rejects the requested ceiling.
MAX_OUTPUT_TOKENS = env_int("MAX_OUTPUT_TOKENS", 64000)
TIMEOUT = env_int("API_TIMEOUT", 180)
RETRIES = env_int("API_RETRIES", 3)

# Unambiguous markers. We key failure detection on <<<<<<< / >>>>>>> because a
# bare "=======" line can legitimately appear in Markdown/RST and would cause
# false positives.
OPENING = re.compile(r"^<{7}", re.M)
CLOSING = re.compile(r"^>{7}", re.M)
# A line that is EXACTLY the git default conflict separator (seven '=' and
# nothing else). Narrow enough that a Markdown/RST setext underline of arbitrary
# length does not match, yet it still catches a model that deletes the angle
# brackets but leaves the separator behind. Only used on the POST-call path
# (see has_residual_conflict) -- never on the pre-call gate.
SEPARATOR = re.compile(r"^={7}\s*$", re.M)
# A line beginning with a Markdown code fence (three backticks). strip_fences
# only removes a *matched* open+close wrapper; an unclosed/truncated fence or a
# fence preceded by preamble prose survives untouched and would otherwise be
# written into the file as literal content. Only used on the POST-call path
# (see resolve_file) -- never on the pre-call gate, and only on NON-doc files:
# code/config (Go, YAML, ...) conflict files cannot legitimately contain a ```
# line, so any survivor is a genuine wrapper. Doc formats (see DOC_EXTENSIONS)
# routinely contain balanced ``` blocks as real content, so the check is skipped
# for them to avoid rejecting a correct resolution. A false positive on the
# remaining files only routes to human review (a safe direction).
FENCE = re.compile(r"^```", re.M)
# File extensions whose legitimate content can include Markdown code fences. The
# post-call FENCE check is skipped for these so a correctly resolved doc with
# internal ``` blocks (e.g. AGENTS.md, CLAUDE.md) is not falsely rejected.
DOC_EXTENSIONS = (".md", ".markdown", ".rst")

SYSTEM_PROMPT = (
    "You are an expert software engineer resolving a Git merge conflict.\n"
    "You will receive the full contents of ONE file that still contains Git "
    "conflict markers (<<<<<<<, =======, >>>>>>>).\n"
    "The side marked HEAD / 'ours' belongs to a FORK that adds VPN-specific "
    "functionality on top of the original project. The incoming side ('theirs') "
    "comes from the UPSTREAM project.\n"
    "Resolve every conflict by integrating the upstream changes while preserving "
    "the fork's intentional modifications. When both sides changed the same "
    "logic, combine them so neither intent is lost. Keep the code correct and "
    "compilable.\n"
    "Output ONLY the complete, final file content. No explanations, no commentary, "
    "no Markdown code fences. There must be zero remaining conflict markers."
)


# `git status --porcelain` XY codes for unmerged index entries, mapped to the
# human wording used in the PR body's conflict table. The type is what tells a
# reviewer WHAT the disagreement was (content vs. existence), so it must be read
# while the entries are still unmerged -- `git add` clears them to stage 0.
CONFLICT_TYPES = {
    "DD": "both deleted",
    "AU": "added by us",
    "UD": "modify/delete",
    "UA": "added by them",
    "DU": "delete/modify",
    "AA": "both added",
    "UU": "both modified",
}


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def conflicted_files():
    out = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return [line for line in out.splitlines() if line.strip()]


def conflict_types():
    """Map each unmerged path to its conflict type (e.g. "both modified").

    Best-effort: a failure here only costs the PR body its `Type` column, so it
    degrades to an empty map rather than raising into the never-crash main().
    """
    try:
        out = run(["git", "status", "--porcelain", "-z", "--untracked-files=no"])
    except Exception as e:  # noqa: BLE001 - cosmetic metadata must never crash
        print(f"WARNING: could not read conflict types ({e}).", file=sys.stderr)
        return {}

    types = {}
    # -z output is NUL-terminated "XY <path>" records; rename/copy records carry
    # the original path as a SEPARATE following field, which must be skipped so
    # it is not misread as the next record.
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1
        if code in CONFLICT_TYPES:
            types[path] = CONFLICT_TYPES[code]
    return types


def user_prompt(path, content):
    return (
        f"Resolve all merge conflicts in this file.\n\n"
        f"File path: {path}\n\n"
        f"----- BEGIN FILE -----\n{content}\n----- END FILE -----"
    )


def strip_fences(text):
    # The model is told not to emit Markdown fences, but if it disobeys we only
    # strip a fence we can prove is a *wrapper*: an opening ``` line AND a
    # matching bare ``` as the last non-empty line. An opening fence with no
    # closing fence is the malformed/truncated case -- we leave the text intact
    # so the downstream empty / has_markers checks (or a human) catch it rather
    # than silently dropping the first line and accepting a likely-corrupt body.
    # Residual edge: a file whose real content both starts and ends with a bare
    # ``` (e.g. a Markdown doc that is nothing but one fenced block) would be
    # unwrapped; that is rare and acceptable versus the silent-corruption risk.
    # Detect on a stripped copy so incidental whitespace the model may add
    # around the wrapper (e.g. a blank line before the opening fence) doesn't
    # defeat detection. But only the *fence delimiters* are ours to remove:
    # when there is no wrapping fence we return the ORIGINAL text untouched, so
    # a file's own legitimate leading/trailing whitespace is never mutated.
    lines = text.strip().split("\n")
    if len(lines) >= 2 and lines[0].startswith("```"):
        last = len(lines) - 1
        while last > 0 and not lines[last].strip():
            last -= 1
        if last > 0 and lines[last].strip() == "```":
            return "\n".join(lines[1:last])
    return text


def has_markers(text):
    return bool(OPENING.search(text) or CLOSING.search(text))


def has_residual_conflict(text):
    # Post-call validation only. Reuses has_markers (the angle brackets) and
    # additionally rejects a lone surviving separator line. Matching "=======*"
    # here is safe because a false positive only routes the file to human
    # review (a safe direction), unlike the pre-call gate where it would waste
    # a model call on a non-conflicted doc.
    return has_markers(text) or bool(SEPARATOR.search(text))


def call_model(path, content):
    body = json.dumps({
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt(path, content)},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # `json.loads` can yield any JSON type; a non-object body (scalar,
            # array, or null from a proxy/CDN) would make `data.get` below raise
            # AttributeError, which is NOT in the caught tuple and would escape
            # the retry loop. Raise ValueError instead to keep it on the retry
            # path, consistent with the `choices` guard.
            if not isinstance(data, dict):
                raise ValueError("non-object API response")
            # A present-but-empty `choices` (or a non-list) would make the index
            # below raise IndexError/TypeError, which is NOT in the caught tuple
            # and would escape the retry loop -- a transient empty response would
            # then permanently route the file to a human instead of being
            # retried. Raising ValueError keeps it on the existing retry path
            # (an ABSENT `choices` key already retries via KeyError) and yields a
            # clearer message than "list index out of range".
            if not isinstance(data.get("choices"), list) or not data["choices"]:
                raise ValueError("empty or malformed 'choices' in API response")
            # `choices[0]` and its `message` are still untyped here: a non-dict
            # choice (str/None/number) makes `["message"]` raise TypeError, and a
            # non-dict `message` makes `["content"]` raise TypeError -- neither is
            # in the caught tuple, so it would escape the retry loop and turn a
            # transient malformed response into a one-shot per-file failure. Guard
            # the shape and raise ValueError to keep it on the retry path,
            # consistent with the dict/choices guards above. A missing "content"
            # key still raises KeyError, which is already caught/retried.
            choice = data["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("unexpected choices[0] shape in API response")
            return choice["message"]["content"]
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
            last_err = e
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8")[:500]
                except Exception:
                    pass
            print(f"  attempt {attempt}/{RETRIES} failed: {e} {detail}", file=sys.stderr)
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"model call failed for {path}: {last_err}")


def resolve_file(path):
    """Return (status, note). status in {'resolved','skipped','failed'}."""
    try:
        with open(path, "rb") as f:
            # Bounded read: cap memory at MAX_BYTES+1 so an oversized file is
            # detected (len(raw) > MAX_BYTES) WITHOUT allocating the whole thing.
            # Reading the full file before the size gate would defeat the gate's
            # OOM-avoidance purpose on a multi-GB blob. The +1 byte is enough to
            # prove the file exceeds the limit; we no longer know the exact size,
            # so the skip note reports the threshold rather than a byte count.
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "skipped", "file missing (delete/rename conflict)"

    if len(raw) > MAX_BYTES:
        return "skipped", f"too large (> {MAX_BYTES} bytes)"

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "skipped", "binary / non-UTF-8 file"

    if not has_markers(content):
        return "skipped", "no inline conflict markers (delete/modify conflict?)"

    resolved = strip_fences(call_model(path, content))

    if not resolved.strip():
        return "failed", "model returned empty output"
    if has_residual_conflict(resolved):
        return "failed", "model left conflict markers behind"
    # strip_fences removes a matched open+close wrapper; a fence that survives
    # here is an unclosed/truncated wrapper or one preceded by preamble prose,
    # which would be written as literal file content. Distinct message: the
    # failure mode differs from leftover conflict markers (aids diagnosis in the
    # workflow's failed-files surface). Skip for doc formats (.md/.markdown/.rst):
    # there a ``` is almost always real content, so the check would reject a
    # correct resolution; for code/config files a survivor is a genuine wrapper.
    if not path.lower().endswith(DOC_EXTENSIONS) and FENCE.search(resolved):
        return "failed", "model wrapped output in Markdown code fences"

    if not resolved.endswith("\n"):
        resolved += "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(resolved)
    run(["git", "add", "--", path])
    return "resolved", "resolved by model"


def gh_out(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    # Use the multiline heredoc form (the current GitHub standard) rather than
    # the deprecated single-line "name=value": it is the future-proof way to
    # write the free-form failed_files list. The delimiter must not appear in
    # the value, so we follow GitHub's documented recommendation and use a
    # random per-call token; collision with our integer/path values is then
    # impossible. We regenerate (rather than raise) on the astronomically
    # improbable collision, so this function can never crash the workflow step
    # and suppress the PR -- matching the file's degrade-gracefully design.
    delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    while delimiter in value:
        delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def gh_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    # A missing key/model is operator misconfig, but exiting non-zero here would
    # fail the step and -- because the downstream merge/push/PR steps are skipped
    # when a step fails -- leave NO pull request, defeating this workflow's whole
    # point. The conflicts need a human regardless, so we degrade gracefully
    # (matching env_int / the `except Exception  # never crash the merge` ethos):
    # skip the model calls, route every conflicted file to a human with a clear
    # reason, emit the SAME gh_out/gh_summary outputs the normal path would, and
    # exit 0 so the workflow still opens a needs-manual-merge PR. A visible
    # WARNING keeps genuine misconfiguration diagnosable.
    unavailable = None
    if not API_KEY:
        unavailable = "AI resolver unavailable: NANOGPT_API_KEY not set"
    elif not MODEL:
        unavailable = "AI resolver unavailable: NANOGPT_MODEL not set"
    if unavailable:
        print(f"WARNING: {unavailable}; routing all conflicts to a human.", file=sys.stderr)

    # Enumerating conflicts shells out to git via run() (check=True), so a
    # CalledProcessError, or a missing git binary (FileNotFoundError/OSError),
    # would raise and exit non-zero -- failing the step and, because the
    # downstream merge/push/PR steps are skipped on failure, leaving NO pull
    # request. Unlikely on a healthy runner (git is present; we only reach here
    # after a merge left conflicts), but a real gap vs the never-crash invariant.
    # Degrade like the missing-key path: WARN, emit outputs, and exit 0 so the
    # workflow's `git add -A` commit still opens a PR with the raw conflicts
    # visible in the diff. A broad `except Exception` is deliberate here -- this
    # is the top-level guard for the whole enumeration and must never let any
    # failure suppress the PR.
    #
    # We emit a NON-zero `failed` (not the zero the `if not files:` branch emits)
    # plus a clear sentinel `failed_files`. This is load-bearing: the workflow
    # gates BOTH the `needs-manual-merge` label and the "⚠️ Files still needing
    # manual attention" PR-body section on `if [ "${FAILED:-0}" != "0" ]`
    # (.github/workflows/sync-upstream.yml). Emitting failed=0 here would open a
    # PR that reads as if nothing needs attention even though we explicitly could
    # not enumerate the conflicts and routed them to a human -- so we MUST report
    # a non-zero count to fire the label + warning. Keep resolved=0 and return
    # (exit 0): the never-crash + always-open-PR invariants are preserved. The
    # sentinel is not a path; the workflow interpolates $FAILED_FILES as plain
    # text inside backticks, so a prose sentinel renders fine, and gh_out's
    # random heredoc delimiter handles any characters in it.
    try:
        files = conflicted_files()
    except Exception as e:  # noqa: BLE001 - never crash; a PR must still open
        print(
            f"WARNING: could not enumerate conflicted files ({e}); "
            f"opening a PR with the raw conflict state for a human.",
            file=sys.stderr,
        )
        sentinel = "(could not enumerate conflicts — manual review required)"
        gh_out("resolved", "0")
        gh_out("failed", "1")
        gh_out("failed_files", sentinel)
        gh_out("conflict_details", json.dumps([{
            "path": sentinel,
            "type": "unknown",
            "status": "manual",
            "note": f"could not enumerate conflicted files: {e}",
        }]))
        return

    if not files:
        print("No conflicted files found.")
        gh_out("resolved", "0")
        gh_out("failed", "0")
        gh_out("failed_files", "")
        gh_out("conflict_details", "[]")
        return

    print(f"Conflicted files ({len(files)}): {', '.join(files)}")

    # Read the types BEFORE resolving: resolve_file() stages what it fixes, which
    # clears the unmerged entries this depends on.
    types = conflict_types()

    resolved, need_human, details = [], [], []
    for path in files:
        print(f"-> {path}")
        if unavailable:
            status, note = "failed", unavailable
        else:
            try:
                status, note = resolve_file(path)
            except Exception as e:  # noqa: BLE001 - never crash the merge
                status, note = "failed", str(e)
        print(f"   [{status}] {note}")
        if status == "resolved":
            resolved.append(path)
        else:
            need_human.append((path, note))
        details.append({
            "path": path,
            "type": types.get(path, "unknown"),
            "status": "resolved" if status == "resolved" else "manual",
            "note": note,
        })

    gh_out("resolved", str(len(resolved)))
    gh_out("failed", str(len(need_human)))
    gh_out("failed_files", ",".join(p for p, _ in need_human))
    gh_out("conflict_details", json.dumps(details))

    summary = ["### AI conflict resolution", ""]
    summary.append(f"- Resolved by model: **{len(resolved)}**")
    summary.append(f"- Need a human: **{len(need_human)}**")
    if resolved:
        summary += ["", "**Resolved:**"] + [
            f"- `{p}` ({types.get(p, 'unknown')})" for p in resolved
        ]
    if need_human:
        summary += ["", "**Left for you to finish:**"] + [
            f"- `{p}` ({types.get(p, 'unknown')}) — {note}" for p, note in need_human
        ]
    gh_summary(summary)
    print("\n".join(summary))


if __name__ == "__main__":
    main()
