"""通过真实 HTTP 接口验证演示账号的资料可见性和交叉问答。"""
import argparse
import json
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener


def client(base_url, username, password):
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    request = Request(base_url + "/api/login", json.dumps({"username": username, "password": password}).encode(), {"Content-Type": "application/json"}, method="POST")
    with opener.open(request, timeout=30) as response:
        json.load(response)
    return opener


def get(opener, url):
    with opener.open(url, timeout=180) as response:
        return json.load(response)


def post(opener, url, payload):
    request = Request(url, json.dumps(payload, ensure_ascii=False).encode(), {"Content-Type": "application/json"}, method="POST")
    with opener.open(request, timeout=180) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8501")
    parser.add_argument("--hr-password", required=True)
    parser.add_argument("--finance-password", required=True)
    args = parser.parse_args()
    hr = client(args.base_url, "demo_hr", args.hr_password)
    finance = client(args.base_url, "demo_finance", args.finance_password)
    hr_names = [item["file_name"] for item in get(hr, args.base_url + "/api/library")["items"]]
    finance_names = [item["file_name"] for item in get(finance, args.base_url + "/api/library")["items"]]
    assert "演示人事部内部手册_2026版.txt" in hr_names and "演示财务部内部手册_2026版.txt" not in hr_names
    assert "演示财务部内部手册_2026版.txt" in finance_names and "演示人事部内部手册_2026版.txt" not in finance_names
    checks = {
        "人事查人事": post(hr, args.base_url + "/api/ask", {"question": "人事部演示验证码是什么？"})["answer"],
        "人事查财务": post(hr, args.base_url + "/api/ask", {"question": "财务部演示验证码是什么？"})["answer"],
        "财务查财务": post(finance, args.base_url + "/api/ask", {"question": "财务部演示验证码是什么？"})["answer"],
        "财务查人事": post(finance, args.base_url + "/api/ask", {"question": "人事部演示验证码是什么？"})["answer"],
    }
    assert "青竹-HR-2026" in checks["人事查人事"]
    assert "海蓝-FIN-2026" not in checks["人事查财务"]
    assert "海蓝-FIN-2026" in checks["财务查财务"]
    assert "青竹-HR-2026" not in checks["财务查人事"]
    print(json.dumps({"library_acl": "passed", "answers": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
