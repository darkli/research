"""
This Python script performs the following tasks:

1. Merge the contents of the 'payload' node from all YAML files except 'proxy.yaml' in the './RULES/RULES_SET' directory
   and create a new YAML file named 'merge.yaml' in the './EXCLUDE' directory.
   The merged 'payload' list will be deduplicated.

2. Copy the 'proxy.yaml' file from the './RULES/RULES_SET' directory to the './EXCLUDE' directory
   and rename it as 'proxy_init.yaml'.

3. Compare the contents of the 'payload' node in the 'merge.yaml' and 'proxy_init.yaml' files.
   If 'proxy_init.yaml' is empty or its 'payload' node is a subset of 'merge.yaml', 'proxy.yaml' will be empty.
   Otherwise, remove the contents present in 'merge.yaml' from 'proxy_init.yaml' and save the remaining
   contents to a new file named 'proxy.yaml' in the './EXCLUDE' directory.

4. Copy the 'proxy.yaml' file from the './EXCLUDE' directory back to the './RULES/RULES_SET' directory,
   overwriting the original 'proxy.yaml' file.

Note:
- The './RULES/RULES_SET' and './EXCLUDE' directories are predefined.
- All files are opened with the encoding='utf-8' parameter.
- The main logic is in the main() function, while independent processing is in separate functions.
"""

import os
import shutil
import yaml

# Define file paths
RULES_DIR = "./RULES/RULES_SET"
EXCLUDE_DIR = "./EXCLUDE"


def merge_payloads_in_dir(rules_dir):
    """
    Merge the 'payload' node contents from all YAML files except 'proxy.yaml' in the specified directory.
    The merged 'payload' list will be deduplicated.

    Args:
        rules_dir (str): The directory path containing YAML files.

    Returns:
        list: A list containing the merged and deduplicated 'payload' node contents.
    """
    merged_payloads = []
    for filename in os.listdir(rules_dir):
        if filename.endswith(".yaml") and filename != "proxy.yaml":
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


def copy_proxy_file(src_dir, dest_dir):
    """
    Copy the 'proxy.yaml' file from the specified source directory to the destination directory
    and rename it as 'proxy_init.yaml'.

    Args:
        src_dir (str): The source directory path containing 'proxy.yaml'.
        dest_dir (str): The destination directory path.
    """
    src_file = os.path.join(src_dir, "proxy.yaml")
    dest_file = os.path.join(dest_dir, "proxy_init.yaml")
    shutil.copy(src_file, dest_file)


def filter_payloads(exclude_dir, rules_dir):
    """
    Compare the 'payload' node contents in the 'merge.yaml' and 'proxy_init.yaml' files.
    If 'proxy_init.yaml' is empty or its 'payload' node is a subset of 'merge.yaml', 'proxy.yaml' will be empty.
    Otherwise, remove the contents present in 'merge.yaml' from 'proxy_init.yaml' and save the remaining
    contents to a new file named 'proxy.yaml' in the specified directory.
    Copy the 'proxy.yaml' file back to the 'rules_dir' directory, overwriting the original file.

    Args:
        exclude_dir (str): The directory path containing 'merge.yaml' and 'proxy_init.yaml'.
        rules_dir (str): The directory path where the 'proxy.yaml' file will be copied back.
    """
    merge_file = os.path.join(exclude_dir, "merge.yaml")
    proxy_init_file = os.path.join(exclude_dir, "proxy_init.yaml")
    with open(merge_file, "r", encoding="utf-8") as f:
        merge_data = yaml.safe_load(f)
    with open(proxy_init_file, "r", encoding="utf-8") as f:
        proxy_data = yaml.safe_load(f)

    merge_payloads = set(merge_data.get("payload", []))
    proxy_payloads = set(proxy_data.get("payload", []))

    if not proxy_payloads or proxy_payloads.issubset(merge_payloads):
        filtered_payloads = []
    else:
        filtered_payloads = list(proxy_payloads - merge_payloads)

    proxy_file = os.path.join(exclude_dir, "proxy.yaml")
    with open(proxy_file, "w", encoding="utf-8") as f:
        yaml.safe_dump({"payload": filtered_payloads}, f)

    # Copy the 'proxy.yaml' file back to the 'rules_dir' directory
    dest_file = os.path.join(rules_dir, "proxy.yaml")
    shutil.copy(proxy_file, dest_file)


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
    filter_payloads(EXCLUDE_DIR, RULES_DIR)


if __name__ == "__main__":
    main()
