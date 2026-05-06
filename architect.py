#!/usr/bin/env python3
"""
Conductor – Waits for a clean repo (zero open PRs) before giving ADIE the next task.
Correctly interprets GitHub mergeable status (MERGEABLE / CONFLICTING / UNKNOWN).
"""

import sys
import os
import time
import subprocess
import re
import requests
import base64
import json
from pathlib import Path

print("🚀 Conductor starting...", flush=True)

# ---------- Config ----------
API_PROVIDER = "groq"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if API_PROVIDER == "groq":
    API_KEY = GROQ_API_KEY
    if not API_KEY:
        raise SystemExit("Please set GROQ_API_KEY. Get one at console.groq.com")
    import groq
    client = groq.Groq(api_key=API_KEY)
    PLANNER_MODEL = "llama-3.3-70b-versatile"
elif API_PROVIDER == "openai":
    API_KEY = OPENAI_API_KEY
    if not API_KEY:
        raise SystemExit("Please set OPENAI_API_KEY")
    from openai import OpenAI
    client = OpenAI(api_key=API_KEY)
    PLANNER_MODEL = "gpt-4o"
else:
    raise ValueError("Unsupported API_PROVIDER")

GOAL_FILE  = "goal.txt"
TASKS_FILE = "adie_tasks.txt"
REPO       = "ryan7302/adie-specialists"
GH_TOKEN   = os.environ.get("GITHUB_TOKEN")

# ---------- GitHub Helpers ----------
def get_current_code(file_path="arachne.py"):
    url = f"https://api.github.com/repos/{REPO}/contents/{file_path}"
    headers = {"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {}
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        content = r.json().get("content", "")
        return base64.b64decode(content).decode()
    print(f"⚠️  Could not fetch {file_path}: {r.status_code}")
    return None

def get_all_open_pr_numbers():
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--json", "number",
         "--jq", ".[].number"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return [int(x) for x in result.stdout.strip().splitlines()]
    return []

def get_pr_diff(pr_number):
    if not pr_number:
        return None
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
        capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None

def is_pr_mergeable(pr_number):
    """
    Checks if a PR is mergeable.
    Polls the GitHub API if the status is 'UNKNOWN' (which happens immediately after PR creation).
    """
    for _ in range(6):  # Poll up to 6 times (30 seconds total)
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", REPO, "--json", "mergeable", "-q", ".mergeable"],
            capture_output=True, text=True
        )
        status = result.stdout.strip().lower()
        
        # GitHub returns "MERGEABLE", "CONFLICTING", or "UNKNOWN"
        if status == "mergeable":
            return True
        elif status == "conflicting":
            return False
            
        # If status is "unknown", GitHub is still calculating. Wait and try again.
        time.sleep(5)
        
    # Default to False if it stays "unknown" after 30 seconds
    return False

def merge_pr(pr_number):
    subprocess.run(
        ["gh", "pr", "merge", str(pr_number), "--repo", REPO, "--squash"],
        capture_output=True
    )
    time.sleep(5)

