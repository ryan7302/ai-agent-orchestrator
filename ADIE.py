#!/usr/bin/env python3
"""
ADIE - Autonomous Development & Integration Engine
Interactive single-task mode + 24/7 daemon mode (Stage 1).
Now rebases onto the actual remote default branch to avoid merge conflicts.
"""

import subprocess
import sys
import ollama
import chromadb
from chromadb.utils import embedding_functions
import hashlib
import os
import uuid
import re
import json
import time
import random
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from git_manager import GitManager
import argparse
import requests

# ---------- Configuration (env vars with defaults) ----------
MODEL = os.environ.get("ADIE_MODEL", "deepseek-coder:6.7b-instruct-q4_K_M")
MAX_ATTEMPTS = int(os.environ.get("ADIE_MAX_ATTEMPTS", "3"))
CHROMA_PATH = os.environ.get("ADIE_CHROMA_PATH", "./chroma_db")
PROJECTS_BASE_DIR = os.environ.get("ADIE_PROJECTS_DIR", "./generated_projects")
SELF_STATE_FILE = os.environ.get("ADIE_STATE_FILE", "./aide_state.json")
EMBEDDING_DEVICE = os.environ.get("ADIE_EMBEDDING_DEVICE", "cpu")

if "GITHUB_TOKEN" in os.environ and "GH_TOKEN" not in os.environ:
    os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"]
elif "GH_TOKEN" in os.environ and "GITHUB_TOKEN" not in os.environ:
    os.environ["GITHUB_TOKEN"] = os.environ["GH_TOKEN"]

GH_AVAILABLE = subprocess.run(["which", "gh"], capture_output=True, text=True).returncode == 0
if not GH_AVAILABLE:
    print("⚠️ GitHub CLI (gh) not found. GitHub features will be disabled.")

# ---------- State management ----------
def load_state():
    if os.path.exists(SELF_STATE_FILE):
        with open(SELF_STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        "name": "ADIE",
        "version": "1.0",
        "birth_date": datetime.now().isoformat(),
        "tasks_completed": 0,
        "successful_tasks": 0,
        "total_attempts": 0,
        "domains_handled": ["python"],
        "last_task": None,
        "last_reflection": None
    }

def save_state(state):
    with open(SELF_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def self_reflect(state, task, success, attempts_used, similarity_score):
    reflection = {
        "timestamp": datetime.now().isoformat(),
        "task": task,
        "success": success,
        "attempts_used": attempts_used,
        "similarity_score": similarity_score,
        "reflection_text": ""
    }
    if success:
        reflection["reflection_text"] = (
            f"Task completed after {attempts_used} attempt(s). "
            f"Memory helped (similarity {similarity_score:.2f})."
        )
        state["successful_tasks"] += 1
    else:
        reflection["reflection_text"] = f"Task failed after {MAX_ATTEMPTS} attempts."
    state["tasks_completed"] += 1
    state["total_attempts"] += attempts_used
    state["last_task"] = task
    state["last_reflection"] = reflection["reflection_text"]
    save_state(state)
    with open("aide_log.txt", "a") as log:
        log.write(f"{reflection['timestamp']} - {reflection['reflection_text']}\n")
    return reflection

# ---------- Helper: safe naming ----------
def slugify(text, max_length=40):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text).strip('-')
    return text[:max_length].strip('-')

def safe_branch_name(task, unique_suffix, max_len=50):
    slug = slugify(task, max_len - len(unique_suffix) - 2)
    return f"pr/{slug}--{unique_suffix}"

# ---------- Memory (ChromaDB) ----------
os.makedirs(PROJECTS_BASE_DIR, exist_ok=True)
client = chromadb.PersistentClient(path=CHROMA_PATH)

print("🔧 Initializing embedding model (first run may download files)...")
embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="all-MiniLM-L6-v2",
    device=EMBEDDING_DEVICE
)
collection = client.get_or_create_collection(
    name="code_solutions",
    embedding_function=embedding_fn
)

