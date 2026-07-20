"""
An MCP server for searching and extracting issues from a GitLab instance.

Uses the GitLab REST API v4 to search for issues across all projects (by title
and description context), retrieve full issue details, fetch comments/activity,
and read repository files. Read-only access via a token stored in the 
CODEBASE_TOKEN environment variable.

Target instance: https://codebase.helmholtz.cloud/api/v4
"""

import os
import time
import httpx
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP(name="codebase")

GITLAB_URL = "https://codebase.helmholtz.cloud"
API_BASE = f"{GITLAB_URL}/api/v4"

_session_start_time = time.time()


def _mcp_changed_since() -> bool:
    """Check if this file has been modified since the session started.

    Returns:
        True if the file was modified after session start, False otherwise.
    """
    try:
        mtime = Path(__file__).stat().st_mtime
        return mtime > _session_start_time
    except Exception:
        return False


def get_token():
    token = os.environ.get("CODEBASE_TOKEN")
    if not token:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    if line.startswith("CODEBASE_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        break
    if not token:
        raise RuntimeError("CODEBASE_TOKEN not set in environment or .env file")
    return token


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"PRIVATE-TOKEN": get_token()},
        timeout=30.0,
    )


@mcp.tool
def search_projects(query: str, per_page: int = 20) -> list[dict]:
    """Search for projects by name or path across the GitLab instance.

    Use this first to discover the project ID or path when you do not know it.
    Returns project id, name, path_with_namespace, and web_url.

    Results are sorted by last_activity_at descending by default.
    """
    with _client() as c:
        r = c.get("/projects", params={"search": query, "per_page": min(per_page, 100), "order_by": "last_activity_at", "sort": "desc"})
        r.raise_for_status()
        results = []
        for p in r.json():
            results.append({
                "id": p["id"],
                "name": p["name"],
                "path_with_namespace": p["path_with_namespace"],
                "web_url": p["web_url"],
                "description": p.get("description", ""),
            })
        return results


@mcp.tool
def search_issues(
    search: str,
    scope: str = "all",
    state: str = None,
    per_page: int = 20,
) -> list[dict]:
    """Search for issues across the entire GitLab instance (or filtered scope).

    This is the primary search tool. It searches issue TITLES and DESCRIPTIONS
    for the given text. Use this when you have a topic, title, or context but
    do NOT know the project.

    Args:
        search: Text to search for in issue title and description.
        scope: 'all' (all visible issues), 'created_by_me', or 'assigned_to_me'.
        state: Filter by 'opened', 'closed', or None (both).
        per_page: Max results (1-100).

    Returns:
        List of issues with id, iid, project_id, title, description (first 500 chars),
        state, web_url, and author info. Sorted by updated_at descending.
    """
    params = {
        "search": search,
        "scope": scope,
        "per_page": min(per_page, 100),
        "order_by": "updated_at",
        "sort": "desc",
    }
    if state:
        params["state"] = state

    with _client() as c:
        r = c.get("/issues", params=params)
        r.raise_for_status()
        results = []
        for issue in r.json():
            desc = issue.get("description") or ""
            results.append({
                "id": issue["id"],
                "iid": issue["iid"],
                "project_id": issue["project_id"],
                "title": issue["title"],
                "description_preview": desc[:500] + ("..." if len(desc) > 500 else ""),
                "state": issue["state"],
                "web_url": issue["web_url"],
                "author": issue["author"]["name"],
                "created_at": issue["created_at"],
                "updated_at": issue["updated_at"],
                "labels": issue.get("labels", []),
                "user_notes_count": issue.get("user_notes_count", 0),
            })
        return results


