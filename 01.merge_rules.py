import concurrent.futures
import ipaddress
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

try:
    from yaml import CSafeDumper as SafeDumper
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeDumper, SafeLoader


SOURCE_DIR = Path("./source")
RULES_DIR = Path("./rules")
RULES_SET_DIR = RULES_DIR / "rules_set"
TMP_OUTPUT_DIR = Path("./.tmp_rules")
DLC_REPO_DIR = Path("./.cache/domain-list-community")
REPO_RAW_BASE = "https://raw.githubusercontent.com/darkli/research/main/rules/rules_set"
DLC_GIT_REPO = "git@github.com:v2fly/domain-list-community.git"
REQUEST_HEADERS = {"User-Agent": "darkli-research-rule-updater"}
REQUEST_TIMEOUT = (10, 300)
MAX_DOWNLOAD_ATTEMPTS = 6
FALLBACK_AFTER_ATTEMPTS = 2
DEFAULT_DOWNLOAD_WORKERS = 8
DLC_DATA_PATH_MARKER = "/data/"
SUPPORTED_SOURCE_FORMATS = {"auto", "clash", "dlc"}
RULE_TYPE_ORDER = {
    "DOMAIN": 10,
    "DOMAIN-SUFFIX": 20,
    "DOMAIN-KEYWORD": 30,
    "DOMAIN-REGEX": 40,
    "IP-CIDR": 50,
    "IP-CIDR6": 60,
    "IP-ASN": 70,
    "GEOIP": 80,
    "PROCESS-NAME": 90,
}
MANUAL_RULE_PATHS = {
    "private_direct.yaml",
    "proxy_selected.yaml",
    "uk_vowifi.yaml",
}
THREAD_LOCAL = threading.local()


class RuleUpdateError(Exception):
    """Raised when rule generation cannot complete safely."""


def yaml_load(stream):
    return yaml.load(stream, Loader=SafeLoader)


def yaml_dump(data, stream):
    yaml.dump(data, stream, Dumper=SafeDumper, allow_unicode=True, sort_keys=False, default_flow_style=False)


def get_download_workers():
    raw_value = os.environ.get("RULE_DOWNLOAD_WORKERS", str(DEFAULT_DOWNLOAD_WORKERS))
    try:
        workers = int(raw_value)
    except ValueError as exc:
        raise RuleUpdateError(f"RULE_DOWNLOAD_WORKERS must be an integer: {raw_value!r}") from exc
    return max(1, workers)


def get_thread_session():
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(REQUEST_HEADERS)
        THREAD_LOCAL.session = session
    return session


def load_yaml_file(file_path):
    with file_path.open("r", encoding="utf-8") as f:
        try:
            data = yaml_load(f)
        except yaml.YAMLError as exc:
            raise RuleUpdateError(f"Invalid YAML in {file_path}: {exc}") from exc
    return data