def find_similar_task(task, threshold=0.5):
    results = collection.query(query_texts=[task], n_results=1)
    if results['distances'] and results['distances'][0]:
        distance = results['distances'][0][0]
        similarity = 1 - distance
        print(f"📊 Similarity: {similarity:.3f} (threshold {threshold})")
        if similarity >= threshold:
            return results['metadatas'][0][0], results['documents'][0][0], similarity
    return None, None, 0.0

def store_solution(task, code, output):
    doc_id = hashlib.md5(task.encode()).hexdigest()
    collection.upsert(
        documents=[code],
        metadatas=[{"task": task, "output": output[:500]}],
        ids=[doc_id]
    )
    print(f"📚 Stored solution for: {task[:50]}...")

# ---------- Internet tool (controlled) ----------
def web_fetch(url, max_bytes=10000, timeout=10):
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    if not hasattr(web_fetch, 'allowed_domains'):
        return "ERROR: No allowed domains configured."
    allowed = web_fetch.allowed_domains
    if not any(domain.endswith(d) for d in allowed):
        return f"ERROR: Domain '{domain}' not allowed."
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "ADIE/1.0"})
        resp.raise_for_status()
        content = resp.text[:max_bytes]
        return content if content else "(empty page)"
    except Exception as e:
        return f"ERROR fetching {url}: {str(e)}"

def extract_and_fetch_urls(task_text, allowed_domains):
    pattern = re.compile(r'\{url:\s*(https?://[^\}]+)\}')
    urls = pattern.findall(task_text)
    cleaned_task = pattern.sub('', task_text).strip()
    extra_context = []
    for url in urls:
        print(f"🌐 Fetching: {url}")
        setattr(web_fetch, 'allowed_domains', allowed_domains)
        content = web_fetch(url)
        extra_context.append(f"\n--- DOCS from {url} ---\n{content}\n--- END DOCS ---")
    if extra_context:
        cleaned_task += "\n" + "\n".join(extra_context)
    return cleaned_task

# ---------- Code generation ----------
def generate_code(task, error_feedback=None, similar_code=None, existing_code=None, file_path=None, extra_context=""):
    messages = [{"role": "system", "content": "You are ADIE. Output only Python code."}]
    if similar_code:
        messages.append({"role": "system", "content": f"Similar solution:\n{similar_code}\nAdapt it."})
    if existing_code:
        user_prompt = (
            f"The file {file_path} currently contains:\n\n{existing_code}\n\n"
            f"Task: {task}\n\n{extra_context}\n"
            f"Output the new, complete file content. No explanations, just the code."
        )
    else:
        user_prompt = f"Write a Python script that: {task}"
        if not similar_code:
            user_prompt += "\nOutput only code, no explanations."
        if extra_context:
            user_prompt += f"\n\nAdditional context:\n{extra_context}"
    messages.append({"role": "user", "content": user_prompt})
    if error_feedback:
        messages.append({"role": "assistant", "content": "Here's my attempt:"})
        messages.append({"role": "user", "content": f"Failed with error:\n{error_feedback}\nFix it. Output only code."})
    response = ollama.chat(model=MODEL, messages=messages)
    code = response['message']['content']
    if "```python" in code:
        code = code.split("```python")[1].split("```")[0]
    elif "```" in code:
        code = code.split("```")[1].split("```")[0]
    return code.strip()

def save_code(code, filename):
    with open(filename, "w") as f:
        f.write(code)

def run_code(filename):
    result = subprocess.run([sys.executable, filename], capture_output=True, text=True)
    return (result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr)

def ask_clarification(task):
    prompt = f"Task '{task}' is vague. Ask 1-2 clarifying questions."
    response = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    print("\n🤔 ADIE needs clarification:\n", response['message']['content'])
    return input("Your clarification: ")

# ---------- GitHub helpers ----------
def repo_exists_on_github(repo_name):
    if not GH_AVAILABLE:
        return False
    result = subprocess.run(["gh", "repo", "view", repo_name, "--json", "name"], capture_output=True, text=True)
    return result.returncode == 0

