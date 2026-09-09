#!/usr/bin/env python3
"""Observe Frigate MQTT event types + sub_label timing for front_doorbell over ~20s.
Watch for a person detection (trigger by walking up). Shows type + sub_label evolution.
Read-only observer."""
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
    try:
        p = json.loads(msg.payload.decode())
    except Exception:
        return
    after = p.get('after', {})
    if after.get('camera') != 'front_doorbell':
        return
    if after.get('label') != 'person':
        return
    t = time.strftime('%H:%M:%S')
    print(f"[{t}] type={p.get('type')} label={after.get('label')} sub_label={after.get('sub_label')} id={after.get('id','')[:12]} score={after.get('score',0):.2f}", flush=True)

user, pw = creds()
c = mqtt.Client()
c.username_pw_set(user, pw)
c.on_message = on_message
c.connect('<ha-host>', 1883, 60)
c.subscribe('frigate/events')
c.loop_start()
print("Observing front_doorbell person events for 30s. Walk up to the door now...", flush=True)
time.sleep(30)
c.loop_stop()
print("done observing")
