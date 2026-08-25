"""创建两个虚构员工、三个演示文档，用于验证部门权限隔离。"""
import argparse
import os
from pathlib import Path

from app.access_control import add_department, authenticate, can_access, save_user, set_document_access


PUBLIC_HANDBOOK = """Tier 智能科技员工手册（2026 演示版）

第一章 工作时间
公司标准工作时间为工作日 09:00 至 18:00，午休时间为 12:00 至 13:00。

第二章 请假制度
员工申请年假应至少提前两个工作日提交，经直属负责人批准后生效。紧急病假可以当日补交证明。

第三章 信息安全
员工不得向公司外部人员泄露客户资料、业务数据、系统账号和内部文件。离开工位时应锁定电脑。

第四章 版本说明
本文件仅用于权限与检索测试，不代表公司的真实规章制度。
"""

HR_HANDBOOK = """人事部内部操作手册（2026 演示版）

一、入职材料复核
人事专员应在员工入职后三个工作日内完成身份证明、劳动合同和紧急联系人信息复核。

二、演示用人事验证码
本次权限测试的人事专属识别词为：青竹-HR-2026。该识别词仅用于验证人事部资料隔离。

三、薪酬资料权限
薪酬调整表仅限人事部管理员与获授权负责人查阅，不得发送到公共群聊。
"""

FINANCE_HANDBOOK = """财务部内部报销手册（2026 演示版）

一、差旅报销
差旅报销应在行程结束后十个工作日内提交，发票、行程单和审批记录必须齐全。

二、演示用财务验证码
本次权限测试的财务专属识别词为：海蓝-FIN-2026。该识别词仅用于验证财务部资料隔离。

三、付款复核
单笔付款超过五万元时，须由制单人与复核人分别确认，不得由同一人完成。
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hr-password", required=True)
    parser.add_argument("--finance-password", required=True)
    args = parser.parse_args()
    source_dir = Path(os.environ["SOURCE_DIR"])
    source_dir.mkdir(parents=True, exist_ok=True)

    documents = {
        "演示员工手册_2026版.txt": PUBLIC_HANDBOOK,
        "演示人事部内部手册_2026版.txt": HR_HANDBOOK,
        "演示财务部内部手册_2026版.txt": FINANCE_HANDBOOK,
    }
    for name, content in documents.items():
        (source_dir / name).write_text(content, encoding="utf-8")

    add_department("人事部")
    add_department("财务部")
    save_user({"username": "demo_hr", "display_name": "演示人事员工", "password": args.hr_password, "role": "employee", "departments": ["人事部"], "enabled": True})
    save_user({"username": "demo_finance", "display_name": "演示财务员工", "password": args.finance_password, "role": "employee", "departments": ["财务部"], "enabled": True})
    set_document_access("演示员工手册_2026版.txt", "public", [])
    set_document_access("演示人事部内部手册_2026版.txt", "departments", ["人事部"])
    set_document_access("演示财务部内部手册_2026版.txt", "departments", ["财务部"])
    hr = authenticate("demo_hr", args.hr_password, "__none__", b"x", "y")
    finance = authenticate("demo_finance", args.finance_password, "__none__", b"x", "y")
    names = list(documents)
    hr_access = [can_access(name, hr) for name in names]
    finance_access = [can_access(name, finance) for name in names]
    assert hr_access == [True, True, False]
    assert finance_access == [True, False, True]
    print(f"permission demo verified: HR={hr_access}, Finance={finance_access}")


if __name__ == "__main__":
    main()