def get_repo_url(repo_name):
    if not GH_AVAILABLE:
        return None
    result = subprocess.run(["gh", "repo", "view", repo_name, "--json", "url", "-q", ".url"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None

def create_github_repo_via_cli(repo_name, description=""):
    if not GH_AVAILABLE:
        return None
    cmd = ["gh", "repo", "create", repo_name, "--public", "--description", description]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if "already exists" in result.stderr.lower() or "graphql error" in result.stderr.lower():
            print(f"📌 Repository {repo_name} already exists (or creation failed): {result.stderr[:200]}")
        else:
            print(f"❌ Failed to create repo: {result.stderr}")
            return None
    url_cmd = ["gh", "repo", "view", repo_name, "--json", "url", "-q", ".url"]
    url_result = subprocess.run(url_cmd, capture_output=True, text=True)
    if url_result.returncode == 0:
        return url_result.stdout.strip()
    return f"https://github.com/{repo_name}"

def pr_exists(repo_name, branch_name):
    if not GH_AVAILABLE:
        return False
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo_name, "--head", branch_name, "--json", "number"],
        capture_output=True, text=True
    )
    return result.returncode == 0 and result.stdout.strip() != "[]"

# ---------- Interactive single task ----------
def autonomous_agent(task, push_to_github=True, confirm_before_push=True,
                     repo_name=None, new_repo=False, target_file=None):
    state = load_state()
    print(f"\n🤖 ADIE (v{state['version']}) — Completed {state['successful_tasks']} tasks.\n")
    if len(task.split()) < 4:
        task = ask_clarification(task)

    print("🔍 Searching memory...")
    metadata, similar_code, similarity = find_similar_task(task)
    if similar_code:
        print(f"📖 Found similar: {metadata['task'][:80]}...")
    else:
        print("No similar task.")

    last_error = None
    final_code = final_output = None
    attempts_used = 0
    run_uuid = str(uuid.uuid4())[:8]
    safe_task_slug = slugify(task, 30)
    project_id = f"{run_uuid}_{safe_task_slug}"
    project_path = Path(PROJECTS_BASE_DIR) / project_id
    project_path.mkdir(parents=True, exist_ok=True)

    existing_code_content = None
    if target_file:
        if not repo_name or not GH_AVAILABLE:
            print("❌ To edit a file, you must provide --repo and have gh available.")
            return False, None, None
        print(f"📡 Cloning {repo_name} to read {target_file}...")
        tmp_clone = project_path.parent / f"clone_{run_uuid}"
        result = subprocess.run(
            ["git", "clone", get_repo_url(repo_name), str(tmp_clone)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"❌ Failed to clone: {result.stderr}")
            return False, None, None
        target_path = tmp_clone / target_file
        if not target_path.exists():
            print(f"❌ File {target_file} does not exist in the repository.")
            shutil.rmtree(tmp_clone)
            return False, None, None
        existing_code_content = target_path.read_text()
    else:
        existing_code_content = None

    for attempt in range(MAX_ATTEMPTS):
        print(f"\n--- Attempt {attempt + 1}/{MAX_ATTEMPTS} ---")
        code = generate_code(
            task, last_error,
            similar_code if attempt == 0 else None,
            existing_code=existing_code_content if attempt == 0 else None,
            file_path=target_file
        )
        print("Generated code:\n", "-" * 40, "\n", code, "\n", "-" * 40, sep="")
        if target_file:
            attempt_file = project_path / f"attempt_{attempt + 1}_{target_file.replace('/', '_')}"
            save_code(code, attempt_file)
            success = True
            output = ""
            existing_code_content = code
        else:
            attempt_file = project_path / f"attempt_{attempt + 1}.py"
            save_code(code, attempt_file)
            success, output = run_code(attempt_file)

        attempts_used = attempt + 1
        if success:
            print(f"\n✅ Success! Output:\n{output}" if output else "\n✅ Generation complete.")
            final_code, final_output = code, output
            store_solution(task, code, output)
            break
        else:
            if target_file:
                print("❌ Syntax or other issue – retrying with error feedback.")
                last_error = "Syntax error or logic error – please fix."
            else:
                print(f"\n❌ Failed:\n{output}")
                last_error = output

    if not final_code:
        print("❌ All attempts failed.")
        self_reflect(state, task, False, MAX_ATTEMPTS, similarity)
        return False, None, None

    if target_file:
        final_file_path = project_path / target_file
        final_file_path.parent.mkdir(parents=True, exist_ok=True)
        final_file_path.write_text(final_code)
        if (tmp_clone / ".git").exists():
            if (project_path / ".git").exists():
                shutil.rmtree(project_path / ".git")
            shutil.move(str(tmp_clone / ".git"), str(project_path / ".git"))
    else:
        (project_path / "main.py").write_text(final_code)
        (project_path / "README.md").write_text(f"# {task}\n\nGenerated by ADIE\n\n## Output\n\n{final_output}\n")

    git = GitManager(project_path)

    if push_to_github and GH_AVAILABLE:
        if repo_name and "/" in repo_name:
            repo_full_name = repo_name
        else:
            gh_user = os.environ.get("GITHUB_USERNAME")
            if not gh_user:
                result = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True)
                if result.returncode == 0:
                    gh_user = result.stdout.strip()
                else:
                    gh_user = "ai-agent-projects"
            task_hash = hashlib.sha1(task.encode()).hexdigest()[:8]
            repo_full_name = f"{gh_user}/{safe_task_slug}--{task_hash}"

        if new_repo or (not repo_exists_on_github(repo_full_name) and not target_file):
            if not target_file:
                print(f"🆕 Creating new repo {repo_full_name}")
                repo_url = create_github_repo_via_cli(repo_full_name, f"AI: {task}")
                if not repo_url:
                    print("❌ Could not create GitHub repo. Skipping push.")
                    self_reflect(state, task, True, attempts_used, similarity)
                    return True, final_code, final_output
                git.init_repo()
                git.add_remote(repo_url, "origin")
                git.run_git(["add", "."])
                git.run_git(["commit", "-m", f"feat: {task[:80]}"])
                git.run_git(["branch", "-M", "main"])
                push_res = git.push_to_github("origin", "main")
                if push_res.returncode != 0:
                    print(f"❌ Failed to push main: {push_res.stderr}")
                    self_reflect(state, task, True, attempts_used, similarity)
                    return True, final_code, final_output
                print("✅ Pushed initial main branch")
                default_branch = "main"
                branch_name = safe_branch_name(task, run_uuid)
                git.run_git(["checkout", "-B", branch_name])
                readme_path = project_path / "README.md"
                if readme_path.exists():
                    current_readme = readme_path.read_text()
                    updated_readme = current_readme + f"\n\n---\n*PR created: {datetime.now().isoformat()}*\n"
                    readme_path.write_text(updated_readme)
                    git.run_git(["add", "README.md"])
                    git.run_git(["commit", "-m", "docs: prepare PR with timestamp"])
            else:
                print("⚠️ Cannot edit file in a new repo. Use existing repo for --file.")
                return False, None, None
        else:
            if not repo_exists_on_github(repo_full_name):
                print(f"❌ Repository {repo_full_name} does not exist. Use --new-repo to create it.")
                return False, None, None
            print(f"📌 Working on existing repo {repo_full_name}")
            if not target_file:
                clone_dir = project_path.parent / f"clone_{run_uuid}"
                subprocess.run(["git", "clone", get_repo_url(repo_full_name), str(clone_dir)], capture_output=True, text=True)
                if not (clone_dir / ".git").exists():
                    print("❌ Clone failed.")
                    return False, None, None
                if (project_path / ".git").exists():
                    shutil.rmtree(project_path / ".git")
                shutil.move(str(clone_dir / ".git"), str(project_path / ".git"))
                shutil.rmtree(clone_dir)

            default_branch = "main"
            head_info = subprocess.run(
                ["git", "-C", str(project_path), "remote", "show", "origin"],
                capture_output=True, text=True
            )
            for line in head_info.stdout.splitlines():
                if "HEAD branch:" in line:
                    default_branch = line.split(":")[1].strip()
                    break

            git.run_git(["fetch", "origin"])
            git.run_git(["checkout", "-B", "main", f"origin/{default_branch}"])

            if not target_file:
                (project_path / "main.py").write_text(final_code)
                (project_path / "README.md").write_text(f"# {task}\n\nGenerated by ADIE\n\n## Output\n\n{final_output}\n")

            git.run_git(["add", "."])
            git.run_git(["commit", "-m", f"feat: {task[:80]}"])
            branch_name = safe_branch_name(task, run_uuid)
            git.run_git(["checkout", "-B", branch_name])

        print(f"🚀 Pushing branch '{branch_name}' to origin...")
        push_result = git.run_git(["push", "-u", "origin", branch_name])
        if push_result.returncode != 0:
            print(f"❌ Push failed: {push_result.stderr}")
        else:
            print(f"✅ Pushed branch '{branch_name}'")
            if not pr_exists(repo_full_name, branch_name):
                pr_title = task[:80]
                pr_body = f"Generated by ADIE\n\nTask: {task}\n\n"
                if final_output:
                    pr_body += f"Output preview:\n{final_output[:200]}"
                with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
                    f.write(pr_body)
                    tmp = f.name
                try:
                    pr_cmd = [
                        "gh", "pr", "create", "--repo", repo_full_name,
                        "--title", pr_title, "--body-file", tmp,
                        "--head", branch_name, "--base", default_branch
                    ]
                    pr_res = subprocess.run(pr_cmd, capture_output=True, text=True)
                    if pr_res.returncode == 0:
                        print(f"✅ PR created: {pr_res.stdout.strip()}")
                    else:
                        print(f"⚠️ PR creation failed: {pr_res.stderr}")
                finally:
                    os.unlink(tmp)
            else:
                print("ℹ️ A PR already exists for this branch. Skipping creation.")
    else:
        git.init_repo()
        git.run_git(["add", "."])
        git.run_git(["commit", "-m", f"feat: {task[:80]}"])

    print(f"📁 Project saved to: {project_path}")
    self_reflect(state, task, True, attempts_used, similarity)
    return True, final_code, final_output

