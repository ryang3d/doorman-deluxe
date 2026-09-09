#!/usr/bin/env python3
"""Check if ANY frigate/events are arriving on MQTT right now (all cameras, 20s)."""
import time, json
import paho.mqtt.client as mqtt

def creds():
    d = {}
    for line in open('~/.hermes/profiles/home-admin/.env'):
        line = line.strip()
        if line.startswith(('MQTT_USER=', 'MQTT_PASSWORD=')):
            k, v = line.split('=', 1)
            d[k] = v.strip().strip('"').strip("'")
    return d.get('MQTT_USER'), d.get('MQTT_PASSWORD')

def on_message(c, u, msg):
    t = time.strftime('%H:%M:%S')
    try:
        p = json.loads(msg.payload.decode())
        cam = p.get('after', {}).get('camera', '?')
        lab = p.get('after', {}).get('label', '?')
        sub = p.get('after', {}).get('sub_label')
        etype = p.get('type')
    except Exception:
        cam = lab = sub = '?'; etype = '?'
    print(f"[{t}] {msg.topic} type={etype} cam={cam} label={lab} sub={sub}", flush=True)

user, pw = creds()
c = mqtt.Client()
c.username_pw_set(user, pw)
c.on_message = on_message
c.connect('<ha-host>', 1883, 60)
c.subscribe('frigate/events')
c.loop_start()
print("Listening to ALL frigate/events for 20s...", flush=True)
time.sleep(20)
c.loop_stop()
print("done")
