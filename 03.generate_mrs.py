#!/usr/bin/env python3
"""Compile existing classical outputs without fetching or changing rule content."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
ROOT = Path(__file__).resolve().parent
RAW_BASE = "https://raw.githubusercontent.com/darkli/research/main/rules/rules_set"
SCHEMA_VERSION = 2
CLASSICAL_TYPES = {
    "DOMAIN-KEYWORD", "DOMAIN-REGEX", "DOMAIN-WILDCARD", "GEOSITE",
    "IP-ASN", "GEOIP", "IP-SUFFIX", "SRC-IP-CIDR", "SRC-IP-ASN", "SRC-GEOIP",
    "SRC-IP-SUFFIX", "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-REGEX",
    "PROCESS-PATH-REGEX", "PROCESS-NAME-WILDCARD", "PROCESS-PATH-WILDCARD",
    "SRC-PORT", "DST-PORT", "IN-PORT", "IN-TYPE", "IN-USER", "IN-NAME",
    "NETWORK", "UID", "DSCP", "AND", "OR", "NOT", "REMATCH-NAME",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?$")


class MrsError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump_yaml(data):
    return yaml.dump(data, Dumper=DUMPER, allow_unicode=True, sort_keys=False).encode()


def run_mihomo(binary, *args):
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True)
    # The converter may log an invalid entry and still return success.
    if result.returncode or re.search(r"level=(warning|error|fatal)|\bWARN\b|\bERROR\b", result.stdout + result.stderr):
        raise MrsError(f"Mihomo failed: {' '.join(map(str, args))}\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def classify(rule):
    if not isinstance(rule, str) or not rule or "\n" in rule:
        raise MrsError(f"Invalid classical rule: {rule!r}")
    rule_type, comma, value = rule.partition(",")
    if not comma or not value:
        raise MrsError(f"Missing rule value: {rule!r}")
    if rule_type in {"DOMAIN", "DOMAIN-SUFFIX"}:
        # Keep unusual literal syntax classical; do not interpret it as a wildcard.
        if not DOMAIN_RE.fullmatch(value) or value.endswith("."):
            return "classical", False, rule
        return "domain", False, ("+." if rule_type == "DOMAIN-SUFFIX" else "") + value.lower()
    if rule_type in {"IP-CIDR", "IP-CIDR6"}:
        fields = value.split(",")
        try:
            network = ipaddress.ip_network(fields[0], strict=True)
        except ValueError as exc:
            raise MrsError(f"Invalid CIDR: {rule}") from exc
        # Mihomo's IP set unmapped these into IPv4 during compilation, whereas a
        # classical IPv6 prefix retains its address family. Keep that distinction.
        if network.version == 6 and network.network_address.ipv4_mapped is not None:
            return "classical", False, rule
        options = fields[1:]
        if any(option not in {"no-resolve", "src"} for option in options):
            raise MrsError(f"Unsupported CIDR options: {rule}")
        if "src" in options:
            return "classical", False, rule
        return "ipcidr", "no-resolve" in options, str(network)
    if rule_type not in CLASSICAL_TYPES:
        raise MrsError(f"Unsupported rule type, refusing to omit it: {rule_type}")
    return "classical", False, rule


def split_payload(payload):
    if not isinstance(payload, list) or not payload:
        raise MrsError("A classical payload must be a nonempty list")
    blocks = []
    for rule in payload:
        behavior, no_resolve, value = classify(rule)
        key = behavior, no_resolve
        if not blocks or (blocks[-1]["behavior"], blocks[-1]["no_resolve"]) != key:
            blocks.append({"behavior": behavior, "no_resolve": no_resolve, "original": [], "values": []})
        blocks[-1]["original"].append(rule)
        blocks[-1]["values"].append(value)
    if [rule for block in blocks for rule in block["original"]] != payload:
        raise MrsError("Rule assignment changed the original order")
    return blocks


def compile_blocks(blocks):
    """DomainSet folds case; guard each exact/suffix-length partition to retain
    classical case-sensitive predicates. Only pure, adjacent domain predicates
    are regrouped; no DNS/process/IP boundary is crossed.
    """
    result = []
    for block in blocks:
        if block["behavior"] != "domain":
            result.append(block)
            continue
        partitions = {}
        for original, value in zip(block["original"], block["values"]):
            guard = f"case-suffix-{len(value) - 2}" if value.startswith("+.") else "case-exact"
            part = partitions.setdefault(guard, {"behavior": "domain", "no_resolve": False,
                                                  "case_guard": guard, "original": [], "values": []})
            part["original"].append(original)
            part["values"].append(value)
        result.extend(partitions.values())
    if sorted(x for b in result for x in b["original"]) != sorted(x for b in blocks for x in b["original"]):
        raise MrsError("Domain partitioning omitted or added a rule")
    return result


def domain_coverage(lines):
    """Canonical exact/suffix coverage; MRS may export '+.x' as 'x' and '.x'."""
    exact, suffix = set(), set()
    for line in lines:
        value = line.strip().lower()
        if value.startswith("+."):
            exact.add(value[2:])
            suffix.add(value[2:])
        elif value.startswith("."):
            suffix.add(value[1:])
        elif DOMAIN_RE.fullmatch(value):
            exact.add(value)
        else:
            raise MrsError(f"Unexpected exported domain: {line!r}")
    def has_parent(value):
        labels = value.split(".")
        return any(".".join(labels[i:]) in suffix for i in range(1, len(labels)))
    return sorted(x for x in exact if not has_parent(x)), sorted(x for x in suffix if not has_parent(x))


def ip_coverage(lines):
    nets = [ipaddress.ip_network(line.strip(), strict=True) for line in lines]
    return tuple(tuple(str(n) for n in ipaddress.collapse_addresses(x for x in nets if x.version == version))
                 for version in (4, 6))


def verify_roundtrip(behavior, original, exported):
    actual = [line.strip() for line in exported.splitlines() if line.strip()]
    canonical = domain_coverage if behavior == "domain" else ip_coverage
    if canonical(original) != canonical(actual):
        raise MrsError(f"{behavior} MRS roundtrip changed rule coverage")
    return len(actual)


def input_files(rules_dir):
    paths = [*rules_dir.glob("*.yaml"), *(rules_dir / "no_resolve").glob("*.yaml")]
    if not paths:
        raise MrsError(f"No classical rule files in {rules_dir}")
    return sorted(paths, key=lambda p: p.relative_to(rules_dir).as_posix())


def render_conf(providers, groups, base_url=RAW_BASE):
    lines = ["# Generated by 03.generate_mrs.py; keep bindings and policy order unchanged.",
             "# Physical providers: name|type|behavior|path|interval|format",
             f'CLASH_RULE_BASE_URL="{base_url}"', 'CLASH_RULE_LOCAL_DIR="./rules_set"',
             "CLASH_RULE_PROVIDERS=("]
    for name, node in providers.items():
        lines.append(f'  "{name}|http|{node["behavior"]}|{node["relative_path"]}|86400|{node["format"]}"')
    lines += [")", "", "# Logical names stay identical to the existing profile bindings.",
              "# Members are ordered and separated by semicolons; flags follow a comma.", "CLASH_RULE_GROUPS=("]
    for name, members in groups.items():
        lines.append(f'  "{name}|{";".join(members)}"')
    return "\n".join([*lines, ")", ""])


def make_outputs(rules_dir, output_dir, binary):
    files = input_files(rules_dir)
    frozen = {p.relative_to(rules_dir).as_posix(): p.read_bytes() for p in files}
    version = run_mihomo(binary, "-v").splitlines()[0]
    fingerprint = json.dumps({"schema": SCHEMA_VERSION, "converter": version,
                              "sources": {k: digest(v) for k, v in frozen.items()}}, sort_keys=True).encode()
    snapshot = digest(fingerprint)[:24]
    release = output_dir / "rules_set" / "mrs" / snapshot
    modes = {"normal": {"providers": {}, "groups": {}}, "no_resolve": {"providers": {}, "groups": {}}}
    manifest = {"schema": SCHEMA_VERSION, "snapshot": snapshot, "converter": version,
                "sources": {}, "files": {}, "modes": modes}
    total_rules = 0
    for relative, content in frozen.items():
        path = Path(relative)
        name = path.stem
        if not NAME_RE.fullmatch(name):
            raise MrsError(f"Unsafe rule set name: {name}")
        mode = "no_resolve" if path.parent == Path("no_resolve") else "normal"
        data = yaml.load(content, Loader=LOADER)
        if not isinstance(data, dict) or set(data) != {"payload"}:
            raise MrsError(f"Expected only a payload mapping in {relative}")
        payload = data["payload"]
        blocks = compile_blocks(split_payload(payload))
        source = {"sha256": digest(content), "count": len(payload), "blocks": []}
        members = []
        for index, block in enumerate(blocks, 1):
            behavior = block["behavior"]
            provider = f"mrs_{'nr' if mode == 'no_resolve' else 'n'}_{name}_{index:03}"
            suffix = "yaml" if behavior == "classical" else "mrs"
            block_path = Path(mode) / name / f"{index:03}.{behavior}.{suffix}"
            target = release / block_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if behavior == "classical":
                target.write_bytes(dump_yaml({"payload": block["original"]}))
                if yaml.load(target.read_bytes(), Loader=LOADER)["payload"] != block["original"]:
                    raise MrsError(f"Classical residual changed: {relative}")
                exported_count = len(block["original"])
            else:
                with tempfile.TemporaryDirectory(prefix="mrs-compile-") as temp:
                    text_path, exported_path = Path(temp) / "input.txt", Path(temp) / "roundtrip.txt"
                    text_path.write_text("\n".join(block["values"]) + "\n")
                    run_mihomo(binary, "convert-ruleset", behavior, "text", text_path, target)
                    run_mihomo(binary, "convert-ruleset", behavior, "mrs", target, exported_path)
                    try:
                        exported_count = verify_roundtrip(behavior, block["values"], exported_path.read_text())
                    except MrsError as exc:
                        raise MrsError(f"{relative} block {index}: {exc}") from exc
            rel_path = f"mrs/{snapshot}/{block_path.as_posix()}"
            node = {"type": "http", "behavior": behavior, "format": suffix if suffix == "mrs" else "yaml",
                    "relative_path": rel_path, "path": f"./rules_set/{rel_path}",
                    "url": f"{RAW_BASE}/{rel_path}", "interval": 86400}
            modes[mode]["providers"][provider] = node
            flag = block.get("case_guard") or ("no-resolve" if block["no_resolve"] else "")
            members.append(provider + ("," + flag if flag else ""))
            source["blocks"].append({"provider": provider, "behavior": behavior,
                                     "no_resolve": block["no_resolve"], "count": len(block["original"]),
                                     "case_guard": block.get("case_guard"),
                                     "exported_count": exported_count})
            manifest["files"][rel_path] = {"sha256": digest(target.read_bytes()), "bytes": target.stat().st_size}
        modes[mode]["groups"][name] = members
        manifest["sources"][relative] = source
        total_rules += len(payload)
    # Hand-maintained sets have no separate no-resolve file. Their original per-rule flags remain intact.
    for name, members in modes["normal"]["groups"].items():
        if name not in modes["no_resolve"]["groups"]:
            modes["no_resolve"]["groups"][name] = members
            for member in members:
                provider = member.split(",", 1)[0]
                modes["no_resolve"]["providers"][provider] = modes["normal"]["providers"][provider]
    for mode, values in modes.items():
        suffix = "_no_resolve" if mode == "no_resolve" else ""
        native_providers = {name: {k: v for k, v in node.items() if k != "relative_path"}
                            for name, node in values["providers"].items()}
        (output_dir / f"rules_mrs{suffix}.yaml").write_bytes(dump_yaml(native_providers))
        (output_dir / f"rule_groups_mrs{suffix}.yaml").write_bytes(dump_yaml(values["groups"]))
        (output_dir / f"rule_providers_mrs{suffix}.conf").write_text(render_conf(values["providers"], values["groups"]))
    manifest["verification"] = {"classical_files": len(files), "assigned_rules": total_rules,
                                 "roundtrip": "passed", "order": "side_effect_boundaries_preserved",
                                 "domain_case": "classical_sensitive_guards"}
    (output_dir / "mrs_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    for relative, content in frozen.items():
        if (rules_dir / relative).read_bytes() != content:
            raise MrsError(f"Classical source changed during compilation: {relative}")
    return manifest


def publish(stage, destination):
    """Immutable bundles first; manifest pointer last. Older bundles are kept for rollback."""
    manifest = json.loads((stage / "mrs_manifest.json").read_text())
    rel = Path("rules_set/mrs") / manifest["snapshot"]
    target = destination / rel
    if target.exists():
        existing = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        expected = {p.relative_to(stage / rel): p.read_bytes() for p in (stage / rel).rglob("*") if p.is_file()}
        if existing != expected:
            raise MrsError(f"Refusing to overwrite a different immutable bundle: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage / rel), target)
    files = sorted(p for p in stage.glob("*") if p.is_file() and p.name != "mrs_manifest.json")
    for source in [*files, stage / "mrs_manifest.json"]:
        temp = destination / f".{source.name}.tmp"
        shutil.copyfile(source, temp)
        temp.replace(destination / source.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mihomo", default=os.environ.get("MIHOMO_BIN") or shutil.which("mihomo"))
    parser.add_argument("--rules-dir", type=Path, default=ROOT / "rules/rules_set")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "rules")
    args = parser.parse_args()
    if not args.mihomo:
        parser.error("Set MIHOMO_BIN or pass --mihomo pointing to an official Mihomo executable")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".mrs-stage-", dir=args.output_dir) as temp:
            stage = Path(temp)
            manifest = make_outputs(args.rules_dir, stage, args.mihomo)
            publish(stage, args.output_dir)
        print(f"MRS verified: {manifest['verification']['classical_files']} classical files, "
              f"{manifest['verification']['assigned_rules']} rules, snapshot={manifest['snapshot']}")
        return 0
    except (MrsError, OSError, yaml.YAMLError, ValueError) as exc:
        print(f"MRS generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
