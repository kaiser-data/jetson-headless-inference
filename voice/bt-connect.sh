#!/bin/bash
# Auto-connect Bluetooth speaker on login and set as default PulseAudio sink.
# Called by jetson-bt.service (systemd user service).

BT_MAC="${BT_SPEAKER_MAC:-88:88:11:07:10:5C}"
BT_NAME="${BT_SPEAKER_NAME:-Boomcore P06}"
MAX_TRIES=8
RETRY_DELAY=4

echo "[bt-connect] Connecting to $BT_NAME ($BT_MAC)..."

for i in $(seq 1 $MAX_TRIES); do
    OUT=$(bluetoothctl connect "$BT_MAC" 2>&1)
    if echo "$OUT" | grep -q "successful"; then
        echo "[bt-connect] Connected on attempt $i"
        sleep 3  # give PulseAudio time to register the sink

        # Find the BT sink and set as default
        SINK=$(pactl list sinks short 2>/dev/null \
               | awk '{print $2}' \
               | grep -i "$(echo $BT_MAC | tr ':' '_')" \
               | head -1)

        if [ -n "$SINK" ]; then
            pactl set-default-sink "$SINK"
            echo "[bt-connect] Set default sink: $SINK"
        else
            echo "[bt-connect] Warning: sink not found in PulseAudio yet"
        fi
        exit 0
    fi
    echo "[bt-connect] Attempt $i/$MAX_TRIES failed — retrying in ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
done

echo "[bt-connect] Could not connect to $BT_NAME after $MAX_TRIES attempts"
exit 1
