#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Incremental Jira Cloud project backup.

Usage:  uv run jira_sync.py [--full] [--env PATH]

Configuration is read from .env (environment variables take precedence):
    JIRA_URL        https://your-domain.atlassian.net
    JIRA_EMAIL      account email
    JIRA_API_TOKEN  API token
    JIRA_PROJECT    project key (default: PROJ)
    OUTPUT_DIR      destination folder (default: ./backup)
    CONCURRENCY     parallel requests (default: 5)
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
PAGE_SIZE = 100


# --------------------------------------------------------------------------- config


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_path: Path) -> dict[str, str]:
    file_values = load_env_file(env_path)

    def get(name: str, default: str | None = None) -> str:
        value = os.environ.get(name) or file_values.get(name) or default
        if value is None:
            sys.exit(f"Missing required setting {name} (set it in {env_path} or the environment)")
        return value

    return {
        "url": get("JIRA_URL").rstrip("/"),
        "email": get("JIRA_EMAIL"),
        "token": get("JIRA_API_TOKEN"),
        "project": get("JIRA_PROJECT", "PROJ"),
        "output_dir": get("OUTPUT_DIR", "./backup"),
        "concurrency": get("CONCURRENCY", "5"),
    }


# --------------------------------------------------------------------------- file helpers


def dumps(data) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def write_if_changed(path: Path, data) -> bool:
    """Atomically write JSON; return True if the file is new or its content changed."""
    content = dumps(data)
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)
    return True


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "unnamed"


