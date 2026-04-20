import os
import shutil
import sys
import time
from pathlib import Path

import requests
import yaml


SOURCE_DIR = Path("./source")
RULES_DIR = Path("./rules")
RULES_SET_DIR = RULES_DIR / "rules_set"
TMP_OUTPUT_DIR = Path("./.tmp_rules")
REPO_RAW_BASE = "https://raw.githubusercontent.com/darkli/research/main/rules/rules_set"
REQUEST_HEADERS = {"User-Agent": "darkli-research-rule-updater"}
REQUEST_TIMEOUT = (10, 300)
MAX_DOWNLOAD_ATTEMPTS = 3
MANUAL_RULE_PATHS = {
    "private_direct.yaml",
    "proxy_selected.yaml",
    "uk_vowifi.yaml",
}


class RuleUpdateError(Exception):
    """Raised when rule generation cannot complete safely."""


def load_yaml_file(file_path):
    with file_path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise RuleUpdateError(f"Invalid YAML in {file_path}: {exc}") from exc
    return data


def write_yaml_file(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def validate_payload(data, url):
    if not isinstance(data, dict):
        raise RuleUpdateError(f"Downloaded YAML is not a mapping: {url}")
    if "payload" not in data:
        raise RuleUpdateError(f"Downloaded YAML has no payload node: {url}")
    payload = data["payload"]
    if not isinstance(payload, list):
        raise RuleUpdateError(f"Downloaded payload is not a list: {url}")
    if not payload:
        raise RuleUpdateError(f"Downloaded payload is empty: {url}")
    return payload


def download_payload(url):
    last_error = None
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            if attempt > 1:
                print(f"Download recovered for {url} on attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}")
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                raise RuleUpdateError(
                    f"Failed to download {url} after {MAX_DOWNLOAD_ATTEMPTS} attempts: {exc}"
                ) from exc
            print(
                f"Download failed for {url} on attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}; "
                f"retrying attempt {attempt + 1}/{MAX_DOWNLOAD_ATTEMPTS}: {exc}"
            )
            time.sleep(attempt)
    else:
        raise RuleUpdateError(f"Failed to download {url}: {last_error}")

    try:
        data = yaml.safe_load(response.text)
    except yaml.YAMLError as exc:
        raise RuleUpdateError(f"Invalid YAML downloaded from {url}: {exc}") from exc

    return validate_payload(data, url)


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_index_name(subpath):
    if not subpath:
        return "rules.yaml"
    return f"rules_{subpath.replace(os.sep, '_')}.yaml"


def build_rule_entry(node_name, node_data, subpath):
    if subpath:
        yaml_path = f"./rules_set/{subpath}/{node_name}.yaml"
        yaml_url = f"{REPO_RAW_BASE}/{subpath}/{node_name}.yaml"
    else:
        yaml_path = f"./rules_set/{node_name}.yaml"
        yaml_url = f"{REPO_RAW_BASE}/{node_name}.yaml"

    return {
        "type": node_data.get("type"),
        "behavior": node_data.get("behavior"),
        "path": yaml_path,
        "interval": node_data.get("interval", 86400),
        "url": yaml_url,
    }


def validate_source_node(file_path, node_name, node_data):
    if not isinstance(node_data, dict):
        raise RuleUpdateError(f"Node {node_name} in {file_path} is not a mapping")
    urls = node_data.get("urls")
    if not isinstance(urls, list) or not urls:
        raise RuleUpdateError(f"Node {node_name} in {file_path} must define a non-empty urls list")
    for url in urls:
        if not isinstance(url, str) or not url:
            raise RuleUpdateError(f"Node {node_name} in {file_path} contains an invalid URL: {url!r}")
    return urls


def collect_generated_rules(source_dir):
    if not source_dir.exists():
        raise RuleUpdateError(f"Source directory does not exist: {source_dir}")

    generated_rules = {}
    generated_indexes = {}
    stats = {
        "nodes": 0,
        "source_payloads": 0,
        "deduped_payloads": 0,
        "removed_duplicates": 0,
    }

    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        filenames = sorted(filenames)
        dirpath = Path(dirpath)
        rel_dir = dirpath.relative_to(source_dir)
        subpath = "" if rel_dir == Path(".") else rel_dir.as_posix()
        index_name = normalize_index_name(subpath)
        index_data = {}

        for filename in filenames:
            if not filename.endswith(".yaml"):
                continue

            file_path = dirpath / filename
            source_data = load_yaml_file(file_path)
            if not isinstance(source_data, dict) or not source_data:
                raise RuleUpdateError(f"Source file must contain at least one mapping node: {file_path}")

            for node_name, node_data in source_data.items():
                urls = validate_source_node(file_path, node_name, node_data)
                downloaded_payloads = []
                for url in urls:
                    downloaded_payloads.extend(download_payload(url))

                merged_payloads = dedupe_keep_order(downloaded_payloads)
                rule_rel_path = Path(subpath) / f"{node_name}.yaml" if subpath else Path(f"{node_name}.yaml")
                generated_rules[rule_rel_path.as_posix()] = {"payload": merged_payloads}
                index_data[node_name] = build_rule_entry(node_name, node_data, subpath)

                source_count = len(downloaded_payloads)
                deduped_count = len(merged_payloads)
                stats["nodes"] += 1
                stats["source_payloads"] += source_count
                stats["deduped_payloads"] += deduped_count
                stats["removed_duplicates"] += source_count - deduped_count
                print(
                    f"Processed {file_path}::{node_name}: "
                    f"source={source_count}, deduped={deduped_count}, duplicates={source_count - deduped_count}"
                )

        generated_indexes[index_name] = index_data

    if stats["nodes"] == 0:
        raise RuleUpdateError(f"No rule nodes found in source directory: {source_dir}")

    return generated_rules, generated_indexes, stats


def existing_auto_rule_paths():
    paths = set()
    for index_name in ("rules.yaml", "rules_no_resolve.yaml"):
        index_path = RULES_DIR / index_name
        if not index_path.exists():
            continue
        data = load_yaml_file(index_path) or {}
        if not isinstance(data, dict):
            raise RuleUpdateError(f"Index file is not a mapping: {index_path}")
        for node_name, node_data in data.items():
            if not isinstance(node_data, dict):
                continue
            local_path = node_data.get("path")
            if not isinstance(local_path, str):
                continue
            normalized = local_path.removeprefix("./")
            if normalized.startswith("rules_set/"):
                rel_path = Path(normalized).relative_to("rules_set").as_posix()
                if rel_path not in MANUAL_RULE_PATHS:
                    paths.add(rel_path)
    return paths


def replace_file(source_path, target_path):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.tmp")
    shutil.copy2(source_path, temp_path)
    temp_path.replace(target_path)


def write_tmp_output(tmp_dir, generated_rules, generated_indexes):
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_rules_set_dir = tmp_dir / "rules_set"

    for rel_path, data in generated_rules.items():
        write_yaml_file(tmp_rules_set_dir / rel_path, data)
    for index_name, data in generated_indexes.items():
        write_yaml_file(tmp_dir / index_name, data)


def publish_output(tmp_dir, generated_rules, generated_indexes):
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    RULES_SET_DIR.mkdir(parents=True, exist_ok=True)

    auto_paths = existing_auto_rule_paths()
    auto_paths.update(generated_rules.keys())

    for rel_path in sorted(auto_paths):
        target_path = RULES_SET_DIR / rel_path
        source_path = tmp_dir / "rules_set" / rel_path
        if source_path.exists():
            replace_file(source_path, target_path)
        elif target_path.exists():
            target_path.unlink()

    for index_name in sorted(generated_indexes):
        replace_file(tmp_dir / index_name, RULES_DIR / index_name)


def main():
    try:
        generated_rules, generated_indexes, stats = collect_generated_rules(SOURCE_DIR)
        write_tmp_output(TMP_OUTPUT_DIR, generated_rules, generated_indexes)
        publish_output(TMP_OUTPUT_DIR, generated_rules, generated_indexes)
        print(
            "Rule update complete: "
            f"nodes={stats['nodes']}, files={len(generated_rules)}, "
            f"source_payloads={stats['source_payloads']}, "
            f"deduped_payloads={stats['deduped_payloads']}, "
            f"removed_duplicates={stats['removed_duplicates']}"
        )
    except RuleUpdateError as exc:
        print(f"Rule update failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if TMP_OUTPUT_DIR.exists():
            shutil.rmtree(TMP_OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
