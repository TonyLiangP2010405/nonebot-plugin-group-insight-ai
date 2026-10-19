"""存储层测试：增删查改与 TTL 语义。"""

from __future__ import annotations

from conftest import DAY, START, day_str, make_at_data, make_message

from nonebot_plugin_group_insight.config import Config
from nonebot_plugin_group_insight.storage import Storage, get_storage, set_storage


async def test_save_and_get_messages_roundtrip(storage):
    await storage.save_message(111, make_message(user_id=1001, text="第一条"))
    await storage.save_message(
        111,
        make_message(user_id=1002, text="第二条", time=START + 5, hasReply=True, images=["http://img/a.jpg"]),
    )

    messages = await storage.get_messages(111, days=1)

    assert [m["message"] for m in messages] == ["第一条", "第二条"]
    assert messages[0]["user_id"] == 1001
    assert messages[1]["hasReply"] is True
    assert messages[1]["images"] == ["http://img/a.jpg"]
    assert messages[0]["timestamp"] == START * 1000


async def test_get_messages_multi_day_is_time_ascending(storage, clock):
    await storage.save_message(111, make_message(text="昨天", time=START))
    clock["now"] = START + DAY
    await storage.save_message(111, make_message(text="今天", time=START + DAY))

    assert [m["message"] for m in await storage.get_messages(111, days=2)] == ["昨天", "今天"]
    assert [m["message"] for m in await storage.get_messages(111, days=1)] == ["今天"]
    assert [m["message"] for m in await storage.get_messages(111, days=1, target_date=day_str(START))] == ["昨天"]
    assert await storage.get_messages(111, days=0) == []
    assert await storage.get_messages(999, days=1) == []


async def test_message_bucket_ttl_set_on_first_write_only(storage, clock):
    await storage.save_message(111, make_message(text="第一条", time=START))
    clock["now"] = START + 1000
    await storage.save_message(111, make_message(text="第二条", time=START + 1))

    rows = await storage._query("SELECT expire_at FROM messages ORDER BY id")
    assert [row["expire_at"] for row in rows] == [START + 7 * DAY, START + 7 * DAY]

    clock["now"] = START + 7 * DAY + 1
    assert await storage.get_messages(111, days=1, target_date=day_str(START)) == []


async def test_new_day_bucket_gets_own_ttl(storage, clock):
    await storage.save_message(111, make_message(text="第一天", time=START))
    clock["now"] = START + 8 * DAY
    await storage.save_message(111, make_message(text="第八天", time=START + 8 * DAY))

    rows = await storage._query("SELECT expire_at FROM messages ORDER BY id")
    assert [row["expire_at"] for row in rows] == [START + 7 * DAY, START + 15 * DAY]

    clock["now"] = START + 7 * DAY + 1
    assert [m["message"] for m in await storage.get_messages(111, days=1, target_date=day_str(START))] == []
    assert [m["message"] for m in await storage.get_messages(111, days=1, target_date=day_str(START + 8 * DAY))] == [
        "第八天"
    ]

    clock["now"] = START + 15 * DAY + 1
    assert await storage.get_messages(111, days=1, target_date=day_str(START + 8 * DAY)) == []


async def test_get_message_count(storage, clock):
    await storage.save_message(111, make_message(time=START))
    await storage.save_message(111, make_message(time=START + 1))
    await storage.save_message(222, make_message(time=START))

    assert await storage.get_message_count(111) == 2
    assert await storage.get_message_count(222) == 1
    assert await storage.get_message_count(111, day_str(START + DAY)) == 0

    clock["now"] = START + 7 * DAY + 1
    assert await storage.get_message_count(111, day_str(START)) == 0


async def test_get_recent_user_messages(storage):
    await storage.save_message(111, make_message(user_id=1001, text="m1", time=START))
    await storage.save_message(111, make_message(user_id=1001, text="m2", time=START + 10))
    await storage.save_message(111, make_message(user_id=1002, text="other", time=START + 15))
    await storage.save_message(111, make_message(user_id=1001, text="m3", time=START + 20))

    recent = await storage.get_recent_user_messages(111, 1001, count=2)
    assert [m["message"] for m in recent] == ["m3", "m2"]
    assert recent[0]["images"] == []
    assert recent[0]["faces"] == {}
    assert recent[0]["links"] == []
    assert recent[0]["videos"] == []

    before = await storage.get_recent_user_messages(111, 1001, count=2, before_time=START + 20)
    assert [m["message"] for m in before] == ["m2", "m1"]

    all_in_window = await storage.get_recent_user_messages(111, "1001", count=1, days=1)
    assert [m["message"] for m in all_in_window] == ["m3", "m2", "m1"]

    assert await storage.get_recent_user_messages(111, 1002, count=5) == [
        {"message": "other", "time": START + 15, "images": [], "faces": {}, "links": [], "videos": []}
    ]
    assert await storage.get_recent_user_messages(111, "all") == []


