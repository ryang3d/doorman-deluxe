#!/usr/bin/env python3
"""Listener tests for DOORMAN_IGNORED_FACES (personalized path).

An ignored recognized name must produce NO trigger; a non-ignored recognized name
must still trigger exactly once with that name; an unrecognized visitor still triggers.

Run (host venv):  .venv/bin/python tests/test_ignored_faces.py
Run (in container): docker cp tests doorman:/tmp/tests
  docker exec doorman sh -c 'cd /tmp/tests && export PYTHONPATH=/app/src && python test_ignored_faces.py'
"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman as dm
import doorman_config as dc

SCALE = 20.0   # fake-seconds per real-second in the listener harness


def _clear():
    for k in list(os.environ):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


class _FakeMsg:
    def __init__(self, payload_dict):
        self.payload = json.dumps(payload_dict).encode()


def _ev(camera, label, etype, event_id, sub=None):
    after = {'camera': camera, 'label': label, 'id': event_id}
    if sub is not None:
        after['sub_label'] = sub
    return {'type': etype, 'after': after}


async def _drive(script, *, ignored=''):
    """Drive the REAL frigate_event_listener with a scripted timeline.

    script: list of (target_fake_time, event) where event is a Frigate event dict.
    Patches paho.mqtt.client.Client and time.monotonic; restores both.
    Returns ordered [(fake_time, label, name)] trigger calls.
    """
    import paho.mqtt.client as paho_mod

    class _FakePaho:
        instance = None
        def __init__(self, *a, **k):
            _FakePaho.instance = self
        def username_pw_set(self, *a, **k):
            pass
        def connect(self, *a, **k):
            pass
        def subscribe(self, topic):
            pass
        def loop_forever(self):
            pass
        def reconnect_delay_set(self, *a, **k):
            pass

    calls = []
    real_mono = time.monotonic
    t0 = real_mono()
    real_client = paho_mod.Client
    real_cool = dm.INTERACTION_COOLDOWN_S

    def fake_mono():
        return (real_mono() - t0) * SCALE

    time.monotonic = fake_mono
    paho_mod.Client = _FakePaho
    dm.INTERACTION_COOLDOWN_S = 0.0
    # set the ignore set the way load() would (so we don't depend on env in the listener)
    dm.IGNORED_FACES = {n.strip().lower() for n in ignored.split(',') if n.strip()}

    async def fake_handle(prompt, trigger_text, meta):
        calls.append((time.monotonic(), meta.get('label'), meta.get('name')))

    async def advance_to(target):
        cur = time.monotonic()
        if target > cur:
            await asyncio.sleep((target - cur) / SCALE)

    try:
        task = asyncio.ensure_future(
            dm.frigate_event_listener(fake_handle, personalized_greeting=True, gate=None))
        await asyncio.sleep(0.05)          # let it connect + subscribe (fake clock advances)
        client = _FakePaho.instance
        assert client is not None, "listener did not construct its MQTT client"
        for target, action in script:
            await advance_to(target)
            client.on_message(None, None, _FakeMsg(action))
            await asyncio.sleep(0.02)      # let the loop process the queued item
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        time.monotonic = real_mono
        paho_mod.Client = real_client
        dm.INTERACTION_COOLDOWN_S = real_cool
    return calls


# ---------------------------------------------------------------- behavior
async def _test_ignored_name_no_trigger():
    # Bob is on the ignore list; recognition arrives @3, event ends @4 -> NO trigger
    script = [
        (0.0, _ev('front_doorbell', 'person', 'new', 'e1')),
        (3.0, _ev('front_doorbell', 'person', 'update', 'e1', sub=['Bob', 0.98])),
        (4.0, _ev('front_doorbell', 'person', 'end', 'e1', sub=['Bob', 0.98])),
    ]
    calls = await _drive(script, ignored='Bob')
    assert calls == [], calls
    print("PASS ignored name (Bob) -> no trigger")


async def _test_non_ignored_name_triggers():
    # Alice is NOT ignored; recognition @3 -> triggers ONCE with name 'Alice'
    script = [
        (0.0, _ev('front_doorbell', 'person', 'new', 'e1')),
        (3.0, _ev('front_doorbell', 'person', 'update', 'e1', sub=['Alice', 0.97])),
    ]
    calls = await _drive(script, ignored='Bob')
    assert len(calls) == 1 and calls[0][2] == 'Alice', calls
    print("PASS non-ignored name (Alice) -> triggers once with the name")


async def _test_case_insensitive_ignore():
    # config lists 'bob' (lower); recognition is 'Bob' -> should still be ignored
    script = [
        (0.0, _ev('front_doorbell', 'person', 'new', 'e1')),
        (3.0, _ev('front_doorbell', 'person', 'update', 'e1', sub=['Bob', 0.98])),
    ]
    calls = await _drive(script, ignored='bob')
    assert calls == [], calls
    print("PASS case-insensitive ignore (config 'bob' vs sub_label 'Bob')")


async def _test_unrecognized_still_triggers():
    # no recognition by the 25s grace -> settles as unknown and still triggers (name None)
    script = [
        (0.0, _ev('front_doorbell', 'person', 'new', 'e1')),
        (30.0, _ev('front_doorbell', 'person', 'update', 'e1')),
    ]
    calls = await _drive(script, ignored='Bob')
    assert len(calls) == 1 and calls[0][2] is None, calls
    print("PASS unrecognized visitor still triggers (name None) even with a non-empty ignore list")


async def _test_name_arriving_on_end_ignored():
    # edge case: the name is only ever seen on the 'end' event (not a prior update).
    # The ignore check lives in the single settled-trigger block, so it must catch this too.
    script = [
        (0.0, _ev('front_doorbell', 'person', 'new', 'e1')),
        (3.0, _ev('front_doorbell', 'person', 'end', 'e1', sub=['Bob', 0.98])),
    ]
    calls = await _drive(script, ignored='Bob')
    assert calls == [], calls
    print("PASS ignored name arriving only on 'end' -> no trigger")


if __name__ == '__main__':
    _clear()
    asyncio.run(_test_ignored_name_no_trigger())
    asyncio.run(_test_non_ignored_name_triggers())
    asyncio.run(_test_case_insensitive_ignore())
    asyncio.run(_test_unrecognized_still_triggers())
    asyncio.run(_test_name_arriving_on_end_ignored())
    print("ALL IGNORED-FACES LISTENER TESTS PASS")
