#!/usr/bin/env python3
"""Test the MQTT listener connects + subscribes to frigate/events without blocking forever.
Prints confirmation, exits after 8s. Read-only."""
import asyncio, sys, os, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import paho.mqtt.client as mqtt

def creds():
    d = {}
    for line in open(os.path.expanduser('~/.hermes/profiles/home-admin/.env')):
        line = line.strip()
        if line.startswith(('MQTT_USER=', 'MQTT_PASSWORD=')):
            k, v = line.split('=', 1)
            d[k] = v.strip().strip('"').strip("'")
    return d.get('MQTT_USER'), d.get('MQTT_PASSWORD')

async def main():
    user, pw = creds()
    connected = asyncio.Event()
    got = []
    loop = asyncio.get_event_loop()
    def on_connect(c, u, f, rc, *a):
        print("connected rc=", rc)
        connected.set()
    def on_message(c, u, msg):
        got.append(msg.topic)
        print("msg:", msg.topic, str(msg.payload)[:80])
    c = mqtt.Client()
    c.username_pw_set(user, pw)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect('<ha-host>', 1883, 60)
    c.subscribe('frigate/events')
    def run(): c.loop_forever()
    import threading
    threading.Thread(target=run, daemon=True).start()
    await asyncio.wait_for(connected.wait(), timeout=10)
    print("subscribed to frigate/events. listening 8s for any event...")
    await asyncio.sleep(8)
    print("done. messages received:", len(got))

asyncio.run(main())
