from app.document_loader import apply_version_metadata, filter_chunks_for_question


def item(name, text="内容"):
    return {"file_name": name, "file_path": name, "location": "全文", "text": text}


def active_names(items, question="当前规定是什么"):
    selected, _ = filter_chunks_for_question(apply_version_metadata(items), question)
    return {entry["file_name"] for entry in selected}


def test_latest_complete_version_replaces_older_complete_versions():
    items = [item("员工守则2024版.pdf"), item("员工守则2025版.pdf"), item("员工守则2026版.pdf")]
    assert active_names(items) == {"员工守则2026版.pdf"}


def test_amendments_are_layered_on_latest_complete_baseline():
    items = [
        item("员工守则2024版.pdf"),
        item("员工守则2025修订通知.pdf", "修改A"),
        item("员工守则2026补充规定.pdf", "修改B"),
    ]
    assert active_names(items) == {
        "员工守则2024版.pdf",
        "员工守则2025修订通知.pdf",
        "员工守则2026补充规定.pdf",
    }


def test_new_complete_version_resets_old_amendments():
    items = [
        item("员工守则2024版.pdf"),
        item("员工守则2025修订通知.pdf"),
        item("员工守则2026完整版.pdf"),
    ]
    assert active_names(items) == {"员工守则2026完整版.pdf"}


def test_historical_question_rebuilds_chain_at_that_year():
    items = [
        item("员工守则2024版.pdf"),
        item("员工守则2025修订通知.pdf"),
        item("员工守则2026补充规定.pdf"),
    ]
    assert active_names(items, "2025年A如何规定") == {
        "员工守则2024版.pdf",
        "员工守则2025修订通知.pdf",
    }


if __name__ == "__main__":
    test_latest_complete_version_replaces_older_complete_versions()
    test_amendments_are_layered_on_latest_complete_baseline()
    test_new_complete_version_resets_old_amendments()
    test_historical_question_rebuilds_chain_at_that_year()
    print("4 version-chain tests passed")
