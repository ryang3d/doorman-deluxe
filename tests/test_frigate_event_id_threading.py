"""Verify the Frigate trigger path carries event_id into the session meta."""
import asyncio, sys, os, types, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman as dm


class _FakeClient:
    def __init__(self, *a, **k):
        self.on_message_cb = None
    def on_message(self, cb):
        self.on_message_cb = cb
    def username_pw_set(self, *a, **k): pass
    def reconnect_delay_set(self, *a, **k): pass
    def connect(self, *a, **k): return 0
    def subscribe(self, *a, **k): return (0, None)
    def loop_start(self): pass
    def loop_forever(self):
        m = types.SimpleNamespace(payload=b'{"type":"new","after":{"id":"1700.5-eid42","camera":"front_doorbell","label":"person","sub_label":null}}')
        u = types.SimpleNamespace()
        self.on_message(None, u, m)
        while True:
            time.sleep(60)
    def loop_stop(self, *a): pass
    def disconnect(self): pass


def test_frigate_trigger_meta_carries_event_id(monkeypatch, tmp_path):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DOORMAN_PERSONALIZED_GREETING', 'false')
    monkeypatch.setenv('DOORMAN_FRONT_CAMERA', 'front_doorbell')
    import paho.mqtt
    import paho.mqtt.client as _mqtt_client
    monkeypatch.setattr(_mqtt_client, 'Client', _FakeClient)
    captured = {}
    async def fake_handle_event(prompt, trigger_text, meta):
        captured.update(meta)
    async def main():
        task = asyncio.ensure_future(dm.frigate_event_listener(
            fake_handle_event, personalized_greeting=False))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(main())
    assert captured.get('frigate_event_id') == '1700.5-eid42', captured
    assert captured.get('label') == 'person'