# --------------------------------------------------------------------------- Jira client


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, concurrency: int, transport=None):
        self.http = httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        self.semaphore = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self.http.aclose()

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            async with self.semaphore:
                try:
                    response = await self.http.request(method, url, **kwargs)
                except httpx.TransportError:
                    if attempt == MAX_ATTEMPTS:
                        raise
                    response = None
            if response is not None and response.status_code not in RETRYABLE_STATUS:
                return response
            if attempt == MAX_ATTEMPTS:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After") if response is not None else None
            await asyncio.sleep(float(retry_after) if retry_after else 2**attempt)
        raise RuntimeError("unreachable")

    async def get(self, url: str, **params):
        response = await self._send("GET", url, params=params)
        response.raise_for_status()
        return response.json()

    async def download(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        async with self.semaphore:
            async with self.http.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
        os.replace(partial, destination)

    async def paginate_offset(self, url: str, items_key: str, **params) -> list:
        """startAt/maxResults pagination (comments, worklogs, changelog, agile API)."""
        items: list = []
        start_at = 0
        while True:
            page = await self.get(url, startAt=start_at, maxResults=PAGE_SIZE, **params)
            batch = page.get(items_key, [])
            items.extend(batch)
            start_at += len(batch)
            if not batch or page.get("isLast") or ("total" in page and start_at >= page["total"]):
                return items

    async def search_issues(self, jql: str, fields: str) -> list:
        """Token-based pagination of /search/jql."""
        issues: list = []
        token = None
        while True:
            params = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
            if token:
                params["nextPageToken"] = token
            page = await self.get("/rest/api/3/search/jql", **params)
            issues.extend(page.get("issues", []))
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                return issues


# --------------------------------------------------------------------------- sync


def fingerprint(issue_fields: dict) -> str:
    """Cheap change detector built from the lightweight listing.

    `updated` covers edits and comments; links and attachments are added in case
    Jira does not bump `updated` on both ends of a link.
    """
    material = {
        "updated": issue_fields.get("updated"),
        "links": sorted(link["id"] for link in issue_fields.get("issuelinks", [])),
        "attachments": sorted(item["id"] for item in issue_fields.get("attachment", [])),
        "parent": (issue_fields.get("parent") or {}).get("key"),
    }
    return hashlib.sha1(json.dumps(material, sort_keys=True).encode()).hexdigest()


def build_link_graph(listing: list) -> dict:
    """Relationships between issues: typed links (deduplicated by link id) and hierarchy."""
    links: dict[str, dict] = {}
    hierarchy = []
    for issue in listing:
        for link in issue["fields"].get("issuelinks", []):
            if "outwardIssue" in link:
                source, target = issue["key"], link["outwardIssue"]["key"]
            else:
                source, target = link["inwardIssue"]["key"], issue["key"]
            links[link["id"]] = {
                "id": link["id"],
                "type": link["type"],
                "outward_issue": target,  # reads: source <type.outward> target
                "inward_issue": source,
            }
        parent = issue["fields"].get("parent")
        if parent:
            hierarchy.append({"child": issue["key"], "parent": parent["key"]})
    return {
        "links": sorted(links.values(), key=lambda item: int(item["id"])),
        "hierarchy": sorted(hierarchy, key=lambda item: item["child"]),
    }


async def fetch_issue_bundle(client: JiraClient, issue_id: str) -> dict:
    issue, changelog, comments, worklogs, remote_links = await asyncio.gather(
        client.get(f"/rest/api/3/issue/{issue_id}", fields="*all", expand="renderedFields,names"),
        client.paginate_offset(f"/rest/api/3/issue/{issue_id}/changelog", "values"),
        client.paginate_offset(f"/rest/api/3/issue/{issue_id}/comment", "comments"),
        client.paginate_offset(f"/rest/api/3/issue/{issue_id}/worklog", "worklogs"),
        client.get(f"/rest/api/3/issue/{issue_id}/remotelink"),
    )
    return {
        "issue": issue,
        "changelog": changelog,
        "comments": comments,
        "worklogs": worklogs,
        "remote_links": remote_links,
    }


async def sync_attachments(client: JiraClient, output: Path, issue_key: str, attachments: list) -> int:
    downloaded = 0
    for attachment in attachments:
        destination = output / "attachments" / issue_key / f"{attachment['id']}_{safe_filename(attachment['filename'])}"
        if destination.exists() and destination.stat().st_size == attachment["size"]:
            continue
        await client.download(attachment["content"], destination)
        downloaded += 1
    return downloaded


async def sync_issues(client: JiraClient, project: str, output: Path, force_full: bool) -> list:
    state_path = output / "state.json"
    state = {} if force_full or not state_path.exists() else json.loads(state_path.read_text(encoding="utf-8"))

    listing = await client.search_issues(
        f"project = {project} ORDER BY key ASC", "updated,issuelinks,attachment,parent"
    )
    remote_ids = {issue["id"] for issue in listing}
    counters = {"new": 0, "updated": 0, "unchanged": 0, "deleted": 0, "attachments": 0}

    pending = []
    for issue in listing:
        entry = state.get(issue["id"])
        current = fingerprint(issue["fields"])
        if entry and entry["fingerprint"] == current and not entry.get("deleted"):
            counters["unchanged"] += 1
        else:
            counters["updated" if entry else "new"] += 1
            pending.append((issue, current))

    async def process(issue: dict, current: str) -> None:
        bundle = await fetch_issue_bundle(client, issue["id"])
        key = bundle["issue"]["key"]
        previous_key = state.get(issue["id"], {}).get("key")
        if previous_key and previous_key != key:  # issue was moved/renamed
            (output / "issues" / f"{previous_key}.json").unlink(missing_ok=True)
        write_if_changed(output / "issues" / f"{key}.json", bundle)
        counters["attachments"] += await sync_attachments(
            client, output, key, bundle["issue"]["fields"].get("attachment", [])
        )
        state[issue["id"]] = {"key": key, "fingerprint": current}
        write_if_changed(state_path, state)  # persisted per issue: an interrupted run resumes here
        print(f"  {key}", flush=True)

    # Let every task finish so each completed issue is persisted before reporting a failure.
    outcomes = await asyncio.gather(*(process(issue, current) for issue, current in pending), return_exceptions=True)
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if failures:
        raise failures[0]

    for issue_id, entry in state.items():
        if issue_id not in remote_ids and not entry.get("deleted"):
            entry["deleted"] = True
            counters["deleted"] += 1
    write_if_changed(state_path, state)

    write_if_changed(output / "links.json", build_link_graph(listing))
    print(
        f"Issues: {len(listing)} in Jira | new {counters['new']} | updated {counters['updated']} | "
        f"unchanged {counters['unchanged']} | deleted in Jira {counters['deleted']} | "
        f"attachments downloaded {counters['attachments']}"
    )
    return listing


async def sync_model(client: JiraClient, project: str, output: Path) -> None:
    """Jira model: project definition, issue types, statuses, fields, link types, boards, sprints."""
    model = output / "model"
    project_data = await client.get(f"/rest/api/3/project/{project}", expand="description,lead,issueTypes")
    results = await asyncio.gather(
        client.get(f"/rest/api/3/project/{project}/statuses"),
        client.get(f"/rest/api/3/project/{project}/components"),
        client.get(f"/rest/api/3/project/{project}/versions"),
        client.get("/rest/api/3/field"),
        client.get("/rest/api/3/issueLinkType"),
        client.get("/rest/api/3/priority"),
        client.paginate_offset("/rest/agile/1.0/board", "values", projectKeyOrId=project),
    )
    names = ["statuses", "components", "versions", "fields", "link_types", "priorities", "boards"]
    changed = [write_if_changed(model / "project.json", project_data)]
    for name, data in zip(names, results):
        changed.append(write_if_changed(model / f"{name}.json", data))

    for board in results[-1]:
        board_id = board["id"]
        configuration = await client.get(f"/rest/agile/1.0/board/{board_id}/configuration")
        changed.append(write_if_changed(model / f"board_{board_id}_configuration.json", configuration))
        try:
            sprints = await client.paginate_offset(f"/rest/agile/1.0/board/{board_id}/sprint", "values")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 400:  # 400 = board type without sprints (kanban)
                raise
            sprints = []
        changed.append(write_if_changed(model / f"board_{board_id}_sprints.json", sprints))
    print(f"Model: {sum(changed)} of {len(changed)} files changed")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental Jira Cloud project backup")
    parser.add_argument("--full", action="store_true", help="ignore local state and refetch every issue")
    parser.add_argument("--env", default=".env", help="path to the .env file")
    arguments = parser.parse_args()

    config = load_config(Path(arguments.env))
    output = Path(config["output_dir"]) / config["project"]
    output.mkdir(parents=True, exist_ok=True)

    client = JiraClient(config["url"], config["email"], config["token"], int(config["concurrency"]))
    try:
        await sync_model(client, config["project"], output)
        await sync_issues(client, config["project"], output, arguments.full)
    except httpx.HTTPStatusError as error:
        sys.exit(f"Jira API error {error.response.status_code} on {error.request.url}: {error.response.text[:300]}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