# ---------- Daemon mode functions ----------
def suggest_files(workspace, task, allowed_files):
    tree = get_file_tree(workspace)
    prompt = (
        f"Project file tree:\n{tree}\n\n"
        f"Task: {task}\n\n"
        "Return ONLY a Python list of relative file paths that need to be modified. "
        "Example: ['src/app.py', 'utils.py']. No other text."
    )
    response = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    raw = response['message']['content']
    print(f"📝 LLM suggested files raw: {raw[:200]}...")
    files = []
    cleaned = raw.strip()
    cleaned = re.sub(r'```(?:python)?\s*', '', cleaned)
    cleaned = re.sub(r'```', '', cleaned)
    try:
        evaluated = eval(cleaned)
        if isinstance(evaluated, list):
            files = [str(f) for f in evaluated if isinstance(f, str) and f.strip()]
    except:
        lines = raw.splitlines()
        for line in lines:
            line = line.strip().strip('"\',[] ')
            if re.match(r'^[\w\-./]+\.py$', line):
                files.append(line)
    existing = [f for f in files if (workspace / f).exists()]
    if existing:
        return existing[:5]
    mentioned = re.findall(r'[\w\-]+\.py', task)
    for m in mentioned:
        if (workspace / m).exists():
            return [m]
    py_files = list(workspace.glob('*.py'))
    if len(py_files) == 1:
        return [py_files[0].name]
    return []