@mcp.tool
def get_project_issues(
    project_id: int,
    search: str = None,
    state: str = None,
    per_page: int = 20,
) -> list[dict]:
    """List issues in a specific project, optionally filtered by search text.

    Use this when you already know the project ID (from search_projects).

    Args:
        project_id: GitLab project ID (numeric).
        search: Optional text to search for in issue title and description.
        state: Filter by 'opened', 'closed', or None (both).
        per_page: Max results (1-100).

    Returns:
        List of issues with id, iid, title, description_preview, state, web_url.
        Sorted by updated_at descending.
    """
    params = {"per_page": min(per_page, 100), "order_by": "updated_at", "sort": "desc"}
    if search:
        params["search"] = search
    if state:
        params["state"] = state

    with _client() as c:
        r = c.get(f"/projects/{project_id}/issues", params=params)
        r.raise_for_status()
        results = []
        for issue in r.json():
            desc = issue.get("description") or ""
            results.append({
                "id": issue["id"],
                "iid": issue["iid"],
                "project_id": issue["project_id"],
                "title": issue["title"],
                "description_preview": desc[:500] + ("..." if len(desc) > 500 else ""),
                "state": issue["state"],
                "web_url": issue["web_url"],
                "author": issue["author"]["name"],
                "created_at": issue["created_at"],
                "updated_at": issue["updated_at"],
                "labels": issue.get("labels", []),
                "user_notes_count": issue.get("user_notes_count", 0),
            })
        return results


@mcp.tool
def get_issue(project_id: int, issue_iid: int) -> dict:
    """Get the full details of a single issue, including full description.

    Args:
        project_id: GitLab project ID (numeric).
        issue_iid: The issue IID (internal ID, shown in the web UI).

    Returns:
        Full issue object with all fields (description not truncated).
    """
    with _client() as c:
        r = c.get(f"/projects/{project_id}/issues/{issue_iid}")
        r.raise_for_status()
        issue = r.json()
        return {
            "id": issue["id"],
            "iid": issue["iid"],
            "project_id": issue["project_id"],
            "title": issue["title"],
            "description": issue.get("description") or "",
            "state": issue["state"],
            "web_url": issue["web_url"],
            "author": issue["author"]["name"],
            "assignees": [a["name"] for a in issue.get("assignees", [])],
            "labels": issue.get("labels", []),
            "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
            "created_at": issue["created_at"],
            "updated_at": issue["updated_at"],
            "closed_at": issue.get("closed_at"),
            "user_notes_count": issue.get("user_notes_count", 0),
            "upvotes": issue.get("upvotes", 0),
            "downvotes": issue.get("downvotes", 0),
            "due_date": issue.get("due_date"),
            "time_stats": issue.get("time_stats", {}),
            "task_completion_status": issue.get("task_completion_status", {}),
        }


@mcp.tool
def get_issue_notes(
    project_id: int,
    issue_iid: int,
    activity_filter: str = "all_notes",
    per_page: int = 50,
) -> list[dict]:
    """Get all comments (notes) and activity on an issue.

    This includes both user comments and system notes (status changes, etc.).

    Args:
        project_id: GitLab project ID (numeric).
        issue_iid: The issue IID (internal ID).
        activity_filter: 'all_notes', 'only_comments', or 'only_activity'.
        per_page: Max results (1-100).

    Returns:
        List of notes with id, body, author, created_at, system (boolean).
    """
    params = {
        "per_page": min(per_page, 100),
        "activity_filter": activity_filter,
        "sort": "asc",
    }

    with _client() as c:
        r = c.get(f"/projects/{project_id}/issues/{issue_iid}/notes", params=params)
        r.raise_for_status()
        notes = []
        for note in r.json():
            notes.append({
                "id": note["id"],
                "body": note["body"],
                "author": note["author"]["name"],
                "created_at": note["created_at"],
                "updated_at": note["updated_at"],
                "system": note.get("system", False),
            })
        return notes


@mcp.tool
def get_repository_file(
    project_id: int,
    file_path: str,
    ref: str = "main",
) -> dict:
    """Read the content of a file from a project's repository.

    Args:
        project_id: GitLab project ID (numeric).
        file_path: Path to the file within the repository.
        ref: Branch, tag, or commit SHA (defaults to "main").

    Returns:
        File content (decoded), encoding info, and metadata.
    """
    import urllib.parse

    with _client() as c:
        encoded_path = urllib.parse.quote(file_path, safe="")
        r = c.get(f"/projects/{project_id}/repository/files/{encoded_path}/raw", params={"ref": ref})
        r.raise_for_status()
        content = r.text
        return {
            "content": content,
            "file_path": file_path,
            "ref": ref,
            "project_id": project_id,
            "size": len(content),
        }


