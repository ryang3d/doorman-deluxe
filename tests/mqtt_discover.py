#!/usr/bin/env python3
"""Discover MQTT topics relevant to the doorbell. Listens ~12s, read-only."""
import sys, time, os
def getenv(path, key):
    for line in open(path):
        line = line.strip()
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None

MU = getenv('~/.hermes/profiles/home-admin/.env', 'MQTT_USER')
MP = getenv('~/.hermes/profiles/home-admin/.env', 'MQTT_PASSWORD')

import paho.mqtt.client as mqtt
seen = set()
def on_msg(c, u, m):
    t = m.topic
    if any(k in t.lower() for k in ['doorbell', 'door', 'person', 'motion', 'occupanc', 'front']):
        seen.add((t, (m.payload or b'')[:60]))

c = mqtt.Client()
c.username_pw_set(MU, MP)
c.on_message = on_msg
c.connect('<ha-host>', 1883, 60)
c.subscribe('#')
c.loop_start()
time.sleep(12)
c.loop_stop()
print('=== relevant topics seen ===')
for t, p in sorted(seen):
    print(f'{t} = {p}')
print(f'total: {len(seen)}')