def write_yaml_file(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as f:
        yaml_dump(data, f)


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


def get_url_text(url, session=None):
    session = session or get_thread_session()
    last_error = None
    fallback_error = None
    fallback_attempted = False
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            if attempt > 1:
                print(f"Download recovered for {url} on attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}", flush=True)
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= FALLBACK_AFTER_ATTEMPTS and not fallback_attempted:
                fallback_attempted = True
                fallback_text, fallback_error = get_github_raw_fallback_text(url, exc, session)
                if fallback_text is not None:
                    return fallback_text
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                if fallback_error is not None:
                    print(f"Last fallback error for {url}: {fallback_error}", flush=True)
                raise RuleUpdateError(
                    f"Failed to download {url} after {MAX_DOWNLOAD_ATTEMPTS} attempts: {exc}"
                ) from exc
            print(
                f"Download failed for {url} on attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}; "
                f"retrying attempt {attempt + 1}/{MAX_DOWNLOAD_ATTEMPTS}: {exc}",
                flush=True,
            )
            time.sleep(min(attempt * 2, 10))
    else:
        raise RuleUpdateError(f"Failed to download {url}: {last_error}")

    return response.text


def get_github_raw_fallback_text(url, original_error, session=None):
    parsed = urlparse(url)
    if parsed.netloc != "raw.githubusercontent.com":
        return None, None

    parts = parsed.path.lstrip("/").split("/", 3)
    if len(parts) != 4:
        return None, None

    owner, repo, ref, file_path = parts
    fallback_url = f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{file_path}"
    session = session or get_thread_session()
    try:
        response = session.get(fallback_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(
            f"Fallback download failed for {url} via {fallback_url}: {exc}; original error: {original_error}",
            flush=True,
        )
        return None, exc

    print(f"Download recovered for {url} via fallback {fallback_url}", flush=True)
    return response.text, None


def is_dlc_data_url(url):
    parsed = urlparse(url)
    return DLC_DATA_PATH_MARKER in parsed.path


def infer_dlc_name(url):
    parsed = urlparse(url)
    if DLC_DATA_PATH_MARKER not in parsed.path:
        return None
    return parsed.path.rsplit(DLC_DATA_PATH_MARKER, 1)[1].strip("/")


def run_command(command, cwd=None):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise RuleUpdateError(f"Command failed: {' '.join(command)}\n{stderr.strip()}") from exc


def ensure_dlc_repo(repo_dir):
    if repo_dir.exists():
        if not (repo_dir / ".git").exists():
            raise RuleUpdateError(f"DLC cache path exists but is not a git repository: {repo_dir}")
        run_command(["git", "fetch", "--depth=1", "origin", "master"], cwd=repo_dir)
        run_command(["git", "checkout", "-q", "FETCH_HEAD"], cwd=repo_dir)
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run_command(["git", "clone", "--depth=1", DLC_GIT_REPO, str(repo_dir)])


def ensure_dlc_repo_once(repo_dir, dlc_state):
    if dlc_state["repo_ready"]:
        return
    ensure_dlc_repo(repo_dir)
    dlc_state["repo_ready"] = True


def read_dlc_data_file(dlc_name, repo_dir):
    file_path = repo_dir / "data" / dlc_name
    if not file_path.is_file():
        raise RuleUpdateError(f"DLC data file does not exist in local cache: {dlc_name}")
    return file_path.read_text(encoding="utf-8")


def strip_dlc_comment(line):
    for index, char in enumerate(line):
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].strip()
    return line.strip()


def parse_dlc_rule_token(token, url, line_number):
    if ":" in token:
        rule_type, value = token.split(":", 1)
        if rule_type not in {"domain", "full", "keyword", "regexp"}:
            raise RuleUpdateError(f"Unsupported DLC rule type {rule_type!r} in {url}:{line_number}")
    else:
        rule_type = "domain"
        value = token

    if not value:
        raise RuleUpdateError(f"Empty DLC rule value in {url}:{line_number}")
    return rule_type, value


def parse_dlc_text(text, url):
    includes = []
    rules = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = strip_dlc_comment(raw_line)
        if not line:
            continue

        parts = line.split()
        token = parts[0]
        attributes = tuple(part[1:] for part in parts[1:] if part.startswith("@"))

        if token.startswith("include:"):
            include_name = token.split(":", 1)[1]
            if not include_name:
                raise RuleUpdateError(f"Empty DLC include in {url}:{line_number}")
            includes.append((include_name, attributes))
            continue

        rule_type, value = parse_dlc_rule_token(token, url, line_number)
        rules.append((rule_type, value, attributes))

    if not includes and not rules:
        raise RuleUpdateError(f"DLC data file has no rules: {url}")
    return includes, rules


def dlc_rule_matches(rule_attributes, include_filters):
    attrs = set(rule_attributes)
    for include_filter in include_filters:
        if include_filter.startswith("-"):
            if include_filter[1:] in attrs:
                return False
        elif include_filter not in attrs:
            return False
    return True


def resolve_dlc_rules(dlc_name, dlc_cache, repo_dir, include_stack=None, text=None):
    include_stack = include_stack or []
    if dlc_name in include_stack:
        chain = " -> ".join([*include_stack, dlc_name])
        raise RuleUpdateError(f"Cyclic DLC include detected: {chain}")
    if dlc_name in dlc_cache:
        return dlc_cache[dlc_name]

    if text is None:
        text = read_dlc_data_file(dlc_name, repo_dir)
    includes, rules = parse_dlc_text(text, f"domain-list-community/data/{dlc_name}")
    resolved_rules = list(rules)
    next_stack = [*include_stack, dlc_name]
    for include_name, include_filters in includes:
        for rule in resolve_dlc_rules(include_name, dlc_cache, repo_dir, next_stack):
            if dlc_rule_matches(rule[2], include_filters):
                resolved_rules.append(rule)

    dlc_cache[dlc_name] = tuple(resolved_rules)
    return dlc_cache[dlc_name]


def convert_dlc_rules_to_clash(rules, url):
    payload = []
    converted_regexps = 0
    for rule_type, value, _attributes in rules:
        if rule_type == "domain":
            payload.append(f"DOMAIN-SUFFIX,{value}")
        elif rule_type == "full":
            payload.append(f"DOMAIN,{value}")
        elif rule_type == "keyword":
            payload.append(f"DOMAIN-KEYWORD,{value}")
        elif rule_type == "regexp":
            payload.append(f"DOMAIN-REGEX,{value}")
            converted_regexps += 1
        else:
            raise RuleUpdateError(f"Unsupported resolved DLC rule type {rule_type!r} from {url}")

    if converted_regexps:
        print(
            f"Converted {converted_regexps} regexp rules from DLC source {url} to DOMAIN-REGEX",
            flush=True,
        )
    if not payload:
        raise RuleUpdateError(f"DLC source produced no convertible Clash rules: {url}")
    return payload


def looks_like_dlc_text(text):
    meaningful_lines = 0
    for raw_line in text.splitlines():
        line = strip_dlc_comment(raw_line)
        if not line:
            continue
        meaningful_lines += 1
        token = line.split()[0]
        if (
            token.startswith("include:")
            or token.startswith(("domain:", "full:", "keyword:", "regexp:"))
            or "." in token
        ):
            continue
        return False
    return meaningful_lines > 0


def download_dlc_payload(source_item, dlc_cache, repo_dir, dlc_state, text=None):
    dlc_name = source_item.get("name") or infer_dlc_name(source_item["url"])
    if not dlc_name:
        raise RuleUpdateError(
            f"DLC source must either point to a /data/ URL or define a name: {source_item['url']}"
        )
    if text is None:
        ensure_dlc_repo_once(repo_dir, dlc_state)
        rules = resolve_dlc_rules(dlc_name, dlc_cache, repo_dir)
    else:
        ensure_dlc_repo_once(repo_dir, dlc_state)
        rules = resolve_dlc_rules(dlc_name, dlc_cache, repo_dir, text=text)
    return convert_dlc_rules_to_clash(rules, source_item["url"])


def download_payload(source_item, dlc_cache, repo_dir, dlc_state, downloaded_texts=None):
    url = source_item["url"]
    source_format = source_item["format"]

    if source_format == "dlc":
        return download_dlc_payload(source_item, dlc_cache, repo_dir, dlc_state)

    if downloaded_texts is not None and url in downloaded_texts:
        text = downloaded_texts[url]
    else:
        text = get_url_text(url)
    if source_format == "auto" and is_dlc_data_url(url):
        return download_dlc_payload(source_item, dlc_cache, repo_dir, dlc_state, text=text)

    try:
        data = yaml_load(text)
    except yaml.YAMLError as exc:
        if source_format == "auto" and looks_like_dlc_text(text):
            return download_dlc_payload(source_item, dlc_cache, repo_dir, dlc_state, text=text)
        raise RuleUpdateError(f"Invalid YAML downloaded from {url}: {exc}") from exc

    if isinstance(data, dict) and "payload" in data:
        return validate_payload(data, url)
    if source_format == "clash":
        raise RuleUpdateError(f"Downloaded Clash source has no payload node: {url}")
    if source_format == "auto" and looks_like_dlc_text(text):
        return download_dlc_payload(source_item, dlc_cache, repo_dir, dlc_state, text=text)

    raise RuleUpdateError(f"Downloaded content is neither Clash YAML payload nor supported DLC data: {url}")


def build_source_plan(source_dir):
    if not source_dir.exists():
        raise RuleUpdateError(f"Source directory does not exist: {source_dir}")

    entries = []
    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        filenames = sorted(filenames)
        dirpath = Path(dirpath)
        rel_dir = dirpath.relative_to(source_dir)
        subpath = "" if rel_dir == Path(".") else rel_dir.as_posix()
        index_name = normalize_index_name(subpath)

        for filename in filenames:
            if not filename.endswith(".yaml"):
                continue

            file_path = dirpath / filename
            source_data = load_yaml_file(file_path)
            if not isinstance(source_data, dict) or not source_data:
                raise RuleUpdateError(f"Source file must contain at least one mapping node: {file_path}")

            for node_name, node_data in source_data.items():
                entries.append(
                    {
                        "file_path": file_path,
                        "node_name": node_name,
                        "node_data": node_data,
                        "source_items": validate_source_node(file_path, node_name, node_data),
                        "subpath": subpath,
                        "index_name": index_name,
                    }
                )

    if not entries:
        raise RuleUpdateError(f"No rule nodes found in source directory: {source_dir}")
    return entries


def needs_text_download(source_item):
    return source_item["format"] != "dlc"


def collect_download_urls(source_entries):
    urls = []
    seen = set()
    for entry in source_entries:
        for source_item in entry["source_items"]:
            if not needs_text_download(source_item):
                continue
            url = source_item["url"]
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def download_url_task(url):
    return get_url_text(url, get_thread_session())


def download_all_texts(urls):
    if not urls:
        return {}, 0.0

    started_at = time.perf_counter()
    workers = min(get_download_workers(), len(urls))
    downloaded = {}
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_url = {executor.submit(download_url_task, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                downloaded[url] = future.result()
            except Exception as exc:
                failures.append((url, exc))

    elapsed = time.perf_counter() - started_at
    if failures:
        lines = [f"Failed to download {len(failures)} source URL(s):"]
        for url, exc in failures[:20]:
            lines.append(f"- {url}: {exc}")
        if len(failures) > 20:
            lines.append(f"- ... {len(failures) - 20} more")
        raise RuleUpdateError("\n".join(lines))

    print(f"Downloaded {len(downloaded)} unique URL(s) with {workers} worker(s) in {elapsed:.2f}s", flush=True)
    return downloaded, elapsed


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_domain_rule(item):
    if not isinstance(item, str):
        return None
    parts = item.split(",", 2)
    if len(parts) < 2:
        return None
    rule_type = parts[0]
    if rule_type not in {"DOMAIN", "DOMAIN-SUFFIX"}:
        return None
    domain = parts[1].strip().lower().rstrip(".")
    if not domain:
        return None
    return rule_type, domain


def covered_by_suffix(domain, suffixes, include_self):
    labels = domain.split(".")
    start_index = 0 if include_self else 1
    for index in range(start_index, len(labels)):
        suffix = ".".join(labels[index:])
        if suffix in suffixes:
            return True
    return False


def remove_covered_domain_rules(items):
    suffixes = {
        parsed[1]
        for parsed in (parse_domain_rule(item) for item in items)
        if parsed and parsed[0] == "DOMAIN-SUFFIX"
    }
    if not suffixes:
        return items, 0

    result = []
    removed_count = 0
    for item in items:
        parsed = parse_domain_rule(item)
        if not parsed:
            result.append(item)
            continue

        rule_type, domain = parsed
        if rule_type == "DOMAIN" and covered_by_suffix(domain, suffixes, include_self=True):
            removed_count += 1
            continue
        if rule_type == "DOMAIN-SUFFIX" and covered_by_suffix(domain, suffixes, include_self=False):
            removed_count += 1
            continue
        result.append(item)

    return result, removed_count


def parse_ip_rule(item):
    if not isinstance(item, str):
        return None
    parts = item.split(",")
    if len(parts) < 2 or parts[0] not in {"IP-CIDR", "IP-CIDR6"}:
        return None

    try:
        network = ipaddress.ip_network(parts[1].strip(), strict=False)
    except ValueError:
        return None

    options = tuple(part.strip().lower() for part in parts[2:])
    return parts[0], network, options


def remove_covered_ip_rules(items):
    parsed_items = []
    for item in items:
        parsed = parse_ip_rule(item)
        if parsed:
            parsed_items.append((item, *parsed))

    if not parsed_items:
        return items, 0

    covered_items = set()
    grouped_networks = {}
    for item, rule_type, network, options in parsed_items:
        grouped_networks.setdefault((rule_type, network.version, options), []).append((item, network))

    for grouped_items in grouped_networks.values():
        seen_networks = set()
        for item, network in sorted(
            grouped_items,
            key=lambda item_and_network: (
                item_and_network[1].prefixlen,
                int(item_and_network[1].network_address),
                int(item_and_network[1].broadcast_address),
                item_and_network[0],
            ),
        ):
            for prefixlen in range(network.prefixlen):
                if network.supernet(new_prefix=prefixlen) in seen_networks:
                    covered_items.add(item)
                    break
            seen_networks.add(network)

    if not covered_items:
        return items, 0

    return [item for item in items if item not in covered_items], len(covered_items)


def parse_rule_item(item):
    if not isinstance(item, str):
        return None, "", ()
    parts = item.split(",")
    rule_type = parts[0]
    values = tuple(part.strip() for part in parts[1:])
    return rule_type, values[0] if values else "", values


def rule_sort_key(index_and_item):
    index, item = index_and_item
    rule_type, _value, _values = parse_rule_item(item)
    rank = RULE_TYPE_ORDER.get(rule_type, 1000)
    return rank, index


def sort_rule_payload(items):
    return [item for _index, item in sorted(enumerate(items), key=rule_sort_key)]


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


def normalize_source_item(file_path, node_name, item):
    if isinstance(item, str) and item:
        return {"url": item, "format": "auto"}
    if not isinstance(item, dict):
        raise RuleUpdateError(f"Node {node_name} in {file_path} contains an invalid URL entry: {item!r}")

    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise RuleUpdateError(f"Node {node_name} in {file_path} contains an invalid URL: {url!r}")

    source_format = item.get("format", "auto")
    if source_format not in SUPPORTED_SOURCE_FORMATS:
        raise RuleUpdateError(
            f"Node {node_name} in {file_path} has unsupported source format {source_format!r}; "
            f"expected one of {sorted(SUPPORTED_SOURCE_FORMATS)}"
        )

    source_item = {"url": url, "format": source_format}
    if "name" in item:
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise RuleUpdateError(f"Node {node_name} in {file_path} contains an invalid DLC name: {name!r}")
        source_item["name"] = name
    return source_item


def validate_source_node(file_path, node_name, node_data):
    if not isinstance(node_data, dict):
        raise RuleUpdateError(f"Node {node_name} in {file_path} is not a mapping")
    urls = node_data.get("urls")
    if not isinstance(urls, list) or not urls:
        raise RuleUpdateError(f"Node {node_name} in {file_path} must define a non-empty urls list")
    return [normalize_source_item(file_path, node_name, item) for item in urls]


def collect_generated_rules(source_dir):
    started_at = time.perf_counter()
    source_entries = build_source_plan(source_dir)
    plan_elapsed = time.perf_counter() - started_at
    downloaded_texts, download_elapsed = download_all_texts(collect_download_urls(source_entries))
    process_started_at = time.perf_counter()
    generated_rules = {}
    generated_indexes = {}
    stats = {
        "nodes": 0,
        "source_payloads": 0,
        "deduped_payloads": 0,
        "removed_duplicates": 0,
        "removed_covered": 0,
        "removed_ip_covered": 0,
    }
    dlc_cache = {}
    dlc_state = {"repo_ready": False}

    for entry in source_entries:
        file_path = entry["file_path"]
        node_name = entry["node_name"]
        node_data = entry["node_data"]
        source_items = entry["source_items"]
        subpath = entry["subpath"]
        index_name = entry["index_name"]
        index_data = generated_indexes.setdefault(index_name, {})

        downloaded_payloads = []
        for source_item in source_items:
            downloaded_payloads.extend(
                download_payload(source_item, dlc_cache, DLC_REPO_DIR, dlc_state, downloaded_texts)
            )

        exact_deduped_payloads = dedupe_keep_order(downloaded_payloads)
        cleaned_payloads, removed_covered = remove_covered_domain_rules(exact_deduped_payloads)
        cleaned_payloads, removed_ip_covered = remove_covered_ip_rules(cleaned_payloads)
        merged_payloads = sort_rule_payload(cleaned_payloads)
        rule_rel_path = Path(subpath) / f"{node_name}.yaml" if subpath else Path(f"{node_name}.yaml")
        generated_rules[rule_rel_path.as_posix()] = {"payload": merged_payloads}
        index_data[node_name] = build_rule_entry(node_name, node_data, subpath)

        source_count = len(downloaded_payloads)
        exact_deduped_count = len(exact_deduped_payloads)
        final_count = len(merged_payloads)
        stats["nodes"] += 1
        stats["source_payloads"] += source_count
        stats["deduped_payloads"] += final_count
        stats["removed_duplicates"] += source_count - exact_deduped_count
        stats["removed_covered"] += removed_covered
        stats["removed_ip_covered"] += removed_ip_covered
        print(
            f"Processed {file_path}::{node_name}: "
            f"source={source_count}, deduped={final_count}, "
            f"duplicates={source_count - exact_deduped_count}, "
            f"covered={removed_covered}, ip_covered={removed_ip_covered}",
            flush=True,
        )

    stats["timings"] = {
        "plan": plan_elapsed,
        "download": download_elapsed,
        "process": time.perf_counter() - process_started_at,
    }
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
        started_at = time.perf_counter()
        generated_rules, generated_indexes, stats = collect_generated_rules(SOURCE_DIR)
        write_started_at = time.perf_counter()
        write_tmp_output(TMP_OUTPUT_DIR, generated_rules, generated_indexes)
        publish_output(TMP_OUTPUT_DIR, generated_rules, generated_indexes)
        write_elapsed = time.perf_counter() - write_started_at
        total_elapsed = time.perf_counter() - started_at
        timings = stats.get("timings", {})
        print(
            "Rule update complete: "
            f"nodes={stats['nodes']}, files={len(generated_rules)}, "
            f"source_payloads={stats['source_payloads']}, "
            f"deduped_payloads={stats['deduped_payloads']}, "
            f"removed_duplicates={stats['removed_duplicates']}, "
            f"removed_covered={stats['removed_covered']}, "
            f"removed_ip_covered={stats['removed_ip_covered']}, "
            f"timings=plan:{timings.get('plan', 0):.2f}s/"
            f"download:{timings.get('download', 0):.2f}s/"
            f"process:{timings.get('process', 0):.2f}s/"
            f"write:{write_elapsed:.2f}s/"
            f"total:{total_elapsed:.2f}s",
            flush=True,
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
