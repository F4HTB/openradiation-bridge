#!/usr/bin/env sh
set -euo pipefail

MAC="AA:AA:AA:AA:AA:AA"


if bluetoothctl info "$MAC" | grep -q "Connected: yes"; then
    	echo "Déjà appairé avec $MAC"
else
	echo "Pas appairé avec $MAC, relance..."
	bluetoothctl --timeout 2 power on
	bluetoothctl --timeout 2 agent on
	bluetoothctl --timeout 2 default-agent
	bluetoothctl --timeout 2 scan on
	bluetoothctl --timeout 2 pair "$MAC"
	bluetoothctl --timeout 2 trust "$MAC"
fi
