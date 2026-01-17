#!/bin/sh

/etc/openradiation/bt-autoconnect.sh

ps aux | grep -v grep | grep -v SCREEN |grep -ie openradiation.py > /dev/null
if [ $? -eq 0 ]; then
  echo "openradiation Process is running."
else
  echo "Process openradiation is not running. Lunch new"
  screen -S daemonopenradiation -dm /etc/openradiation/openradiation.py
fi
