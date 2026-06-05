#!/usr/bin/env python3
"""Generate concise GitHub PR metadata for the upstream sync workflow.

The script uses NanoGPT's OpenAI-compatible chat completions API when available,
then falls back to deterministic text without failing the workflow. It emits two
GitHub Actions outputs: `title` and `body`.
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
RETRIES = env_int("PR_METADATA_API_RETRIES", 2)
MAX_OUTPUT_TOKENS = env_int("PR_METADATA_MAX_OUTPUT_TOKENS", 1200)

UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main").strip() or "main"
TARGET_BRANCH = os.environ.get("TARGET_BRANCH", "main").strip() or "main"
BEHIND = os.environ.get("BEHIND", "0").strip() or "0"
CONFLICTS = os.environ.get("CONFLICTS", "false").strip().lower() == "true"
RESOLVED = os.environ.get("RESOLVED", "0").strip() or "0"
FAILED = os.environ.get("FAILED", "0").strip() or "0"
FAILED_FILES = os.environ.get("FAILED_FILES", "").strip()

# Covers the standard emoji blocks and symbol ranges where GitHub-style emoji
# glyphs usually appear. Keep normal ASCII Markdown punctuation intact.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ISSUE_CLOSER_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#)(\d+)",
    flags=re.IGNORECASE,
)


SYSTEM_PROMPT = """You write concise, professional GitHub pull request metadata.
Return only a strict JSON object with string keys "title" and "body".
No emojis, no markdown code fences, no marketing language.
Title: at most 80 characters, descriptive, sentence case.
Body: at most 900 characters. Mention merge status, conflict status, and review focus.
""".strip()


def git_output(args: list[str], default: str = "") -> str:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
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


def plural_commit(count: str) -> str:
    return "commit" if count == "1" else "commits"


def short_list(value: str, limit: int = 6) -> str:
    items = [line.strip() for line in re.split(r"[,\n]", value) if line.strip()]
    if not items:
        return ""
    shown = items[:limit]
    suffix = "" if len(items) <= limit else f" and {len(items) - limit} more"
    return ", ".join(shown) + suffix


def fallback_metadata() -> tuple[str, str]:
    commit_word = plural_commit(BEHIND)
    if CONFLICTS:
        title = f"Sync upstream/{UPSTREAM_BRANCH}: {BEHIND} {commit_word} with conflicts"
        status = (
            f"Merged `upstream/{UPSTREAM_BRANCH}` into `{TARGET_BRANCH}` with conflicts. "
            f"NanoGPT resolved {RESOLVED} file(s); {FAILED} still need manual review."
        )
    else:
        title = f"Sync upstream/{UPSTREAM_BRANCH}: {BEHIND} {commit_word}"
        status = f"Merged `upstream/{UPSTREAM_BRANCH}` into `{TARGET_BRANCH}` cleanly."

    files = git_output(["diff", "--name-only", f"{TARGET_BRANCH}...HEAD"])

    body_parts = [status, f"This PR brings the fork up to date by {BEHIND} {commit_word}."]
    focus = short_list(files)
    if focus:
        body_parts.append(f"Review focus: {focus}.")
    if CONFLICTS and FAILED not in {"", "0"} and FAILED_FILES:
        body_parts.append(f"Manual attention required: `{FAILED_FILES}`")
        body_parts.append(
            "Some conflicts may be structural and may not contain inline markers; "
            "see the workflow summary for per-file details."
        )
    return title, "\n\n".join(body_parts)


def build_prompt() -> str:
    context = {
        "upstream_branch": UPSTREAM_BRANCH,
        "target_branch": TARGET_BRANCH,
        "commits_behind": BEHIND,
        "conflicts": CONFLICTS,
        "ai_resolved_files": RESOLVED,
        "manual_files_count": FAILED,
        "manual_files": FAILED_FILES,
        "commits": truncate(
            git_output(["log", "--oneline", "--no-decorate", "--max-count=20", f"{TARGET_BRANCH}..HEAD"]),
            max_chars=2000,
            max_lines=20,
        ),
        "changed_files": truncate(
            git_output(["diff", "--name-status", f"{TARGET_BRANCH}...HEAD"]),
            max_chars=4000,
            max_lines=100,
        ),
        "diff_stat": truncate(
            git_output(["diff", "--stat", f"{TARGET_BRANCH}...HEAD"]),
            max_chars=3000,
            max_lines=80,
        ),
    }
    return (
        "Generate PR metadata for an automated upstream sync. "
        "The PR must be concise, descriptive, professional, and contain no emojis.\n\n"
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


def call_model() -> tuple[str, str] | None:
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
            parsed = parse_model_json(response_content(data))
            return parsed["title"], parsed["body"]
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


def parse_model_json(content: str) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        raise ValueError("model returned a markdown code fence")
    data = json.loads(extract_json_object(text))
    if not isinstance(data, dict):
        raise ValueError("model JSON was not an object")
    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("model JSON must contain string title and body")
    if not title.strip() or not body.strip():
        raise ValueError("model returned empty title or body")
    return {"title": title, "body": body}


def strip_emoji_and_controls(value: str) -> str:
    return CONTROL_RE.sub("", EMOJI_RE.sub("", value)).strip()


def neutralize_issue_closers(value: str) -> str:
    return ISSUE_CLOSER_RE.sub(lambda match: f"{match.group(1)} issue {match.group(3)}", value)


def sanitize_metadata(title: str, body: str) -> tuple[str, str]:
    fallback_title, fallback_body = fallback_metadata()
    title = " ".join(strip_emoji_and_controls(title).split()) or fallback_title
    body = neutralize_issue_closers(strip_emoji_and_controls(body)) or fallback_body

    if len(title) > 100:
        title = title[:97].rstrip() + "..."
    if len(body) > 1800:
        body = body[:1797].rstrip() + "..."

    # Preserve the workflow's conflict-surfacing invariant even if the model omits it.
    if CONFLICTS and FAILED not in {"", "0"} and FAILED_FILES and FAILED_FILES not in body:
        body = f"{body}\n\nManual attention required: `{FAILED_FILES}`"
    if CONFLICTS and FAILED not in {"", "0"} and "workflow summary" not in body.lower():
        body = (
            f"{body}\n\nSome conflicts may be structural and may not contain inline markers; "
            "see the workflow summary for per-file details."
        )

    body = neutralize_issue_closers(strip_emoji_and_controls(body))
    # Re-apply the cap: the conflict backstops above can append after the
    # earlier truncation and push the body past the intended maximum.
    if len(body) > 1800:
        body = body[:1797].rstrip() + "..."
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
        generated = call_model()
        if generated is None:
            generated = fallback_metadata()
        title, body = sanitize_metadata(*generated)
    except Exception as exc:  # noqa: BLE001 - PR metadata must never suppress PR creation
        print(f"WARNING: unexpected PR metadata failure; using fallback text: {exc}", file=sys.stderr)
        title, body = sanitize_metadata(*fallback_metadata())
    gh_out("title", title)
    gh_out("body", body)


if __name__ == "__main__":
    main()
