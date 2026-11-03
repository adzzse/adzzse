"""Public-only adaptation of nizarelidrysy/nizarelidrysy's cached profile updater.

Reference: https://github.com/nizarelidrysy/nizarelidrysy/blob/main/today.py
Keep the GraphQL/history/cache structure, updating existing SVGs only for commit changes.
"""

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


REPOSITORIES = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    id followers { totalCount }
    repositories(first: 100, after: $cursor, privacy: PUBLIC,
      ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      nodes {
        id nameWithOwner isPrivate owner { login } stargazerCount
        defaultBranchRef { target { oid } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

HISTORY = """
query($owner: String!, $name: String!, $head: GitObjectID!, $author: ID!,
      $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    object(oid: $head) {
      ... on Commit {
        history(first: $first, after: $cursor, author: {id: $author}) {
          totalCount nodes { oid additions deletions }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""


def graphql(document, variables):
    """Retry the transient gateway errors seen with commit history, not auth failures."""
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Set ACCESS_TOKEN or GITHUB_TOKEN before running today.py")
    request = Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": document, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "github-profile-metrics"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
            break
        except (HTTPError, RemoteDisconnected) as error:
            if (isinstance(error, HTTPError) and error.code not in (502, 503, 504)) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if result.get("errors"):
        raise RuntimeError("GitHub GraphQL: " + "; ".join(e["message"] for e in result["errors"]))
    return result["data"]


def get_repositories(user_name, query):
    """Enumerate public owned/collaborated repos before asking for their history."""
    repositories, cursor = [], None
    while True:
        user = query(REPOSITORIES, {"login": user_name, "cursor": cursor})["user"]
        if user is None:
            raise ValueError(f"GitHub user not found: {user_name}")
        connection = user["repositories"]
        repositories.extend(repo for repo in connection["nodes"] if not repo["isPrivate"])
        if not connection["pageInfo"]["hasNextPage"]:
            return user["id"], user["followers"]["totalCount"], repositories
        next_cursor = connection["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise ValueError("Repository pagination did not advance")
        cursor = next_cursor


def commit_signature(repositories):
    """Other authors, uptime, dates and social counts must not trigger generated commits."""
    return {key: (repo["count"], repo["latest"]) for key, repo in repositories.items() if repo["count"]}


def collect_stats(user_name, old_cache, query):
    """Probe authored history cheaply and reuse cached LOC for unchanged repositories."""
    author, followers, repositories = get_repositories(user_name, query)
    previous = old_cache.get("repositories", {}) if old_cache.get("user_name") == user_name else {}
    current = {}
    for index, repo in enumerate(repositories, 1):
        print(f"Checking public repository {index}/{len(repositories)}: {repo['nameWithOwner']}", flush=True)
        key = sha256(repo["id"].encode()).hexdigest()
        entry = {"count": 0, "latest": None, "additions": 0, "deletions": 0}
        current[key] = entry
        if repo["defaultBranchRef"] is None:
            continue
        owner, name = repo["nameWithOwner"].split("/", 1)
        variables = {"owner": owner, "name": name, "author": author, "first": 1,
                     "head": repo["defaultBranchRef"]["target"]["oid"], "cursor": None}
        history = query(HISTORY, variables)["repository"]["object"]["history"]
        count = history["totalCount"]
        latest = history["nodes"][0]["oid"] if history["nodes"] else None
        cached = previous.get(key)
        if cached and (cached["count"], cached["latest"]) == (count, latest):
            current[key] = cached
            continue
        entry.update(count=count, latest=latest)
        seen = set()
        while True:
            for commit in history["nodes"]:
                if commit["oid"] in seen:
                    raise ValueError("Duplicate commit in paginated history")
                seen.add(commit["oid"])
                entry["additions"] += commit["additions"]
                entry["deletions"] += commit["deletions"]
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]
            if not cursor or cursor == variables["cursor"]:
                raise ValueError("Commit pagination did not advance")
            variables = {**variables, "first": 100, "cursor": cursor}
            history = query(HISTORY, variables)["repository"]["object"]["history"]
        if len(seen) != count:
            raise ValueError("Incomplete authored commit history")
    if old_cache.get("user_name") == user_name and commit_signature(previous) == commit_signature(current):
        return None
    owned = [repo for repo in repositories if repo["owner"]["login"].lower() == user_name.lower()]
    stats = {"repos": len(owned), "contributed": len(repositories),
             "stars": sum(repo["stargazerCount"] for repo in owned), "followers": followers,
             "commits": sum(repo["count"] for repo in current.values()),
             "additions": sum(repo["additions"] for repo in current.values()),
             "deletions": sum(repo["deletions"] for repo in current.values())}
    stats["loc"] = stats["additions"] - stats["deletions"]
    return {"user_name": user_name, "repositories": current, "stats": stats}


def field(key, value, width=66, prefix=". "):
    """Preserve the accepted fixed-width dot leaders as numeric values grow."""
    label = [("leader", prefix), ("key", key), ("accent", ":")]
    dots = width - sum(len(text) for _, text in label + value) - 2
    if dots < 0:
        raise ValueError(f"Metric is too wide for the profile: {key}")
    return label + [("leader", " " + "." * dots + " ")] + value


def replace_element(svg, element_id, contents):
    """Replace only targeted element contents, retaining the portrait bytes and comments."""
    pattern = rf'(<(?P<tag>text|metadata)\b[^>]*\bid="{re.escape(element_id)}"[^>]*>).*?(</(?P=tag)>)'
    output, count = re.subn(pattern, lambda m: m[1] + contents + m[3], svg, flags=re.S)
    if count != 1:
        raise ValueError(f"Expected one SVG element: {element_id}")
    return output


def svg_overwrite(svg, stats, date):
    """Update the reference-style metrics without recalculating or writing uptime."""
    repos = [("value", f'{stats["repos"]} {{'), ("key", "Contributed"),
             ("accent", ": "), ("value", f'{stats["contributed"]}' + "}")]
    rows = {
        "repo_data": field("Repos", repos, 39) + [("accent", " | ")] + field("Stars", [("value", str(stats["stars"]))], 24, ""),
        "commit_data": field("Commits", [("value", f'{stats["commits"]:,}')], 39) + [("accent", " | ")] + field("Followers", [("value", str(stats["followers"]))], 24, ""),
        "loc_data": field("Lines of Code on GitHub", [("value", f'{stats["loc"]:,} ('),
                    ("positive", f'{stats["additions"]:,}++'), ("accent", ", "),
                    ("negative", f'{stats["deletions"]:,}--'), ("accent", ")")]),
    }
    for element_id, segments in rows.items():
        contents = "".join(f'<tspan class="{style}">{escape(text)}</tspan>' for style, text in segments)
        svg = replace_element(svg, element_id, contents)
    svg = replace_element(svg, "updated_at", f"PUBLIC STATS / {date}")
    svg = replace_element(svg, "stats-snapshot", escape(json.dumps({**stats, "updated_at": date, "scope": "public"})))
    ET.fromstring(svg)
    return svg


def update_readme(text, stats, date):
    """Keep the accessible text equivalent synchronized with the image statistics."""
    newline = "\r\n" if "\r\n" in text else "\n"
    item = (f"    <li>Public GitHub stats ({date}): {stats['repos']} repos, "
            f"{stats['contributed']} owned/contributed repos, {stats['stars']} stars, "
            f"{stats['followers']} followers. Commits: {stats['commits']:,}. "
            f"Lines of code: {stats['loc']:,} ({stats['additions']:,}++, {stats['deletions']:,}--).</li>")
    output, count = re.subn(r"(?<=<!-- metrics:start -->).*?(?=<!-- metrics:end -->)",
                            lambda _: newline + item + newline + "    ", text, flags=re.S)
    if count != 1:
        raise ValueError("Expected one README metrics section")
    return output


def update_profile(root, user_name, query=graphql, date=None):
    """Prepare every output before touching files so API/template failures preserve the snapshot."""
    cache_path = root / "cache/metrics.json"
    old_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    snapshot = collect_stats(user_name, old_cache, query)
    if snapshot is None:
        return False
    date = date or datetime.now(timezone.utc).date().isoformat()
    snapshot["updated_at"] = date
    stats = snapshot["stats"]
    outputs = {}
    for name in ("assets/profile-dark.svg", "assets/profile-light.svg"):
        path = root / name
        outputs[path] = svg_overwrite(path.read_bytes().decode("utf-8"), stats, date)
    readme = root / "README.md"
    outputs[readme] = update_readme(readme.read_bytes().decode("utf-8"), stats, date)
    outputs[cache_path] = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    for path, text in outputs.items():
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(text.encode("utf-8"))
        temporary.replace(path)
    return True


if __name__ == "__main__":
    changed = update_profile(Path(__file__).resolve().parent, os.environ.get("USER_NAME", "adzzse"))
    print("Profile metrics updated" if changed else "No authored commit changes; files left untouched")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"changed={str(changed).lower()}\n")
