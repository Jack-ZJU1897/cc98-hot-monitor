#!/usr/bin/env bash
# ============================================================
# CC98 监控 · 服务器一键初始化(交互式,密码不落 shell 历史)
# 前提:
#   1) 本脚本与 cc98_hot_monitor.py、cc98_dashboard.py 放在同一目录,
#      且目录已建 venv 并装好依赖(requests beautifulsoup4 schedule pysocks);
#   2) /opt/zju-connect 二进制已就位(zju-connect linux/amd64);
#   3) 服务器能访问 rvpn.zju.edu.cn。
# 用法: sudo bash server_setup.sh
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$APP_DIR/venv/bin/python"
ZJU_BIN=/opt/zju-connect

echo "== CC98 服务器初始化 =="
[ "$(id -u)" = "0" ] || { echo "请用 root/sudo 运行"; exit 1; }
[ -x "$ZJU_BIN" ] || { echo "缺少 $ZJU_BIN,请先下载 zju-connect 并 chmod +x"; exit 1; }
[ -f "$VENV_PY" ] || { echo "缺少 venv($VENV_PY),请先创建并安装依赖"; exit 1; }
[ -f "$APP_DIR/cc98_hot_monitor.py" ] || { echo "缺少 $APP_DIR/cc98_hot_monitor.py"; exit 1; }

read -r -p "浙大统一认证学号: " ZJU_ID
read -r -s -p "统一认证密码(输入不回显): " ZJU_PWD; echo
[ -n "$ZJU_ID" ] && [ -n "$ZJU_PWD" ] || { echo "学号/密码不能为空"; exit 1; }

# 1) 密码入 env 文件(600)
umask 077
cat > /etc/zju-connect.env <<EOF
ZJU_CONNECT_PASSWORD=$ZJU_PWD
EOF
chmod 600 /etc/zju-connect.env
unset ZJU_PWD

# 2) 开启 CC98_PROXY 走隧道
sed -i 's#^CC98_PROXY: str = ""#CC98_PROXY: str = "socks5h://127.0.0.1:1080"#' \
  "$APP_DIR/cc98_hot_monitor.py" || true

# 3) 写 systemd 服务
cat > /etc/systemd/system/zju-connect.service <<EOF
[Unit]
Description=ZJU RVPN tunnel (zju-connect)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/zju-connect.env
ExecStart=$ZJU_BIN -protocol easyconnect -server rvpn.zju.edu.cn -port 443 -username $ZJU_ID
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/cc98-monitor.service <<EOF
[Unit]
Description=CC98 Hot Topic Monitor
After=network-online.target zju-connect.service
Requires=zju-connect.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY -u cc98_hot_monitor.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/cc98-dashboard.service <<EOF
[Unit]
Description=CC98 Monitor Dashboard
After=network-online.target cc98-monitor.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY -u cc98_dashboard.py --no-browser
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now zju-connect cc98-monitor cc98-dashboard

echo
echo "== 已启用三个服务,10 秒后验证 =="
sleep 10
systemctl is-active zju-connect cc98-monitor cc98-dashboard
echo "---- 隧道日志 ----"
journalctl -u zju-connect -n 6 --no-pager | tail -6
echo "---- 监控日志 ----"
tail -n 5 "$APP_DIR/cc98_hot_monitor.log" 2>/dev/null || true
echo
echo "后续: bash $APP_DIR/deploy/health_check.sh 可一键体检"
