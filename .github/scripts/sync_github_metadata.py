"""Preview or apply additive GitHub label and topic configuration using gh."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the printed changes")
    args = parser.parse_args()
    config_dir = Path(__file__).resolve().parent.parent
    # JSON is a YAML subset: this representation needs no third-party YAML parser.
    labels = json.loads((config_dir / "labels.yml").read_text(encoding="utf-8"))
    metadata = json.loads((config_dir / "topics.json").read_text(encoding="utf-8"))
    repo = metadata["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository name")
    names = set()
    for label in labels:
        name = label["name"]
        if not name or len(name) > 50 or name.casefold() in names:
            raise ValueError(f"Invalid or duplicate label: {name}")
        names.add(name.casefold())
        if not re.fullmatch(r"[0-9a-fA-F]{6}", label["color"]):
            raise ValueError(f"Invalid color: {name}")
        if len(label["description"]) > 100:
            raise ValueError(f"Description too long: {name}")
    topics = metadata["topics"]
    if len(topics) != len(set(topics)) or len(topics) > 20:
        raise ValueError("Invalid topic count or duplicate topics")
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic) for topic in topics):
        raise ValueError("Invalid topic name")
    current = {
        item["name"].casefold(): item
        for item in json.loads(gh("label", "list", "--repo", repo, "--limit", "1000",
                                  "--json", "name,color,description"))
    }
    info = json.loads(gh("repo", "view", repo, "--json", "repositoryTopics"))
    existing_topics = {item["name"] for item in info["repositoryTopics"] or []}
    if len(existing_topics | set(topics)) > 20:
        raise ValueError("Combined topics exceed GitHub's limit; review manually")
    changes = 0
    for label in labels:
        existing = current.get(label["name"].casefold())
        if existing and all(
            (existing.get(key, "").lower() == label[key].lower())
            if key == "color"
            else (existing.get(key, "") == label[key])
            for key in ("color", "description")
        ):
            continue
        changes += 1
        print(f"{'Update' if existing else 'Create'} label: {repo} {label['name']}")
        if args.apply:
            gh("label", "create", label["name"], "--repo", repo, "--force",
               "--color", label["color"], "--description", label["description"])
    missing_topics = sorted(set(topics) - existing_topics)
    for topic in missing_topics:
        print(f"Add topic: {repo} {topic}")
    if args.apply and missing_topics:
        topic_args = [arg for topic in missing_topics for arg in ("--add-topic", topic)]
        gh("repo", "edit", repo, *topic_args)
    print(f"{changes} label change(s), {len(missing_topics)} topic addition(s); "
          f"{'applied' if args.apply else 'dry run (use --apply to write)'}. "
          "Unmanaged labels and topics are preserved.")


if __name__ == "__main__":
    main()
