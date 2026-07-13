#!/usr/bin/env python3
"""
Docker Image Tag Sync Automation Script
"""
import os
import re
import sys
import yaml
import logging
import time
from typing import Any, Dict, List, Tuple

# We import PyGithub elements, wrapping imports in try/except for CLI dry runs where they might not be installed yet
try:
    from github import Github, GithubException
except ImportError:
    # Will be mocked or handled in testing
    Github = None
    GithubException = Exception

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_image_tag")


def load_config(config_path: str) -> dict:
    """
    Reads and parses updater-config.yml.
    Validates required fields exist: registry, targets.
    Validates each target has branch and file.
    Raises ValueError with a clear message if validation fails.
    """
    if not os.path.exists(config_path):
        raise ValueError(f"Config file not found at path: {config_path}")
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse YAML config: {e}")

    if not config or not isinstance(config, dict):
        raise ValueError("Config file must be a valid YAML object.")

    if "registry" not in config or not config["registry"]:
        raise ValueError("Missing or empty required field: 'registry'")

    if "targets" not in config or not isinstance(config["targets"], list):
        raise ValueError("Missing or invalid 'targets' list in config")

    for idx, target in enumerate(config["targets"]):
        if not isinstance(target, dict):
            raise ValueError(f"Target at index {idx} is not an object")
        if "branch" not in target or not target["branch"]:
            raise ValueError(f"Target at index {idx} is missing required 'branch' field")
        if "file" not in target or not target["file"]:
            raise ValueError(f"Target at index {idx} is missing required 'file' field")

    return config


def resolve_settings(config: dict) -> dict:
    """
    Merges user settings with defaults.
    """
    user_settings = config.get("settings", {})
    if not isinstance(user_settings, dict):
        user_settings = {}

    defaults = {
        "pr_title_template": "chore: update image tag to {new_tag}",
        "feature_branch_template": "auto/image-tag-{new_tag}",
        "pr_labels": ["automation", "image-update"],
        "auto_merge": False,
        "pr_body_template": (
            "Automated Pull Request to update the Docker image tag.\n\n"
            "- **Previous Tag:** {old_tag}\n"
            "- **New Tag:** {new_tag}\n"
            "- **Triggered By:** {triggered_by}\n"
        )
    }

    resolved = {}
    for key, default_val in defaults.items():
        val = user_settings.get(key)
        if val is None:
            resolved[key] = default_val
        else:
            resolved[key] = val

    return resolved


def navigate_yaml_path(data: Any, path: str) -> Any:
    """
    Supports paths like "image", "spec.containers[0].image", "spec.template.spec.containers[0].image"
    Handles array indexing with [N] syntax.
    Returns the value at that path. Raises ValueError if not found.
    """
    if not path:
        return data
    
    parts = path.split('.')
    current = data
    
    for part in parts:
        # Check for array indexing, e.g., containers[0]
        match = re.match(r'^([^\[]+)(?:\[(\d+)\])+$', part)
        if match:
            key = match.group(1)
            # Find all indices
            indices = [int(i) for i in re.findall(r'\[(\d+)\]', part)]
            
            if not isinstance(current, dict) or key not in current:
                raise ValueError(f"Path part '{key}' not found in structure")
            current = current[key]
            
            for idx in indices:
                if not isinstance(current, list) or idx >= len(current):
                    raise ValueError(f"Index {idx} out of range or not a list in '{part}'")
                current = current[idx]
        else:
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f"Path part '{part}' not found in structure")
            current = current[part]
            
    return current