@mcp.tool
def list_repository_tree(
    project_id: int,
    ref: str = "main",
    path: str = "",
    recursive: bool = False,
    include_last_modified: bool = False,
) -> list[dict]:
    """List files and directories in a project's repository.

    Args:
        project_id: GitLab project ID (numeric).
        ref: Branch, tag, or commit SHA (defaults to "main").
        path: Subdirectory path to list (empty for root).
        recursive: If True, list all files recursively.
        include_last_modified: If True, fetch last modified date for each file
            (slower due to additional API calls, but enables sorting by date).

    Returns:
        List of files and directories with id, name, type, path, web_url, and optionally updated_at.
        Sorted by updated_at descending if include_last_modified is True, otherwise by path.
    """
    import urllib.parse
    import time

    params = {"ref": ref}
    if path:
        params["path"] = path
    if recursive:
        params["recursive"] = "true"

    with _client() as c:
        r = c.get(f"/projects/{project_id}/repository/tree", params=params)
        r.raise_for_status()
        results = []
        for item in r.json():
            result = {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "path": item["path"],
                "web_url": f"{GITLAB_URL}/{project_id}/-/blob/{ref}/{item['path']}",
                "updated_at": None,
            }

            # Fetch last modified date for files if requested
            if include_last_modified and item["type"] == "blob":
                encoded_path = urllib.parse.quote(item["path"], safe="")
                file_r = c.get(f"/projects/{project_id}/repository/files/{encoded_path}", params={"ref": ref})
                if file_r.status_code == 200:
                    file_info = file_r.json()
                    last_commit_id = file_info.get("last_commit_id")
                    if last_commit_id:
                        # Get commit details for the date
                        commit_r = c.get(f"/projects/{project_id}/repository/commits/{last_commit_id}")
                        if commit_r.status_code == 200:
                            commit = commit_r.json()
                            result["updated_at"] = commit.get("committed_date", "")

                # Rate limit: 2 requests per second (500ms delay)
                time.sleep(0.5)

            results.append(result)

        # Sort by updated_at descending if available, otherwise by path
        if include_last_modified:
            results.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        else:
            results.sort(key=lambda x: x.get("path", ""))

        return results


@mcp.tool
def get_current_user() -> dict:
    """Get the current authenticated user's profile.

    Returns:
        User id, username, name, email, and avatar URL.
    """
    with _client() as c:
        r = c.get("/user")
        r.raise_for_status()
        user = r.json()
        return {
            "id": user["id"],
            "username": user["username"],
            "name": user["name"],
            "email": user["email"],
            "avatar_url": user.get("avatar_url", ""),
            "state": user.get("state", ""),
        }


@mcp.tool
def get_file_last_modified(
    project_id: int,
    file_path: str,
    ref: str = "main",
) -> dict:
    """Get the last modified date of a file from the repository.

    Uses the GitLab repository files API to fetch file metadata including
    the last commit ID, then retrieves the commit details for the date.

    Args:
        project_id: GitLab project ID (numeric).
        file_path: Path to the file within the repository.
        ref: Branch, tag, or commit SHA (defaults to "main").

    Returns:
        File path, last commit SHA, author, date, and commit message.
    """
    import urllib.parse

    with _client() as c:
        # First get file metadata (includes last_commit_id)
        encoded_path = urllib.parse.quote(file_path, safe="")
        r = c.get(f"/projects/{project_id}/repository/files/{encoded_path}", params={"ref": ref})
        r.raise_for_status()
        file_info = r.json()

        last_commit_id = file_info.get("last_commit_id")
        if not last_commit_id:
            return {
                "file_path": file_path,
                "commit_sha": None,
                "author_name": None,
                "author_email": None,
                "committed_date": None,
                "message": None,
            }

        # Now get the commit details
        r = c.get(f"/projects/{project_id}/repository/commits/{last_commit_id}")
        r.raise_for_status()
        commit = r.json()

        return {
            "file_path": file_path,
            "commit_sha": commit["id"],
            "author_name": commit["author_name"],
            "author_email": commit["author_email"],
            "committed_date": commit["committed_date"],
            "message": commit["message"][:100] if commit.get("message") else None,
        }


if __name__ == "__main__":
    mcp.run()