async def test_report_roundtrip_and_permanent(storage, clock):
    report = {
        "stats": {"totalMessages": 10},
        "topics": [{"topic": "话题"}],
        "goldenQuotes": [{"quote": "金句"}],
        "userTitles": [{"title": "称号"}],
        "messageCount": 10,
        "tokenUsage": {"prompt": 1, "completion": 2, "total": 3},
    }
    await storage.save_report(111, day_str(START), report)

    saved = await storage.get_report(111, day_str(START))
    assert saved["stats"] == {"totalMessages": 10}
    assert saved["topics"] == [{"topic": "话题"}]
    assert saved["goldenQuotes"] == [{"quote": "金句"}]
    assert saved["userTitles"] == [{"title": "称号"}]
    assert saved["messageCount"] == 10
    assert saved["tokenUsage"] == {"prompt": 1, "completion": 2, "total": 3}
    assert saved["date"] == day_str(START)
    assert saved["savedAt"] == START * 1000
    assert saved["lastUpdateTime"] == START * 1000

    clock["now"] = START + 365 * DAY
    assert await storage.get_report(111, day_str(START)) is not None
    assert await storage.get_report(111, "2023-01-01") is None
    assert await storage.get_report(222, day_str(START)) is None


async def test_report_with_retention(storage, clock):
    await storage.save_report(111, day_str(START), {"stats": {}}, retention_days=1)
    assert await storage.get_report(111, day_str(START)) is not None

    clock["now"] = START + DAY + 1
    assert await storage.get_report(111, day_str(START)) is None


async def test_report_overwrite_keeps_latest(storage):
    await storage.save_report(111, day_str(START), {"stats": {"v": 1}, "messageCount": 1})
    await storage.save_report(111, day_str(START), {"stats": {"v": 2}, "messageCount": 2})

    saved = await storage.get_report(111, day_str(START))
    assert saved["stats"] == {"v": 2}
    assert saved["messageCount"] == 2


async def test_batch_cache_ttl(storage, clock):
    await storage.save_batch(
        111,
        day_str(START),
        0,
        {"success": True, "topics": [{"topic": "t"}], "goldenQuotes": [], "tokenUsage": {}, "analyzedAt": 1},
    )

    batch = await storage.get_batch(111, day_str(START), 0)
    assert batch["batchIndex"] == 0
    assert batch["success"] is True
    assert batch["topics"] == [{"topic": "t"}]
    assert await storage.get_batch(111, day_str(START), 1) is None

    clock["now"] = START + 86400 - 1
    assert await storage.get_batch(111, day_str(START), 0) is not None

    clock["now"] = START + 86400 + 1
    assert await storage.get_batch(111, day_str(START), 0) is None


async def test_batch_failure_marker_roundtrip(storage):
    await storage.save_batch(111, day_str(START), 2, {"success": False, "error": "boom", "analyzedAt": 1})

    batch = await storage.get_batch(111, day_str(START), 2)
    assert batch["success"] is False
    assert batch["error"] == "boom"
    assert batch["batchIndex"] == 2


async def test_cooldown(storage, clock):
    assert (await storage.check_cooldown(111))["inCooldown"] is False

    await storage.set_cooldown(111, generated_by="user", message_count=42)

    status = await storage.check_cooldown(111)
    assert status["inCooldown"] is True
    assert status["remainingMinutes"] == 60
    assert status["lastGenerated"]["generatedBy"] == "user"
    assert status["lastGenerated"]["messageCount"] == 42
    assert status["lastGenerated"]["elapsedMinutes"] == 0

    assert (await storage.check_cooldown(111, ignore_cooldown=True))["inCooldown"] is False
    assert (await storage.check_cooldown(222))["inCooldown"] is False

    clock["now"] = START + 30 * 60
    assert (await storage.check_cooldown(111))["remainingMinutes"] == 30
    assert (await storage.check_cooldown(111, cooldown_minutes=10))["inCooldown"] is False

    clock["now"] = START + 61 * 60
    assert (await storage.check_cooldown(111))["inCooldown"] is False

    clock["now"] = START + 86400 + 1
    assert (await storage.check_cooldown(111))["inCooldown"] is False


