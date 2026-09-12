#!/usr/bin/env python3
"""Unit tests for the door-zone occupancy gate (DOORMAN_PERSON_GATE /
DOORMAN_PERSON_HOLD_S) and its integration into the Frigate listener.

Run (inside the doorman container):
  docker cp tests doorman:/tmp/tests
  docker exec doorman sh -c 'cd /tmp/tests && export PYTHONPATH=/app/src && python test_doorzone_gate.py'
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


# ---------------------------------------------------------------- config layer
def test_gate_default_entity():
    _clear()
    assert dc.load()['DOORMAN_PERSON_GATE'] == 'binary_sensor.front_patio_motion_zone_person_occupancy'
    print("PASS gate default = front patio zone occupancy sensor")


def test_hold_default_and_override():
    _clear()
    assert dc.load()['DOORMAN_PERSON_HOLD_S'] == 5.0
    os.environ['DOORMAN_PERSON_HOLD_S'] = '8'
    assert dc.load()['DOORMAN_PERSON_HOLD_S'] == 8.0
    print("PASS hold default 5.0 + env override numeric")


def test_gate_empty_disables():
    _clear()
    os.environ['DOORMAN_PERSON_GATE'] = ''
    assert dc.load()['DOORMAN_PERSON_GATE'] == ''
    print("PASS empty DOORMAN_PERSON_GATE disables the gate")


# ---------------------------------------------------------------- gate state machine
def test_gate_state_machine():
    g = dm.DoorZoneGate('binary_sensor.x', 5.0)
    assert g.satisfied(0.0) is False
    assert g.in_seconds(0.0) == 0.0
    g.mark_on(10.0)
    assert g.satisfied(14.9) is False
    assert g.satisfied(15.0) is True
    assert g.in_seconds(17.0) == 7.0
    g.mark_off(16.0)
    assert g.satisfied(20.0) is False
    g.mark_on(20.0)
    assert g.satisfied(24.9) is False
    assert g.satisfied(25.0) is True
    print("PASS gate state machine (on/off break the hold)")


# ---------------------------------------------------------------- listener harness
class _FakeMsg:
    def __init__(self, payload_dict):
        self.payload = json.dumps(payload_dict).encode()


def _ev(camera, label, etype, event_id, sub=None):
    after = {'camera': camera, 'label': label, 'id': event_id}
    if sub is not None:
        after['sub_label'] = sub
    return {'type': etype, 'after': after}


async def _drive(script, *, hold=5.0, gate=True, personalized=False, cooldown=0.0):
    """Drive the REAL frigate_event_listener with a scripted timeline.

    script: a list of (target_fake_time, action) where action is:
        ('on',)        -> gate.mark_on(now)
        ('off',)       -> gate.mark_off(now)
        ('ev', event)  -> push a Frigate event
    Times are FAKE seconds. The event-loop clock (time.monotonic) is a scaled
    real-advancing clock so asyncio timers keep firing; we advance to each target
    by sleeping the real equivalent. Returns ordered [(fake_time, label, name)]
    trigger calls. Patches paho.mqtt.client.Client and time.monotonic; restores both.
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

    calls = []
    real_mono = time.monotonic
    t0 = real_mono()
    real_client = paho_mod.Client
    real_cool = dm.INTERACTION_COOLDOWN_S
    gate_obj = dm.DoorZoneGate('binary_sensor.gate', float(hold)) if gate else None

    def fake_mono():
        return (real_mono() - t0) * SCALE
    time.monotonic = fake_mono
    paho_mod.Client = _FakePaho
    dm.INTERACTION_COOLDOWN_S = float(cooldown)

    async def fake_handle(prompt, trigger_text, meta):
        calls.append((time.monotonic(), meta.get('label'), meta.get('name')))

    async def advance_to(target):
        cur = time.monotonic()
        if target > cur:
            await asyncio.sleep((target - cur) / SCALE)

    try:
        task = asyncio.ensure_future(
            dm.frigate_event_listener(fake_handle,
                                      personalized_greeting=personalized,
                                      gate=gate_obj))
        await asyncio.sleep(0.05)          # let it connect + subscribe (fake clock advances)
        client = _FakePaho.instance
        assert client is not None, "listener did not construct its MQTT client"
        for target, action in script:
            await advance_to(target)
            if action[0] == 'on':
                gate_obj.mark_on(time.monotonic())
            elif action[0] == 'off':
                gate_obj.mark_off(time.monotonic())
            else:
                client.on_message(None, None, _FakeMsg(action[1]))
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


def _near(t, expected, tol=1.0):
    return abs(t - expected) <= tol


