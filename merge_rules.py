import os
import yaml
import shutil
import requests
from urllib.parse import urlparse

# 定义全局变量
SOURCE_DIR = "./source"
TMP_DIR = "./.tmp"
RULES_DIR = "./rules"
RULES_SET_DIR = "./rules/rules_set"


def download_files(urls, tmp_dir):
    """
    下载URLs中的文件到临时目录
    """
    downloaded_payloads = []
    for url in urls:
        try:
            response = requests.get(url, timeout=300)
            response.raise_for_status()
            file_name = os.path.basename(urlparse(url).path)
            file_path = os.path.join(tmp_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(response.text)
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            downloaded_payloads.extend(data.get("payload", []))
        except Exception as e:
            print(f"Error downloading file: {url}, {e}")
    return downloaded_payloads


def merge_payloads(payloads):
    """
    合并Payloads列表并去重
    """
    merged_payloads = []
    for payload in payloads:
        if payload not in merged_payloads:
            merged_payloads.append(payload)
    return merged_payloads


def write_to_yaml(file_path, data):
    """
    写入YAML文件
    """
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def process_file(file_path, rules_yaml):
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    total_payloads = 0
    processed_payloads = 0
    for node_name, node_data in data.items():
        urls = node_data.get("urls", [])
        downloaded_payloads = download_files(urls, TMP_DIR)
        total_payloads += len(downloaded_payloads)

        merged_payloads = merge_payloads(downloaded_payloads)
        processed_payloads += len(merged_payloads)

        rule_set_file = os.path.join(RULES_SET_DIR, f"{node_name}.yaml")
        write_to_yaml(rule_set_file, {"payload": merged_payloads})

        rules_yaml[node_name] = {
            "type": node_data.get("type"),
            "behavior": node_data.get("behavior"),
            "path": f"./rules_set/{node_name}.yaml",
            "interval": node_data.get("interval", 86400),
            "url": f"https://github.com/darkli/research/main/rules/rules_set/{node_name}.yaml",
        }

    if total_payloads != processed_payloads:
        print(
            f"Warning: Payload count mismatch for {os.path.basename(file_path)}. Expected: {total_payloads}, Processed: {processed_payloads}"
        )
    else:
        print(f"Successfully processed {os.path.basename(file_path)}. Total payloads: {total_payloads}")

    return rules_yaml


def main():
    # 创建必要的目录
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(RULES_SET_DIR, exist_ok=True)
    rules_path = os.path.join(RULES_DIR, "rules.yaml")

    if not os.path.exists(rules_path):
        rules_yaml = {}
    else:
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_yaml = yaml.safe_load(f) or {}

    for file_name in os.listdir(SOURCE_DIR):
        file_path = os.path.join(SOURCE_DIR, file_name)
        rules_yaml = process_file(file_path, rules_yaml)

    write_to_yaml(rules_path, rules_yaml)

    # 删除临时目录
    shutil.rmtree(TMP_DIR)


if __name__ == "__main__":
    main()