def cleanup_merged_branches():
    """Delete remote branches that have been merged into origin/main."""
    result = subprocess.run(
        ["git", "branch", "-r", "--merged", "origin/main"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        branch = line.strip().replace("origin/", "")
        if branch.startswith("adie/") and branch != "main":
            subprocess.run(
                ["git", "push", "origin", "--delete", branch],
                capture_output=True
            )

def close_pr(pr_number):
    subprocess.run(
        ["gh", "pr", "close", str(pr_number), "--repo", REPO],
        capture_output=True
    )

def diff_contains_substantial_changes(diff_text):
    if not diff_text:
        return False
    non_trivial = [line for line in diff_text.splitlines()
                   if line.strip() and not line.strip().startswith(("+ #", "- #", "+#", "-#", "//", "<!--"))]
    return len(non_trivial) >= 3

def wait_until_no_open_prs(timeout=600):
    waited = 0
    while True:
        open_prs = get_all_open_pr_numbers()
        if not open_prs:
            print("✅ No open PRs – repo is clean.")
            return True
        print(f"⏳ Open PRs: {open_prs}. Waiting...")
        time.sleep(30)
        waited += 30
        if waited > timeout:
            print("❌ Timeout waiting for clean repo.")
            return False

# ---------- Task File Helpers ----------
def write_task(task):
    with open(TASKS_FILE, "a") as f:
        f.write(task + "\n")

# ---------- Planner ----------
SYSTEM_PROMPT = """You are a meticulous software architect supervising a junior code agent.
The agent can only follow instructions that:
- Begin with '@file: <filename>'
- Then contain a SHORT, specific edit to make.
- The edit must be so small that a junior developer can complete it in one try.
- The agent cannot "implement full features" – break everything down.

Every time you are called, you will see the current code of arachne.py.
Your job is to move it closer to the goal by issuing ONE tiny, actionable edit.

**CRITICAL: Output exactly ONE line. No code blocks, no backticks, no multiple lines.**
Example: @file: arachne.py Add a method run_tests(self) that runs self.config['test_command'] via subprocess.run(..., shell=True) and returns True if returncode is 0.

If the goal is already achieved, output exactly: GOAL_ACHIEVED
Otherwise output ONLY the task line.
"""

def plan_next_task(goal, current_code, last_closed_diff=None):
    user_msg = f"Goal: {goal}\n\nCurrent code of arachne.py:\n```python\n{current_code}\n```"
    if last_closed_diff:
        user_msg += f"\n\nLast closed PR diff (use if it correctly implements a missing feature):\n{last_closed_diff}"

    if API_PROVIDER == "groq":
        completion = client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
            max_tokens=400
        )
        raw = completion.choices[0].message.content.strip()
    else:
        completion = client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
            max_tokens=400
        )
        raw = completion.choices[0].message.content.strip()

    lines = raw.splitlines()
    if len(lines) > 1:
        print(f"⚠️  Model returned {len(lines)} lines. Using first line only.")
        return lines[0].strip()
    return raw

# ---------- Main Loop ----------
def main():
    goal = Path(GOAL_FILE).read_text().strip()
    if not goal:
        print("Put a goal in goal.txt")
        return

    print(f"🎯 Conductor orchestrating goal: {goal}")
    print(f"   Model: {PLANNER_MODEL} ({API_PROVIDER})")

    previous_task = None
    consecutive_empty_diffs = 0
    last_closed_pr = None

    while True:
        if not wait_until_no_open_prs():
            print("❌ Could not achieve clean repo. Exiting.")
            return

        code = get_current_code("arachne.py")
        if code is None:
            print("❌ Could not fetch code. Retrying in 60s...")
            time.sleep(60)
            continue

        closed_diff = None
        if last_closed_pr:
            closed_diff = get_pr_diff(last_closed_pr)
            if closed_diff:
                print(f"📋 Including diff from closed PR #{last_closed_pr}")

        task = plan_next_task(goal, code, closed_diff)
        print(f"\n🗒️  Planner says: {task}")

        if task == "GOAL_ACHIEVED":
            print("✅ Goal achieved! Exiting.")
            break

        if task == previous_task and closed_diff:
            print("⚠️  Same task repeated even with closed PR diff. Stopping.")
            break
        previous_task = task

        write_task(task)
        print(f"📝 Gave ADIE task: {task}")

        print("⏳ Waiting for ADIE to open a PR...")
        time.sleep(20)
        waited = 0
        new_pr = None
        while True:
            all_open = get_all_open_pr_numbers()
            if len(all_open) == 1:
                new_pr = all_open[0]
                print(f"🔍 Detected new PR #{new_pr}")
                break
            elif len(all_open) > 1:
                print(f"⚠️  Unexpected multiple open PRs: {all_open}. Waiting for cleanup...")
            time.sleep(30)
            waited += 30
            if waited > 600:
                print("❌ Timeout waiting for a single new PR. Continuing.")
                break

        if new_pr is None:
            continue

        if not is_pr_mergeable(new_pr):   # now correctly handles "MERGEABLE"
            print(f"⚠️  PR #{new_pr} has conflicts (or timed out). Closing it.")
            close_pr(new_pr)
            last_closed_pr = new_pr
            consecutive_empty_diffs = 0
            time.sleep(5)
            continue

        new_diff = get_pr_diff(new_pr)
        if diff_contains_substantial_changes(new_diff):
            print(f"🔀 Merging substantial PR #{new_pr}...")
            merge_pr(new_pr)
            cleanup_merged_branches()  # Call the cleanup function after merging
            consecutive_empty_diffs = 0
            last_closed_pr = None
        else:
            print(f"⚠️  PR #{new_pr} has trivial changes. Closing.")
            close_pr(new_pr)
            last_closed_pr = new_pr
            consecutive_empty_diffs += 1
            if consecutive_empty_diffs >= 3:
                print("❌ Too many trivially changed PRs. Stopping.")
                return

if __name__ == "__main__":
    main()