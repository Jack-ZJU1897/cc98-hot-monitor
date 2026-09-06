#!/usr/bin/env bash
# ============================================================
# CC98 监控系统 · 一键体检
# 用法: bash health_check.sh        # 常规体检
#       bash health_check.sh -f     # 附赠一次飞书连接测试(群里会收到消息)
# 退出码 = 严重故障数(0=全部健康)
# 若安装目录不是 /opt/cc98,请改下面的 DIR / ZJU。
# ============================================================

DIR=/opt/cc98
ZJU=/opt/zju-connect
LOG=$DIR/cc98_hot_monitor.log
STATUS=$DIR/cc98_status.json
HISTORY=$DIR/cc98_hot_history.json
ENVFILE=/etc/zju-connect.env
SOCKS=127.0.0.1:1080
DASH_PORT=8788
PY=${DIR}/venv/bin/python
SEND_FEISHU=0
[ "$1" = "-f" ] && SEND_FEISHU=1

if [ -t 1 ]; then C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'; else C_G=""; C_R=""; C_Y=""; C_B=""; C_0=""; fi
OK()  { echo "  ${C_G}[OK]${C_0} $1"; }
BAD() { echo "  ${C_R}[FAIL]${C_0} $1"; }
WARN(){ echo "  ${C_Y}[WARN]${C_0} $1"; }
INFO(){ echo "  ${C_B}[INFO]${C_0} $1"; }

FAILS=0
echo "=================================================="
echo " CC98 监控体检  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="

# ---------- 1. systemd 服务 ----------
echo "[1] systemd 服务"
for svc in zju-connect cc98-monitor cc98-dashboard; do
  st=$(systemctl is-active "$svc" 2>/dev/null)
  en=$(systemctl is-enabled "$svc" 2>/dev/null)
  if [ "$st" = "active" ] && [ "$en" = "enabled" ]; then OK "$svc  (active+开机自启)"
  elif [ "$st" = "active" ]; then WARN "$svc active 但未设开机自启"
  else BAD "$svc 状态=$st(应为 active)"; FAILS=$((FAILS+1)); fi
done

# ---------- 2. 关键端口 ----------
echo "[2] 监听端口"
ss -ltn 2>/dev/null | grep -qE ':(1080)\s' && OK "SOCKS5 1080(隧道) 在听" || { BAD "SOCKS5 1080 未监听"; FAILS=$((FAILS+1)); }
ss -ltn 2>/dev/null | grep -qE ":${DASH_PORT}\s" && OK "面板 ${DASH_PORT} 在听" || { BAD "面板 ${DASH_PORT} 未监听"; FAILS=$((FAILS+1)); }

