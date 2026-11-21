#!/bin/bash
# voice.sh — control and inspect the voice+video server on this Pi.
# Usage: ./voice.sh status | logs | restart | stats | config | url
URL="https://100.87.117.55:9123"
case "${1:-status}" in
  status)  systemctl status voice --no-pager ;;
  logs)    journalctl -u voice -n 50 --no-pager ;;
  restart) sudo systemctl restart voice && sleep 3 && systemctl status voice --no-pager --lines 3 ;;
  stats)   curl -sk "$URL/stats"; echo ;;
  config)  curl -sk "$URL/config"; echo ;;
  url)     echo "$URL" ;;
  *) echo "usage: $0 status|logs|restart|stats|config|url" >&2; exit 1 ;;
esac
