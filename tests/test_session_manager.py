from concurrent.futures import ThreadPoolExecutor

from core.session.manager import SessionManager


def test_native_session_key_format_is_unchanged() -> None:
    assert SessionManager.build_key("ou_1", "oc_1") == "ou_1:oc_1"
    assert SessionManager.build_key("u1", "c1") != SessionManager.build_key("u2", "c1")
    assert SessionManager.build_key("u1", "c1") != SessionManager.build_key("u1", "c2")


def test_pending_file_fifo_keeps_latest_twenty() -> None:
    manager = SessionManager()
    key = SessionManager.build_key("u1", "c1")
    for index in range(25):
        manager.note_incoming_file(key, f"file-{index}")

    assert manager.take_pending_files(key) == [f"file-{index}" for index in range(5, 25)]
    assert manager.take_pending_files(key) == []


def test_reset_is_local_to_one_session_and_idempotent() -> None:
    manager = SessionManager()
    manager.note_incoming_file("u1:c1", "feishu-file")
    manager.note_incoming_file("wechat:bot:u1", "wechat-file")
    manager.reset_session("u1:c1")
    manager.reset_session("u1:c1")

    assert manager.take_pending_files("u1:c1") == []
    assert manager.take_pending_files("wechat:bot:u1") == ["wechat-file"]


def test_concurrent_drains_deliver_each_notice_once() -> None:
    manager = SessionManager()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: manager.note_incoming_file("key", f"file-{index}"), range(20)))
        drained = list(pool.map(lambda _: manager.take_pending_files("key"), range(8)))

    notices = [notice for batch in drained for notice in batch]
    assert len(notices) == 20
    assert set(notices) == {f"file-{index}" for index in range(20)}
    assert sum(bool(batch) for batch in drained) == 1