# ---------- 3. 隧道 → CC98 连通性 ----------
echo "[3] 隧道 → CC98 连通性"
code=$(curl -sS -m 20 -x "socks5h://$SOCKS" -o /tmp/cc98_health.json -w '%{http_code}' https://api.cc98.org/config/index 2>/dev/null)
if [ "$code" = "200" ]; then
  n=$("$PY" -c "import json;d=json.load(open('/tmp/cc98_health.json'));print(len(d.get('hotTopic') or []))" 2>/dev/null)
  first=$("$PY" -c "import json;d=json.load(open('/tmp/cc98_health.json'));h=d.get('hotTopic') or [];print(h[0]['title'][:30] if h else '')" 2>/dev/null)
  OK "经隧道抓取成功 HTTP 200, hotTopic $n 条 (第①条: $first)"
else
  BAD "经隧道抓取失败 HTTP=${code}(期望200) —— 隧道可能断了"; FAILS=$((FAILS+1))
fi
rm -f /tmp/cc98_health.json

# ---------- 4. 监控新鲜度 ----------
echo "[4] 监控运行新鲜度"
if [ -f "$STATUS" ]; then
  hb_out=$("$PY" - "$STATUS" <<'PYEOF' 2>/dev/null
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
print(int(s.get("heartbeat_ts", 0) or 0))
print("true" if s.get("ok") else "false")
print((s.get("strategy", "") or ""), "|", (s.get("updated_at", "") or ""), "|", (s.get("error") or ""))
PYEOF
)
  heartbeat=$(echo "$hb_out" | sed -n '1p')
  okflag=$(echo "$hb_out" | sed -n '2p')
  meta=$(echo "$hb_out" | sed -n '3p')
  now=$(date +%s)
  if [ -n "$heartbeat" ] && [ "$heartbeat" -gt 0 ] 2>/dev/null; then age=$((now-heartbeat)); else age=9999; fi
  INFO "最近更新: $meta | 本轮ok=$okflag | 心跳 ${age}s 前"
  if [ "$okflag" = "true" ] && [ "$age" -lt 150 ]; then OK "监控进程存活且最近一轮成功"
  elif [ "$age" -lt 150 ]; then BAD "进程活着但最近一轮失败,看日志"; FAILS=$((FAILS+1))
  else BAD "状态文件过旧(心跳${age}s前),监控可能已停"; FAILS=$((FAILS+1)); fi
else
  BAD "缺少 $STATUS(监控至少成功跑过一轮才会有)"; FAILS=$((FAILS+1))
fi

# ---------- 5. 历史数据 ----------
if [ -f "$HISTORY" ]; then
  ev=$("$PY" -c "import json;h=json.load(open('$HISTORY'));print(len(h.get('events',[])), h.get('last_fetch_time',''))" 2>/dev/null)
  INFO "历史事件 $ev"
else
  WARN "暂无历史文件(正常:基线建立后才有)"
fi

# ---------- 6. 飞书 ----------
echo "[5] 飞书"
fc=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' https://open.feishu.cn/ 2>/dev/null)
if [ -n "$fc" ] && [ "$fc" != "000" ]; then OK "open.feishu.cn 可达(HTTP $fc)"
else WARN "open.feishu.cn 不可达(HTTP=$fc),飞书推送会失败"; fi
if [ "$SEND_FEISHU" = "1" ] && [ -f "$DIR/cc98_hot_monitor.py" ]; then
  (cd "$DIR" && "$PY" -u cc98_hot_monitor.py --feishu-test) || WARN "飞书自检命令失败"
fi

# ---------- 7. 稳定性 ----------
echo "[6] 稳定性"
for svc in zju-connect cc98-monitor cc98-dashboard; do
  nr=$(systemctl show -p NRestarts --value "$svc" 2>/dev/null)
  if [ -n "$nr" ] && [ "$nr" -gt 0 ] 2>/dev/null; then WARN "$svc 曾自动重启 $nr 次(一般属自愈正常,频繁则需查)"
  else INFO "$svc 无意外重启"; fi
done
if [ -f "$LOG" ]; then
  errs=$(tail -n 500 "$LOG" | grep -c 'ERROR')
  if [ "$errs" -gt 0 ]; then WARN "日志尾部500行内有 $errs 条 ERROR(最后一条如下):"
    tail -n 500 "$LOG" | grep 'ERROR' | tail -1
  else INFO "日志近500行无 ERROR"; fi
fi

# ---------- 8. 资源 ----------
echo "[7] 资源"
INFO "项目目录占用: $(du -sh "$DIR" 2>/dev/null | cut -f1)"
df_line=$(df -h / | awk 'NR==2{print $5" 已用,剩 "$4}')
if [ "$(df -h / | awk 'NR==2{gsub("%","",$5); if($5>80) print 1}')" = "1" ]; then WARN "磁盘: $df_line (>80%)"
else OK "磁盘: $df_line"; fi
if [ -f "$ENVFILE" ] && [ "$(stat -c %a "$ENVFILE" 2>/dev/null)" = "600" ]; then INFO "凭据文件权限正常(600)"
else WARN "/etc/zju-connect.env 缺失或权限非600,请 chmod 600"; fi
if [ -x "$ZJU" ]; then INFO "zju-connect 二进制存在($ZJU)"
else BAD "缺少 $ZJU"; FAILS=$((FAILS+1)); fi

# ---------- 汇总 ----------
echo "=================================================="
if [ "$FAILS" = "0" ]; then echo " ${C_G}体检结果:全部健康 ✅${C_0}  无需处理"
else echo " ${C_R}体检结果:$FAILS 项严重故障,请按上方 [FAIL] 项排查${C_0}"; fi
echo "=================================================="
exit "$FAILS"
