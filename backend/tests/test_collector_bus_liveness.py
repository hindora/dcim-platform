"""A collector is alive when its heartbeat reaches the stream, whether or not
a worker has processed it yet - see collector_visibility.seen_on_bus."""

import msgpack

from app.alarms.collector_visibility import newest_on_bus


def _entry(ms, cid, seq=0):
    return (f"{ms}-{seq}".encode(), {b"p": msgpack.packb({"collector_id": cid})})


def test_the_newest_arrival_per_collector_is_taken_from_the_entry_id():
    got = newest_on_bus([
        _entry(1_700_000_030_000, "col-a"),
        _entry(1_700_000_010_000, "col-a"),
        _entry(1_700_000_020_000, "col-b"),
    ])
    assert got == {"col-a": 1_700_000_030.0, "col-b": 1_700_000_020.0}


def test_entries_it_cannot_read_are_skipped_not_guessed():
    got = newest_on_bus([
        (b"1700000000000-0", {b"x": b"no payload"}),
        (b"1700000000000-1", {b"p": b"\xc1not msgpack"}),
        (b"1700000000000-2", {b"p": msgpack.packb({"no": "id"})}),
        (b"garbage", {b"p": msgpack.packb({"collector_id": "col-z"})}),
    ])
    assert got == {}
