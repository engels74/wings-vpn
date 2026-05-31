#!/usr/bin/env python3
"""Resolve git merge conflicts using NanoGPT's OpenAI-compatible API.

Run this AFTER a `git merge` that produced conflicts. It will:
  1. find conflicted files            (git diff --name-only --diff-filter=U)
  2. send each one to the model and ask for a fully resolved version
  3. validate the result (no leftover conflict markers) and write it back
  4. `git add` the files it successfully resolved
  5. report a summary to stdout, $GITHUB_STEP_SUMMARY and $GITHUB_OUTPUT

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
# (see has_residual_conflict) -- never on the pre-call gate. Go/YAML conflict
# files cannot legitimately contain a ``` line, and a false positive only routes
# the file to human review (a safe direction).
FENCE = re.compile(r"^```", re.M)

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


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def conflicted_files():
    out = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return [line for line in out.splitlines() if line.strip()]


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
            return data["choices"][0]["message"]["content"]
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
            raw = f.read()
    except FileNotFoundError:
        return "skipped", "file missing (delete/rename conflict)"

    if len(raw) > MAX_BYTES:
        return "skipped", f"too large ({len(raw)} bytes > {MAX_BYTES})"

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
    # workflow's failed-files surface).
    if FENCE.search(resolved):
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
    if not API_KEY:
        sys.exit("NANOGPT_API_KEY is not set")
    if not MODEL:
        sys.exit("NANOGPT_MODEL is not set (e.g. moonshotai/kimi-k2-0905)")

    files = conflicted_files()
    if not files:
        print("No conflicted files found.")
        gh_out("resolved", "0")
        gh_out("failed", "0")
        gh_out("failed_files", "")
        return

    print(f"Conflicted files ({len(files)}): {', '.join(files)}")

    resolved, need_human = [], []
    for path in files:
        print(f"-> {path}")
        try:
            status, note = resolve_file(path)
        except Exception as e:  # noqa: BLE001 - never crash the merge
            status, note = "failed", str(e)
        print(f"   [{status}] {note}")
        if status == "resolved":
            resolved.append(path)
        else:
            need_human.append((path, note))

    gh_out("resolved", str(len(resolved)))
    gh_out("failed", str(len(need_human)))
    gh_out("failed_files", ",".join(p for p, _ in need_human))

    summary = ["### AI conflict resolution", ""]
    summary.append(f"- Resolved by model: **{len(resolved)}**")
    summary.append(f"- Need a human: **{len(need_human)}**")
    if resolved:
        summary += ["", "**Resolved:**"] + [f"- `{p}`" for p in resolved]
    if need_human:
        summary += ["", "**Left for you to finish:**"] + [
            f"- `{p}` — {note}" for p, note in need_human
        ]
    gh_summary(summary)
    print("\n".join(summary))


if __name__ == "__main__":
    main()