async def test_at_record_lifecycle(storage, clock):
    at_data = make_at_data(context_messages=[{"message": "前文", "time": START - 10}])

    record_id = await storage.save_at_record(111, 222, at_data)

    assert record_id == f"111_222_{START}"
    records = await storage.get_at_records(111, 222)
    assert len(records) == 1
    record = records[0]
    assert record["user_id"] == 333
    assert record["nickname"] == "sender"
    assert record["message"] == "hi"
    assert record["messageId"] == "99"
    assert record["contextMessages"] == [{"message": "前文", "time": START - 10}]
    assert record["nextMessages"] == []
    assert record["endTime"]
    assert await storage.get_at_records(111, 999) == []

    assert await storage.update_at_record_next_message(record_id, [{"message": "后文", "time": START + 5}]) is True
    assert (await storage.get_at_records(111, 222))[0]["nextMessages"] == [{"message": "后文", "time": START + 5}]

    clock["now"] = START + 24 * 3600 + 1
    assert await storage.get_at_records(111, 222) == []
    assert await storage.update_at_record_next_message(record_id, []) is False
    assert await storage.update_at_record_next_message(None, []) is False


async def test_at_record_expired_is_not_saved(tmp_path, clock):
    store = Storage(db_path=tmp_path / "expired.db", config=Config(group_insight_at_retention_hours=0))
    await store.init()
    try:
        assert await store.save_at_record(111, 222, make_at_data()) is None
        assert await store.get_at_records(111, 222) == []

        await store.save_pending_at(111, 333, None, START + 300)
        assert await store.get_pending_ats(111, 333) == [None]
    finally:
        await store.close()


async def test_clear_at_records(storage):
    await storage.save_at_record(111, 222, make_at_data())
    await storage.save_at_record(111, 333, make_at_data(time=START + 1))
    await storage.save_at_record(222, 222, make_at_data())

    assert await storage.clear_at_records(111, 222) == 1
    assert await storage.get_at_records(111, 222) == []
    assert len(await storage.get_at_records(111, 333)) == 1
    assert await storage.clear_at_records(111, 222) == 0

    assert await storage.clear_all_at_records() == 2
    assert await storage.get_at_records(111, 333) == []


async def test_pending_ats_lifecycle(storage, clock):
    await storage.save_pending_at(111, 333, "111_222_1", START + 300)

    assert await storage.get_pending_ats(111, 333) == ["111_222_1"]
    assert await storage.get_pending_ats(111, 444) == []

    clock["now"] = START + 300
    assert await storage.get_pending_ats(111, 333) == ["111_222_1"]

    clock["now"] = START + 301
    assert await storage.get_pending_ats(111, 333) == []

    await storage.save_pending_at(111, 333, "rec-a", START + 900)
    await storage.save_pending_at(111, 333, "rec-b", START + 901)
    assert sorted(await storage.pop_pending_ats(111, 333)) == ["rec-a", "rec-b"]
    assert await storage.get_pending_ats(111, 333) == []

    await storage.save_pending_at(111, 333, "rec-c", START + 900)
    await storage.remove_pending_at(111, 333, "rec-c")
    assert await storage.get_pending_ats(111, 333) == []


async def test_generating_lock(storage, clock):
    assert await storage.acquire_generating_lock(111, day_str(START)) is True
    assert await storage.acquire_generating_lock(111, day_str(START)) is False
    assert await storage.acquire_generating_lock(111, day_str(START + DAY)) is True

    clock["now"] = START + 300
    assert await storage.acquire_generating_lock(111, day_str(START)) is True

    await storage.release_generating_lock(111, day_str(START))
    assert await storage.acquire_generating_lock(111, day_str(START)) is True


async def test_purge_expired(storage, clock):
    await storage.save_message(111, make_message())
    await storage.save_batch(111, day_str(START), 0, {"success": True})
    await storage.set_cooldown(111)
    await storage.save_at_record(111, 222, make_at_data())
    await storage.save_pending_at(111, 333, "rec", START + 10)
    await storage.save_report(111, day_str(START), {"stats": {}}, retention_days=1)

    clock["now"] = START + 8 * DAY
    deleted = await storage.purge_expired()

    assert deleted == 6
    assert await storage.get_messages(111, days=1, target_date=day_str(START)) == []
    assert await storage.get_batch(111, day_str(START), 0) is None
    assert await storage.get_at_records(111, 222) == []
    assert await storage.get_pending_ats(111, 333) == []
    assert await storage.get_report(111, day_str(START)) is None


def test_storage_singleton():
    original = get_storage()
    try:
        assert get_storage() is original
        custom = Storage(db_path=":memory:")
        set_storage(custom)
        assert get_storage() is custom
    finally:
        set_storage(original)