def get_file_tree(workspace):
    result = subprocess.run(["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
                            cwd=workspace, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "No files"

def generate_changes(workspace, files, task, test_cmd, lint_cmd, max_attempts, allowed_domains):
    originals = {}
    for f in files:
        path = workspace / f
        if path.exists():
            originals[f] = path.read_text()

    last_error = None
    for attempt in range(max_attempts):
        print(f"\n🔄 Attempt {attempt+1}/{max_attempts}")
        for f in files:
            path = workspace / f
            current_code = path.read_text() if path.exists() else ""
            new_code = generate_code(
                task=task,
                error_feedback=last_error if attempt > 0 else None,
                existing_code=current_code,
                file_path=f
            )
            if new_code is None:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_code)
            print(f"✏️ Updated {f}")

        if test_cmd:
            test_result = subprocess.run(test_cmd, shell=True, cwd=workspace, capture_output=True, text=True)
            if test_result.returncode != 0:
                combined = test_result.stderr + test_result.stdout
                if re.search(r"No module named|ModuleNotFoundError", combined):
                    print("⚠️ Test command failed due to missing test framework. Skipping tests.")
                    break
                else:
                    print(f"❌ Tests failed:\n{test_result.stderr[-500:]}")
                    last_error = test_result.stderr + "\n" + test_result.stdout
                    continue
            else:
                print("✅ Tests passed")
        else:
            print("ℹ️ No test command, assuming success.")

        if lint_cmd:
            lint_result = subprocess.run(lint_cmd, shell=True, cwd=workspace, capture_output=True, text=True)
            if lint_result.returncode != 0:
                print(f"⚠️ Lint issues (non-fatal): {lint_result.stdout[:300]}")
        return True

    print("❌ All attempts failed. Restoring files.")
    for f, content in originals.items():
        (workspace / f).write_text(content)
    return False

def detect_default_branch(workspace):
    result = subprocess.run(
        ["git", "-C", str(workspace), "remote", "show", "origin"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if "HEAD branch:" in line:
            return line.split(":")[1].strip()
    return "main"

def create_pr(repo_name, task, branch, base_branch):
    title = task[:80]
    body = f"Autonomous PR by ADIE\n\nTask: {task}"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        cmd = [
            "gh", "pr", "create", "--repo", repo_name,
            "--title", title, "--body-file", tmp,
            "--head", branch, "--base", base_branch
        ]
        pr_res = subprocess.run(cmd, capture_output=True, text=True)
        if pr_res.returncode == 0:
            return pr_res.stdout.strip()
        else:
            print(f"⚠️ PR creation failed: {pr_res.stderr}")
    finally:
        os.unlink(tmp)
    return None

def autonomous_daemon(config_path, tasks_path):
    print("🚀 Starting ADIE Daemon...")
    config = json.load(open(config_path))
    repo_name = config["repo"]
    allowed_domains = config.get("allowed_domains", [])
    test_cmd = config.get("test_command")
    lint_cmd = config.get("lint_command")
    max_attempts = config.get("max_attempts", MAX_ATTEMPTS)
    allowed_files = config.get("allowed_files", ["**"])

    workspace = Path("./adie_workspace") / repo_name.replace("/", "_")
    done_file = Path(tasks_path).stem + "_done.txt"
    failed_file = Path(tasks_path).stem + "_failed.txt"

    if not (workspace / ".git").exists():
        print(f"🔄 Cloning {repo_name}...")
        clone_url = get_repo_url(repo_name)
        if not clone_url:
            print("❌ Could not get repo URL. Exiting.")
            return
        subprocess.run(["git", "clone", clone_url, str(workspace)])
        git = GitManager(workspace)
        git.add_remote(clone_url, "origin")
    else:
        print(f"📦 Workspace exists at {workspace}")

    while True:
        tasks = read_tasks_from_file(tasks_path)
        if not tasks:
            print("😴 No tasks. Sleeping...")
            time.sleep(60)
            continue

        task_raw = tasks[0]
        print(f"\n📋 Task: {task_raw}")
        remove_task_from_file(tasks_path, task_raw)

        try:
            task_enriched = extract_and_fetch_urls(task_raw, allowed_domains)

            file_directive = None
            match = re.match(r'@file:\s*(\S+)', task_enriched)
            if match:
                file_directive = match.group(1)
                task_enriched = task_enriched[len(match.group(0)):].strip()

            git = GitManager(workspace)

            # --- Detect the actual remote default branch ---
            default_branch = detect_default_branch(workspace)   # e.g. "main" or "master"
            git.run_git(["fetch", "origin"])
            git.run_git(["checkout", default_branch])
            git.run_git(["pull", "origin", default_branch])
            git.run_git(["reset", "--hard", f"origin/{default_branch}"])

            task_id = slugify(task_raw, 25) + "-" + uuid.uuid4().hex[:6]
            branch = f"adie/{slugify(task_raw)}--{task_id}"
            git.run_git(["checkout", "-B", branch])

            if file_directive:
                files_to_edit = [file_directive]
            else:
                files_to_edit = suggest_files(workspace, task_enriched, allowed_files)

            if not files_to_edit:
                print("⚠️ No files to edit. Marking as failed.")
                write_task_to_file(failed_file, task_raw)
                continue

            print(f"📝 Files to edit: {files_to_edit}")

            success = generate_changes(workspace, files_to_edit, task_enriched,
                                       test_cmd, lint_cmd, max_attempts, allowed_domains)
            if not success:
                write_task_to_file(failed_file, task_raw)
                continue

            commit_msg = f"feat(adie): {task_raw[:80]}"
            git.run_git(["add", "."])
            status = git.run_git(["status", "--short"]).stdout.strip()
            if not status:
                print("ℹ️ No changes to commit.")
                write_task_to_file(failed_file, task_raw)
                continue

            git.run_git(["commit", "-m", commit_msg])

            # --- Rebase onto remote default branch BEFORE pushing ---
            git.run_git(["fetch", "origin"])
            rebase_result = git.run_git(["rebase", f"origin/{default_branch}"])
            if rebase_result.returncode != 0:
                print(f"❌ Rebase failed: {rebase_result.stderr}")
                git.run_git(["rebase", "--abort"])
                write_task_to_file(failed_file, task_raw)
                continue
            # --------------------------------------------------------

            push_res = git.push_to_github("origin", branch)
            if push_res.returncode != 0:
                print(f"❌ Push failed: {push_res.stderr}")
                write_task_to_file(failed_file, task_raw)
                continue

            print(f"✅ Pushed branch {branch}")

            pr_url = create_pr(repo_name, task_raw, branch, default_branch)
            if pr_url:
                print(f"📬 PR: {pr_url}")
            write_task_to_file(done_file, task_raw)

        except Exception as e:
            print(f"💥 Unexpected error: {e}")
            import traceback; traceback.print_exc()
            write_task_to_file(failed_file, task_raw)

        delay = random.randint(60, 300)
        print(f"⏳ Sleeping {delay}s...")
        time.sleep(delay)

def read_tasks_from_file(path):
    if not Path(path).exists():
        return []
    with open(path, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def remove_task_from_file(path, task):
    tasks = read_tasks_from_file(path)
    if task in tasks:
        tasks.remove(task)
        with open(path, 'w') as f:
            for t in tasks:
                f.write(t + '\n')

def write_task_to_file(path, task):
    with open(path, 'a') as f:
        f.write(task + '\n')

# ---------- Entry point ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ADIE – Autonomous Development & Integration Engine")
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode: (default) single task, daemon")

    single_parser = subparsers.add_parser("run", help="Run a single task")
    single_parser.add_argument("task", nargs="+", help="Description of the coding task")
    single_parser.add_argument("--repo", help="GitHub repository name")
    single_parser.add_argument("--new-repo", action="store_true")
    single_parser.add_argument("--file", help="Path to a file in the existing repository to edit")
    single_parser.add_argument("--no-push", action="store_true")
    single_parser.add_argument("--yes", "-y", action="store_true")

    daemon_parser = subparsers.add_parser("daemon", help="Run in 24/7 autonomous daemon mode")
    daemon_parser.add_argument("--config", required=True)
    daemon_parser.add_argument("--tasks", required=True)

    args, remaining = parser.parse_known_args()
    if args.mode is None:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
            args.task = sys.argv[1:]
            args.repo = None
            args.new_repo = False
            args.file = None
            args.no_push = False
            args.yes = False
            args.mode = "run"
        else:
            parser.print_help()
            sys.exit(1)

    if args.mode == "run" or args.mode is None:
        task = " ".join(args.task)
        print("=" * 60)
        print("🤖 ADIE - Autonomous Development & Integration Engine")
        print("=" * 60)
        success, _, _ = autonomous_agent(
            task,
            push_to_github=not args.no_push,
            confirm_before_push=not args.yes,
            repo_name=args.repo,
            new_repo=args.new_repo,
            target_file=args.file
        )
        if success:
            print("\n🎉 ADIE completed the task.")
            state = load_state()
            print(f"📊 Stats: {state['successful_tasks']} of {state['tasks_completed']} successful.")
        else:
            print("\n😞 Task failed.")
    elif args.mode == "daemon":
        if not GH_AVAILABLE:
            print("❌ GitHub CLI (gh) required for daemon mode. Exiting.")
            sys.exit(1)
        autonomous_daemon(args.config, args.tasks)