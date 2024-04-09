"""
检查网页是否正常工作

Args:
    urls: 要检查的网页 URL 列表

Returns:
    一个字典，其中包含每个网页的检查结果
"""

import requests

# 定义要检查的网页列表
URLS_LIST = [
    "http://010005.xyz",
    "http://010006.xyz",
    "http://050006.xyz",
    "https://060007.xyz",
    "http://060009.xyz",
]


def check_urls(urls):
    """
    检查网页是否正常工作

    Args:
        urls: 要检查的网页 URL 列表

    Returns:
        一个字典，其中包含每个网页的检查结果
    """

    results = {}
    for url in urls:
        # 发送 HTTP 请求
        try:
            response = requests.get(url, timeout=5)
        except requests.exceptions.Timeout:
            results[url] = {"status_code": "超时"}
        else:
            # 检查 HTTP 状态码
            if response.status_code == 200:
                results[url] = {"status_code": "正常"}
            else:
                results[url] = {"status_code": f"异常，状态码为 {response.status_code}"}

    return results


if __name__ == "__main__":
    # 检查网页
    checked_results = check_urls(URLS_LIST)

    # 打印结果
    normal_urls = []
    error_urls = []
    for url, result in checked_results.items():
        if result["status_code"] == "正常":
            normal_urls.append(url)
        else:
            error_urls.append(url)

    print("访问正常的网页：")
    if normal_urls:
        for url in normal_urls:
            print(f"    {url}")
    else:
        print("    无")

    print("访问异常的网页：")
    if error_urls:
        for url in error_urls:
            print(f"    {url}：{checked_results[url]['status_code']}")
    else:
        print("    无")