def extract_image_tag(yaml_content: str, image_field_path: str, registry: str) -> str:
    """
    Parses YAML content string.
    Navigates to the field specified by image_field_path.
    Extracts and returns just the tag portion after the last :.
    Raises ValueError if field not found or image doesn't match registry.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except Exception as e:
        raise ValueError(f"Invalid YAML content: {e}")
        
    try:
        image_val = navigate_yaml_path(data, image_field_path)
    except ValueError as e:
        raise ValueError(f"Could not find field path '{image_field_path}': {e}")
        
    if not isinstance(image_val, str):
        raise ValueError(f"Value at path '{image_field_path}' is not a string: {image_val}")
        
    # Check if registry matches
    if not image_val.startswith(registry):
        raise ValueError(f"Image '{image_val}' does not match registry '{registry}'")
        
    if ":" not in image_val:
        raise ValueError(f"Image '{image_val}' does not contain a tag (missing ':')")
        
    tag = image_val.split(":")[-1]
    return tag


def replace_image_tag(yaml_content: str, image_field_path: str, registry: str, new_tag: str) -> Tuple[str, str]:
    """
    Uses string replacement (NOT full YAML dump) to preserve formatting, comments, and ordering.
    Finds the line with {registry}:{old_tag} and replaces {old_tag} with {new_tag}.
    Returns (updated_yaml_string, old_tag).
    """
    old_tag = extract_image_tag(yaml_content, image_field_path, registry)
    
    target_str = f"{registry}:{old_tag}"
    replacement_str = f"{registry}:{new_tag}"
    
    if target_str not in yaml_content:
        # Try line-by-line matching with quotes handling
        lines = yaml_content.splitlines(keepends=True)
        replaced = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                new_lines.append(line)
                continue
            # Look for registry and old_tag in same line (ignoring comments)
            if registry in line and old_tag in line and not replaced:
                # Replace the exact match including tag
                for quote in ('"', "'", ''):
                    q_target = f"{quote}{registry}:{old_tag}{quote}"
                    q_replacement = f"{quote}{registry}:{new_tag}{quote}"
                    if q_target in line:
                        line = line.replace(q_target, q_replacement)
                        replaced = True
                        break
                if not replaced:
                    line = line.replace(old_tag, new_tag)
                    replaced = True
            new_lines.append(line)
        if not replaced:
            raise ValueError(f"Could not find exact image reference '{target_str}' in YAML content")
        updated_yaml = "".join(new_lines)
    else:
        updated_yaml = yaml_content.replace(target_str, replacement_str)
        
    return updated_yaml, old_tag


def with_retry(func):
    """
    Decorator for GitHubClient API methods providing retry logic.
    Retries up to 3 attempts with exponential backoff for 5xx errors and rate limits (403).
    """
    def wrapper(*args, **kwargs):
        attempts = 3
        backoff = 2
        for attempt in range(1, attempts + 1):
            try:
                return func(*args, **kwargs)
            except GithubException as e:
                # Rate limit (403) or Server Error (5xx) are retryable
                is_retryable = (e.status >= 500) or (e.status == 403)
                if is_retryable and attempt < attempts:
                    logger.warning(
                        f"GitHub API error {e.status} on attempt {attempt}. "
                        f"Retrying in {backoff}s..."
                    )
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    raise e
    return wrapper


class GitHubClient:
    """
    GitHub API client wrapper using PyGithub.
    """
    def __init__(self, token: str, repo_full_name: str):
        if not token:
            raise ValueError("GitHub Token is required")
        if not repo_full_name:
            raise ValueError("Repository full name (owner/repo) is required")
        self.github = Github(token)
        self.repo = self.github.get_repo(repo_full_name)

    @with_retry
    def get_file(self, branch: str, file_path: str) -> Tuple[str, str]:
        """
        Fetches file content from a specific branch using Contents API.
        Returns (decoded_content_string, file_sha).
        """
        try:
            contents = self.repo.get_contents(file_path, ref=branch)
            if isinstance(contents, list):
                raise ValueError(f"Expected a file at '{file_path}' but got a directory.")
            decoded = contents.decoded_content.decode("utf-8")
            return decoded, contents.sha
        except GithubException as e:
            if e.status == 404:
                raise ValueError(f"File '{file_path}' or branch '{branch}' not found on GitHub")
            raise e

    @with_retry
    def get_branch_sha(self, branch: str) -> str:
        """
        Returns the latest commit SHA of a branch.
        """
        try:
            b = self.repo.get_branch(branch)
            return b.commit.sha
        except GithubException as e:
            if e.status == 404:
                raise ValueError(f"Branch '{branch}' not found on GitHub")
            raise e

    @with_retry
    def get_open_pr(self, head: str, base: str) -> str:
        """
        Checks if there is an open PR from head -> base.
        Returns the HTML URL of the PR if found, otherwise None.
        """
        try:
            prs = self.repo.get_pulls(state="open", head=f"{self.repo.owner.login}:{head}", base=base)
            if prs.totalCount > 0:
                return prs[0].html_url
            return None
        except Exception as e:
            logger.warning(f"Error checking for open PR head={head} base={base}: {e}")
            return None

    @with_retry
    def create_branch(self, new_branch: str, from_sha: str) -> bool:
        """
        Creates a new branch from the given SHA.
        If branch already exists, deletes it first and recreates.
        Returns True on success.
        """
        ref_path = f"refs/heads/{new_branch}"
        try:
            ref = self.repo.get_git_ref(f"heads/{new_branch}")
            # Check if there is any open PR using this branch as head
            prs = self.repo.get_pulls(state="open", head=f"{self.repo.owner.login}:{new_branch}")
            if prs.totalCount > 0:
                logger.info(f"Branch '{new_branch}' is currently in use by an open PR. Skipping deletion/recreation.")
                return True
            
            logger.info(f"Branch '{new_branch}' already exists. Deleting it first...")
            ref.delete()
        except GithubException as e:
            if e.status != 404:
                raise e
        
        self.repo.create_git_ref(ref=ref_path, sha=from_sha)
        logger.info(f"Created branch '{new_branch}' from SHA {from_sha}")
        return True

    @with_retry
    def commit_file(self, branch: str, file_path: str, content: str, sha: str, message: str) -> str:
        """
        Commits updated file content to the specified branch.
        Returns the commit SHA.
        """
        res = self.repo.update_file(
            path=file_path,
            message=message,
            content=content,
            sha=sha,
            branch=branch
        )
        return res["commit"].sha

    @with_retry
    def create_pull_request(self, head: str, base: str, title: str, body: str, labels: List[str]) -> str:
        """
        Creates a PR from head -> base.
        Adds labels.
        Returns PR URL.
        If PR already exists for this head->base, returns existing PR URL.
        """
        try:
            pr = self.repo.create_pull(
                title=title,
                body=body,
                head=head,
                base=base
            )
            logger.info(f"Created PR: {pr.html_url}")
        except GithubException as e:
            if e.status == 422:
                # Find existing PRs
                prs = self.repo.get_pulls(state="open", head=f"{self.repo.owner.login}:{head}", base=base)
                if prs.totalCount > 0:
                    pr = prs[0]
                    logger.info(f"Found existing open PR: {pr.html_url}")
                else:
                    raise e
            else:
                raise e
        
        if labels:
            try:
                pr.add_to_labels(*labels)
            except Exception as le:
                logger.warning(f"Failed to add labels to PR {pr.html_url}: {le}")
                
        return pr.html_url


def create_dummy_yaml_for_path(path: str, value: str) -> str:
    """
    Dynamically constructs a mock nested YAML string matching the dot-notation path layout.
    """
    parts = path.split('.')
    root = {}
    current = root
    for i, part in enumerate(parts):
        is_last = (i == len(parts) - 1)
        match = re.match(r'^([^\[]+)\[(\d+)\]$', part)
        if match:
            key = match.group(1)
            idx = int(match.group(2))
            arr = []
            while len(arr) <= idx:
                arr.append({})
            current[key] = arr
            if is_last:
                arr[idx] = value
            else:
                current = arr[idx]
        else:
            if is_last:
                current[part] = value
            else:
                current[part] = {}
                current = current[part]
    return yaml.dump(root)


def sync_all_targets(
    config: dict,
    github_client: Any,
    new_tag: str,
    triggered_by: str,
    dry_run: bool = False
) -> List[Dict[str, Any]]:
    """
    Iterates through all targets in config and syncs them.
    Catches exceptions per-target (never fails the whole batch).
    """
    resolved_settings = resolve_settings(config)
    registry = config["registry"]
    image_field_path = config.get("image_field_path", "image")
    
    results = []
    
    for target in config["targets"]:
        branch = target["branch"]
        file_path = target["file"]
        desc = target.get("description", f"{branch}:{file_path}")
        
        logger.info(f"Processing target: {desc} (branch: {branch}, file: {file_path})")
        
        result = {
            "branch": branch,
            "file": file_path,
            "old_tag": "unknown",
            "new_tag": new_tag,
            "pr_url": "",
            "status": "failed",
            "error": ""
        }
        
        try:
            feature_branch = resolved_settings["feature_branch_template"].format(new_tag=new_tag, branch=branch)
            
            # Check for existing open PR
            existing_pr_url = None
            if github_client and hasattr(github_client, "get_open_pr"):
                val = github_client.get_open_pr(feature_branch, branch)
                if isinstance(val, str):
                    existing_pr_url = val
                
            if existing_pr_url:
                logger.info(f"  [SKIP] Open PR already exists for branch {branch}: {existing_pr_url}")
                result["status"] = "skipped"
                result["old_tag"] = "n/a"
                result["pr_url"] = existing_pr_url
                results.append(result)
                continue

            if dry_run:
                if github_client:
                    yaml_content, sha = github_client.get_file(branch, file_path)
                else:
                    logger.info(f"[Dry Run] Simulating fetch of file {file_path} from branch {branch}")
                    yaml_content = create_dummy_yaml_for_path(image_field_path, f"{registry}:v1.0.0")
                    sha = "dummy-sha"
            else:
                yaml_content, sha = github_client.get_file(branch, file_path)
                
            old_tag = extract_image_tag(yaml_content, image_field_path, registry)
            result["old_tag"] = old_tag
            
            if old_tag == new_tag:
                logger.info(f"Target {desc} is already up to date with tag '{new_tag}'. Skipping.")
                result["status"] = "skipped"
                results.append(result)
                continue
                
            updated_yaml, _ = replace_image_tag(yaml_content, image_field_path, registry, new_tag)
            
            if dry_run:
                logger.info(f"[Dry Run] Would update {file_path} tag from {old_tag} to {new_tag} and create PR.")
                result["status"] = "success"
                result["pr_url"] = "https://github.com/dry-run/pr/mock"
                results.append(result)
                continue
                
            # Create feature branch
            target_sha = github_client.get_branch_sha(branch)
            github_client.create_branch(feature_branch, target_sha)
            
            # Commit file
            commit_msg = f"chore: update image tag to {new_tag} in {file_path}"
            github_client.commit_file(
                branch=feature_branch,
                file_path=file_path,
                content=updated_yaml,
                sha=sha,
                message=commit_msg
            )
            
            # Create Pull Request
            pr_title = resolved_settings["pr_title_template"].format(new_tag=new_tag)
            pr_body = resolved_settings["pr_body_template"].format(
                new_tag=new_tag,
                old_tag=old_tag,
                triggered_by=triggered_by
            )
            labels = resolved_settings.get("pr_labels", [])
            
            pr_url = github_client.create_pull_request(
                head=feature_branch,
                base=branch,
                title=pr_title,
                body=pr_body,
                labels=labels
            )
            
            result["pr_url"] = pr_url
            result["status"] = "success"
            logger.info(f"Successfully processed target {desc}")
            
        except Exception as e:
            logger.error(f"Error processing target {desc}: {e}")
            result["status"] = "failed"
            result["error"] = str(e)
            
        results.append(result)
        
    return results


def generate_summary(results: List[Dict[str, Any]]) -> str:
    """
    Generates a Markdown summary table and writes to sync_results.md.
    """
    emoji_map = {
        "success": "✅ success",
        "skipped": "⏭️ skipped",
        "failed": "❌ failed"
    }
    
    lines = [
        "# Docker Image Tag Sync Summary",
        "",
        "| Branch | Old Tag | New Tag | Status | PR Link / Error |",
        "| --- | --- | --- | --- | --- |"
    ]
    
    for r in results:
        branch = r["branch"]
        old_tag = r["old_tag"]
        new_tag = r["new_tag"]
        status = emoji_map.get(r["status"], r["status"])
        
        if r["status"] == "success":
            pr_link = f"[PR Link]({r['pr_url']})" if r["pr_url"] else "N/A"
        elif r["status"] == "failed":
            pr_link = f"Error: {r['error']}"
        else:
            pr_link = "Already up to date"
            
        lines.append(f"| `{branch}` | `{old_tag}` | `{new_tag}` | {status} | {pr_link} |")
        
    summary = "\n".join(lines)
    
    with open("sync_results.md", "w", encoding="utf-8") as f:
        f.write(summary)
        
    print("\n--- Sync Results Summary ---")
    try:
        print(summary)
    except UnicodeEncodeError:
        ascii_summary = summary.replace("✅", "[SUCCESS]").replace("⏭️", "[SKIPPED]").replace("❌", "[FAILED]")
        print(ascii_summary)
    print("----------------------------\n")
    
    return summary


def main() -> int:
    """
    Main CLI entrypoint.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Sync Docker image tags across branches.")
    parser.add_argument("--config", default="updater-config.yml", help="Path to config file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying state")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Validate environment variables
    # For dry-run we can mock these if not present
    token = os.environ.get("GITHUB_TOKEN")
    new_tag = os.environ.get("NEW_TAG")
    triggered_by = os.environ.get("TRIGGERED_BY", "manual")
    repo = os.environ.get("REPO")

    if args.dry_run:
        # Provide defaults for dry run if not present
        if not new_tag:
            new_tag = "dry-run-tag-v1.0.0"
        if not repo:
            repo = "example-owner/example-repo"
    else:
        missing = []
        if not token:
            missing.append("GITHUB_TOKEN")
        if not new_tag:
            missing.append("NEW_TAG")
        if not repo:
            missing.append("REPO")
        if missing:
            logger.error(f"Missing required environment variables: {', '.join(missing)}")
            return 1

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return 1

    client = None
    if not args.dry_run or token:
        try:
            client = GitHubClient(token or "dummy-token", repo)
        except Exception as e:
            logger.error(f"Failed to initialize GitHub Client: {e}")
            if not args.dry_run:
                return 1

    results = sync_all_targets(
        config=config,
        github_client=client,
        new_tag=new_tag,
        triggered_by=triggered_by,
        dry_run=args.dry_run
    )

    generate_summary(results)

    # Exit with code 1 if any target failed, 0 otherwise
    if any(r["status"] == "failed" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
