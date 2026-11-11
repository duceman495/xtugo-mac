#!/bin/bash
# 双击即可启动：XTU GO 相机 Web UI
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 webui/server.py
