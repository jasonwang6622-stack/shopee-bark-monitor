#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f .venv/bin/python3 ]; then
  echo '尚未安裝！請先執行 setup_mac.command。'
  read -r -p '按 Enter 結束...'
  exit 1
fi
.venv/bin/python3 monitor.py
read -r -p '監控已結束，按 Enter 關閉視窗...'
