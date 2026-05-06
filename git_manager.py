import subprocess
import shlex
from pathlib import Path
from datetime import datetime

class GitManager:
    """Safe Git operations with no shell injection."""
    def __init__(self, repo_path):
        self.repo_path = Path(repo_path).resolve()
        self.repo_path.mkdir(parents=True, exist_ok=True)

    def run_git(self, command):
        """Run a git command safely. Accepts a string or a list of arguments."""
        if isinstance(command, str):
            args = shlex.split(command)
        else:
            args = command
        cmd = ["git", "-C", str(self.repo_path)] + args
        return subprocess.run(cmd, capture_output=True, text=True)

    def init_repo(self):
        """Initialize a new git repository."""
        return self.run_git(["init"])

    def add_remote(self, remote_url, remote_name="origin"):
        """Add a remote repository."""
        existing = self.run_git(["remote"]).stdout.strip().split()
        if remote_name in existing:
            self.run_git(["remote", "remove", remote_name])
        return self.run_git(["remote", "add", remote_name, remote_url])

    def commit_all(self, message):
        """Commit all changes."""
        self.run_git(["add", "."])
        status = self.run_git(["status", "--short"]).stdout.strip()
        if not status:
            print("ℹ️ No changes to commit.")
            return None
        return self.run_git(["commit", "-m", message])

    def get_current_branch(self):
        """Get the current branch name."""
        result = self.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch != "HEAD":
                return branch
        return "main"

    def push_to_github(self, remote_name="origin", branch=None):
        """Push to GitHub using configured credentials."""
        if branch is None:
            branch = self.get_current_branch()
        print(f"📤 Pushing to GitHub ({remote_name}/{branch})...")
        return self.run_git(["push", "-u", remote_name, branch])

    def commit_generated_project(self, project_name, component_type="code"):
        """Commit a newly generated project to the local repository."""
        if not (self.repo_path / ".git").exists():
            self.init_repo()
            print(f"🔄 Initialized new repository at {self.repo_path}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit_msg = f"feat({component_type}): add {project_name} generated at {timestamp}"
        result_commit = self.commit_all(commit_msg)

        if result_commit and result_commit.returncode == 0:
            print(f"✅ Committed {project_name} to Git: {commit_msg}")
            print(f"📋 status:\n{self.run_git(['status', '--short']).stdout}")
        elif result_commit and result_commit.stderr and "nothing to commit" in result_commit.stderr:
            print("ℹ️ No changes to commit.")
        elif result_commit:
            print(f"❌ Commit failed: {result_commit.stderr}")
        return result_commit