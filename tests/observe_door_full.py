#!/usr/bin/env python3
"""Observe front_doorbell ALL MQTT events over 40s to see the full new->update lifecycle
and exactly when sub_label arrives. Walk up to the door during the window."""
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
    t = time.strftime('%H:%M:%S')
    # id short, event id
    eid = (after.get('id') or '')[:14]
    print(f"[{t}] type={p.get('type')} label={after.get('label')} sub={after.get('sub_label')} id={eid}", flush=True)

user, pw = creds()
c = mqtt.Client()
c.username_pw_set(user, pw)
c.on_message = on_message
c.connect('<ha-host>', 1883, 60)
c.subscribe('frigate/events')
c.loop_start()
print("Observing ALL front_doorbell MQTT events for 45s. WALK UP TO THE DOOR now and stand there.", flush=True)
time.sleep(45)
c.loop_stop()
print("done observing")