# ---------------------------------------------------------------- listener behavior
async def _test_walkby_gated():
    # person new @0; gate on @1 off @3; person still detected (update) @4 -> gate not held
    script = [
        (0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1'))),
        (1.0, ('on',)),
        (3.0, ('off',)),
        (4.0, ('ev', _ev('front_doorbell', 'person', 'update', 'e1'))),
    ]
    calls = await _drive(script)
    assert calls == [], calls
    print("PASS walk-by: gate on only 2s, person event does not trigger")


async def _test_held_visit_triggers_once():
    script = [
        (0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1'))),
        (0.5, ('on',)),
        (5.5, ('ev', _ev('front_doorbell', 'person', 'update', 'e1'))),
        (6.5, ('ev', _ev('front_doorbell', 'person', 'update', 'e1'))),
    ]
    calls = await _drive(script)
    assert len(calls) == 1 and calls[0][1] == 'person', calls
    assert _near(calls[0][0], 5.5, 1.5), calls          # fires on first event once held
    print("PASS held visit (gate on from 0.5s) triggers once, not on the later update")


async def _test_gate_off_triggers_on_new_as_today():
    script = [(0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1')))]
    calls = await _drive(script, gate=False)
    assert len(calls) == 1 and calls[0][1] == 'person', calls
    assert _near(calls[0][0], 0.0, 1.0), calls
    print("PASS gate disabled (None): triggers on first detection, as today")


async def _test_animal_ignores_gate():
    script = [(0.0, ('ev', _ev('front_doorbell', 'cat', 'new', 'c1')))]
    calls = await _drive(script)     # gate present but never turned on
    assert len(calls) == 1 and calls[0][1] == 'cat', calls
    assert _near(calls[0][0], 0.0, 1.0), calls
    print("PASS animal fires immediately even with gate never on")


async def _test_face_held_like_person():
    script = [(0.0, ('ev', _ev('front_doorbell', 'face', 'new', 'f1')))]
    calls = await _drive(script)     # gate present, never on -> not satisfied
    assert calls == [], calls
    print("PASS face is gated like person")


async def _test_settled_path_gate_gates():
    # personalized=True, no recognition: settles at 25s grace; gate on only 0-3s
    script = [
        (0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1'))),
        (0.0, ('on',)),
        (3.0, ('off',)),
        (30.0, ('ev', _ev('front_doorbell', 'person', 'update', 'e1'))),
    ]
    calls = await _drive(script, personalized=True)
    assert calls == [], calls
    print("PASS personalized path: gate not held at settle time -> no trigger")


async def _test_settled_path_gate_held():
    script = [
        (0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1'))),
        (0.0, ('on',)),
        (30.0, ('ev', _ev('front_doorbell', 'person', 'update', 'e1'))),
    ]
    calls = await _drive(script, personalized=True)
    assert len(calls) == 1 and calls[0][1] == 'person', calls
    assert _near(calls[0][0], 30.0, 1.5), calls
    print("PASS personalized path: gate held -> triggers at settle time")


async def _test_recognized_name_survives_gate():
    # recognition arrives @2 (event would settle) but gate not yet held (5s from @0);
    # the 25s-grace settle @30 (gate still on) fires WITH the recognized name
    script = [
        (0.0, ('ev', _ev('front_doorbell', 'person', 'new', 'e1'))),
        (0.0, ('on',)),
        (2.0, ('ev', _ev('front_doorbell', 'person', 'update', 'e1', sub=['Ryan', 0.97]))),
        (30.0, ('ev', _ev('front_doorbell', 'person', 'end', 'e1'))),
    ]
    calls = await _drive(script, personalized=True)
    assert len(calls) == 1 and calls[0][2] == 'Ryan', calls
    print("PASS face recognition name survives the gate (settled path)")


# ---------------------------------------------------------------- seed (startup / reconnect state sync)
class _FakeAioResp:
    def __init__(self, state, status=200):
        self._state = state
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return {'entity_id': 'binary_sensor.gate', 'state': self._state}


class _FakeAioSession:
    def __init__(self, state, status=200, raise_exc=False):
        self._state = state
        self._status = status
        self._raise = raise_exc
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return _FakeAioResp(self._state, self._status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _seed_case(state, hold=5.0, status=200, raise_exc=False):
    """Run the real _seed_gate_from_state against a fake aiohttp; assert the
    hold clock was set (on) or left unset (off)."""
    import aiohttp
    real_cs = aiohttp.ClientSession
    fake = _FakeAioSession(state, status, raise_exc)
    aiohttp.ClientSession = lambda *a, **k: fake
    g = dm.DoorZoneGate('binary_sensor.gate', hold)
    try:
        await dm._seed_gate_from_state('http://ha.local:8123/', 'binary_sensor.gate', 'tok', g)
    finally:
        aiohttp.ClientSession = real_cs
    return g, fake


async def _test_seed_starts_hold_when_already_on():
    g, fake = await _seed_case('on')
    assert fake.calls == 1, "seed must hit /api/states exactly once"
    assert g._on_since is not None, "seed 'on' must set the hold clock"
    # before the hold window elapses it is not satisfied; after, it is
    assert g.satisfied(g._on_since + g.hold_s - 0.5) is False
    assert g.satisfied(g._on_since + g.hold_s) is True
    print("PASS seed: sensor already 'on' -> hold clock starts (satisfies after hold_s)")


async def _test_seed_ignores_when_off():
    g, fake = await _seed_case('off')
    assert fake.calls == 1
    assert g._on_since is None, "seed with 'off' must NOT start the hold"
    assert g.satisfied(1e9) is False
    print("PASS seed: sensor 'off' -> hold clock not started")


async def _test_seed_survives_http_error():
    # a failing GET must not raise and must leave the gate in a known (not-on) state
    g, fake = await _seed_case('on', raise_exc=True)
    assert fake.calls == 1
    assert g._on_since is None
    print("PASS seed: HTTP error -> no raise, hold not started")


if __name__ == '__main__':
    test_gate_default_entity()
    test_hold_default_and_override()
    test_gate_empty_disables()
    test_gate_state_machine()
    asyncio.run(_test_walkby_gated())
    asyncio.run(_test_held_visit_triggers_once())
    asyncio.run(_test_gate_off_triggers_on_new_as_today())
    asyncio.run(_test_animal_ignores_gate())
    asyncio.run(_test_face_held_like_person())
    asyncio.run(_test_settled_path_gate_gates())
    asyncio.run(_test_settled_path_gate_held())
    asyncio.run(_test_recognized_name_survives_gate())
    asyncio.run(_test_seed_starts_hold_when_already_on())
    asyncio.run(_test_seed_ignores_when_off())
    asyncio.run(_test_seed_survives_http_error())
    print("ALL DOORZONE GATE TESTS PASS")
