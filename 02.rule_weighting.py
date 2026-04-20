import os
import sys
from pathlib import Path

import yaml


RULES_SET_DIR = Path("./rules/rules_set")
PROXY_FILENAMES = ("proxy.yaml",)


class RuleWeightingError(Exception):
    """Raised when proxy rule weighting cannot complete safely."""


def load_yaml_file(file_path):
    with file_path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise RuleWeightingError(f"Invalid YAML in {file_path}: {exc}") from exc
    return data


def write_yaml_file(file_path, data):
    temp_path = file_path.with_name(f".{file_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    temp_path.replace(file_path)


def load_payload_file(file_path):
    data = load_yaml_file(file_path)
    if not isinstance(data, dict):
        raise RuleWeightingError(f"Rule file is not a mapping: {file_path}")
    payload = data.get("payload")
    if payload is None:
        raise RuleWeightingError(f"Rule file has no payload node: {file_path}")
    if not isinstance(payload, list):
        raise RuleWeightingError(f"Rule file payload is not a list: {file_path}")
    return payload


def collect_excluded_payloads(rules_dir, proxy_file):
    excluded = set()
    for filename in sorted(os.listdir(rules_dir)):
        file_path = rules_dir / filename
        if file_path == proxy_file or not file_path.is_file() or file_path.suffix != ".yaml":
            continue
        for payload in load_payload_file(file_path):
            excluded.add(payload)
    return excluded


def build_proxy_update(rules_dir, proxy_name):
    proxy_file = rules_dir / proxy_name
    if not proxy_file.exists():
        return None

    proxy_payloads = load_payload_file(proxy_file)
    excluded_payloads = collect_excluded_payloads(rules_dir, proxy_file)
    filtered_payloads = [payload for payload in proxy_payloads if payload not in excluded_payloads]

    removed_count = len(proxy_payloads) - len(filtered_payloads)
    return {
        "file": proxy_file,
        "payload": filtered_payloads,
        "original": len(proxy_payloads),
        "filtered": len(filtered_payloads),
        "removed": removed_count,
    }


def iter_rules_dirs(rules_set_dir):
    for dirpath, dirnames, _ in os.walk(rules_set_dir):
        dirnames.sort()
        yield Path(dirpath)


def main():
    if not RULES_SET_DIR.exists():
        print(f"Rule weighting failed: directory does not exist: {RULES_SET_DIR}", file=sys.stderr)
        return 1

    updates = []
    try:
        for rules_dir in iter_rules_dirs(RULES_SET_DIR):
            for proxy_name in PROXY_FILENAMES:
                update = build_proxy_update(rules_dir, proxy_name)
                if update:
                    updates.append(update)
    except RuleWeightingError as exc:
        print(f"Rule weighting failed: {exc}", file=sys.stderr)
        return 1

    if not updates:
        print("Rule weighting complete: no proxy.yaml files found")
        return 0

    for update in updates:
        write_yaml_file(update["file"], {"payload": update["payload"]})

    for result in updates:
        print(
            f"Weighted {result['file']}: "
            f"original={result['original']}, filtered={result['filtered']}, removed={result['removed']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
