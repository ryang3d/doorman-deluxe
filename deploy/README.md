# Doorman deployment / operations (Phase 4)

Doorman now runs as a persistent systemd USER service on the doorman host (<doorman-host>),
the host where the working venv lives. It auto-starts, auto-restarts, and logs to journald.

## Service

- Unit: ~/.config/systemd/user/doorman.service (tracked in repo at deploy/doorman.service)
- Runs: ~/doorman/.venv/bin/python ~/doorman/src/doorman.py
- WorkingDirectory: ~/doorman/src  (so `import audio_bridge` resolves)
- Restart=always, RestartSec=5  (auto-recovers from crash AND SIGTERM; Restart=on-failure
  treats SIGTERM as a clean stop and does NOT recover - verified 2026-09-08)
- Enabled for autostart (lingering enabled for user ryan, so it runs without GUI login)

## Common operations

    systemctl --user status doorman.service        # is it up / recent logs
    systemctl --user restart doorman.service       # after a code change
    systemctl --user stop doorman.service          # stop it
    journalctl --user -u doorman.service -f        # follow live logs (observability)
    journalctl --user -u doorman.service -n 100    # last 100 lines

## Crash recovery test (verified 2026-09-08)
Killing the main PID with SIGTERM: service auto-restarts within ~5s and re-subscribes
to frigate/events (NRestarts increments). A healthy restart logs
"INFO subscribed to frigate/events" with no DeprecationWarning (paho v2 callback API).

## Config / credentials the service needs (NOT in repo - stay in home)
- ~/.hermes/profiles/home-admin/.env        (HASS_URL/HASS_TOKEN, MQTT_USER/PASSWORD)
- ~/.hermes/profiles/home-admin/frigate.env (FRIGATE_URL/USER/PASS, GEMINI_API_KEY)
- SSH key ~/.hermes/profiles/home-admin/home/.ssh/id_ed25519_hass
  (used by doorman_tools._publish_snapshot_to_ha to scp snapshots to HA www for notifications)
- The service reads these absolute paths at runtime (doorman_tools / doorman config).

## Behavior
- Subscribes to Frigate MQTT `frigate/events`, filters front_doorbell person/cat/dog/face.
- Waits for face recognition (sub_label, which Frigate sends as ['Name', conf] on 'update')
  before greeting - greets by name if recognized, unknown if not. (Delay tuning deferred to
  future plans; see plan.)
- On trigger: opens Gemini Live voice session, talks through the doorbell, and can call
  snapshot_front_door + notify_ryan tools.
