"""
This Python script performs the following tasks:

1. Merge the contents of the 'payload' node from all YAML files in the './rules/rules_set' directory
   and create a new YAML file named 'merge.yaml' in the './exclude' directory.
   The merged 'payload' list will be deduplicated.

2. Copy the 'proxy.yaml' file from the './rules/rules_set' directory to the './exclude' directory
   and rename it as 'proxy_init.yaml'.

3. Compare the contents of the 'payload' node in the 'merge.yaml' and 'proxy_init.yaml' files.
   Remove the contents present in 'merge.yaml' from 'proxy_init.yaml' and save the remaining
   contents to a new file named 'proxy.yaml' in the './exclude' directory.
   If 'merge.yaml' is empty, 'proxy.yaml' will contain the contents of 'proxy_init.yaml'.

Note:
- The './rules/rules_set' and './exclude' directories are predefined.
- All files are opened with the encoding='utf-8' parameter.
- The main logic is in the main() function, while independent processing is in separate functions.
"""

import os
import yaml

# Define file paths
RULES_DIR = "./rules/rules_set"
EXCLUDE_DIR = "./exclude"


def merge_payloads_in_dir(rules_dir):
    """
    Merge the 'payload' node contents from all YAML files in the specified directory.
    The merged 'payload' list will be deduplicated.

    Args:
        rules_dir (str): The directory path containing YAML files.

    Returns:
        list: A list containing the merged and deduplicated 'payload' node contents.
    """
    merged_payloads = []
    for filename in os.listdir(rules_dir):
        if filename == "proxy.yaml":
            continue
        if filename.endswith(".yaml"):
            filepath = os.path.join(rules_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                try:
                    data = yaml.safe_load(f)
                except yaml.YAMLError as exc:
                    print(f"Error loading YAML file: {filepath}. Error: {exc}")
                    continue
                if data and "payload" in data:
                    payloads = data.get("payload", [])
                    if payloads:
                        merged_payloads.extend(payloads)
    # Deduplicate the merged payloads
    merged_payloads = list(set(merged_payloads))
    return merged_payloads


def copy_proxy_file(rules_dir, exclude_dir):
    """
    Copy the 'proxy.yaml' file from the specified source directory to the destination directory
    and rename it as 'proxy_init.yaml'.

    Args:
        rules_dir (str): The source directory path containing 'proxy.yaml'.
        exclude_dir (str): The destination directory path.
    """
    proxy_file = os.path.join(rules_dir, "proxy.yaml")
    proxy_init_file = os.path.join(exclude_dir, "proxy_init.yaml")
    with open(proxy_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    os.makedirs(exclude_dir, exist_ok=True)
    with open(proxy_init_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


def filter_payloads(exclude_dir):
    """
    Compare the 'payload' node contents in the 'merge.yaml' and 'proxy_init.yaml' files.
    Remove the contents present in 'merge.yaml' from 'proxy_init.yaml' and save the remaining
    contents to a new file named 'proxy.yaml' in the specified directory.
    If 'merge.yaml' is empty, 'proxy.yaml' will contain the contents of 'proxy_init.yaml'.

    Args:
        exclude_dir (str): The directory path containing 'merge.yaml' and 'proxy_init.yaml'.
    """
    merge_file = os.path.join(exclude_dir, "merge.yaml")
    proxy_init_file = os.path.join(exclude_dir, "proxy_init.yaml")
    with open(merge_file, "r", encoding="utf-8") as f:
        merge_data = yaml.safe_load(f)
    with open(proxy_init_file, "r", encoding="utf-8") as f:
        proxy_data = yaml.safe_load(f)

    merge_payloads = merge_data.get("payload", [])
    proxy_payloads = proxy_data.get("payload", [])

    if merge_payloads:
        filtered_payloads = [p for p in proxy_payloads if p not in merge_payloads]
    else:
        filtered_payloads = proxy_payloads

    proxy_file = os.path.join(exclude_dir, "proxy.yaml")
    with open(proxy_file, "w", encoding="utf-8") as f:
        yaml.safe_dump({"payload": filtered_payloads}, f)


def main():
    """
    Main function to execute the tasks.
    """
    merged_payloads = merge_payloads_in_dir(RULES_DIR)
    os.makedirs(EXCLUDE_DIR, exist_ok=True)
    merge_file = os.path.join(EXCLUDE_DIR, "merge.yaml")
    with open(merge_file, "w", encoding="utf-8") as f:
        yaml.safe_dump({"payload": merged_payloads}, f)

    copy_proxy_file(RULES_DIR, EXCLUDE_DIR)
    filter_payloads(EXCLUDE_DIR)


if __name__ == "__main__":
    main()
