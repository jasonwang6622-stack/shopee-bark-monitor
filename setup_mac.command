#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
if [ ! -f config.json ]; then cp config.example.json config.json; fi
printf '\n安裝完成！現在請編輯 config.json，設定 Bark Key 和商品網址。\n'
open -e config.json
