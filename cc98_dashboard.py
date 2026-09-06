# -*- coding: utf-8 -*-
"""
==============================================================================
 cc98_dashboard.py —— CC98 热门话题监控 · 本地可视化面板
==============================================================================
 用途: 在浏览器里实时观察 cc98_hot_monitor.py 的运行情况:
       - 进程是否存活(心跳灯)、上次/下次检测时间、本轮成功/失败/错误
       - 当前「热门话题」Top10 榜单(标题可点击)
       - 新进榜事件流(时间、榜单、标题、链接)
       - 飞书(群机器人/多维表格)等配置状态
       - 运行日志尾部(自动刷新,ERROR 高亮)

 数据来源(同一目录下的文件,监控进程每轮/每 10 秒自动更新):
       - cc98_status.json        运行状态(心跳/最近一轮结果)
       - cc98_hot_history.json   榜单快照 + 事件历史
       - cc98_hot_monitor.log    运行日志
       - cc98_hot_monitor.py     配置(仅读取,用于展示飞书等开关)

 运行(纯 Python 标准库,无需额外安装):
       python cc98_dashboard.py            # 默认 http://127.0.0.1:8788
       python cc98_dashboard.py --port 9000
       python cc98_dashboard.py --no-browser   # 不自动打开浏览器
==============================================================================
"""

import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = "cc98_status.json"
HISTORY_FILE = "cc98_hot_history.json"
LOG_FILE = "cc98_hot_monitor.log"
MONITOR_FILE = "cc98_hot_monitor.py"


# ---------------------------------------------------------------------------
# 本地数据读取(全部只读,绝不修改监控进程的文件)
# ---------------------------------------------------------------------------

def _read_json(name: str) -> Dict[str, Any]:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_status() -> Dict[str, Any]:
    return _read_json(STATUS_FILE)


def read_history() -> Dict[str, Any]:
    return _read_json(HISTORY_FILE)


def read_log_tail(max_lines: int = 160) -> List[str]:
    path = os.path.join(HERE, LOG_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            lines = fp.readlines()
        return lines[-max_lines:]
    except Exception:
        return []


def read_monitor_config() -> Dict[str, Any]:
    """从主脚本源码里提取少量配置开关(字符串值只判断是否非空)。"""
    path = os.path.join(HERE, MONITOR_FILE)
    out: Dict[str, Any] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fp:
            text = fp.read()
    except Exception:
        return out

    def quoted(name: str) -> str:
        """取 name: str = "..." 的第一个引号内容(允许跨行括号写法)。"""
        idx = text.find(name)
        if idx < 0:
            return ""
        seg = text[idx:]
        m = re.search(r'=\s*\(?\s*["\']([^"\']*)["\']', seg)
        return m.group(1) if m else ""

    def int_val(name: str, default: int) -> int:
        m = re.search(rf"{name}\s*:\s*int\s*=\s*(\d+)", text)
        return int(m.group(1)) if m else default

    out["range_key"] = quoted("RANGE_KEY")
    out["interval_minutes"] = int_val("CHECK_INTERVAL_MINUTES", 10)
    out["strategy"] = quoted("FETCH_STRATEGY") or "auto"
    out["webhook_url"] = quoted("FEISHU_WEBHOOK_URL")
    out["webhook_secret"] = quoted("FEISHU_WEBHOOK_SECRET")
    out["bitable_app_id"] = quoted("FEISHU_BITABLE_APP_ID")
    out["bitable_wiki"] = quoted("FEISHU_BITABLE_WIKI_TOKEN")
    out["access_token"] = quoted("CC98_ACCESS_TOKEN")
    out["edge_profile"] = quoted("EDGE_USER_DATA_DIR")
    out["user_name"] = quoted("CC98_USERNAME")
    return out


def collect_data() -> Dict[str, Any]:
    """聚合一次 /api/data 需要的全部数据。"""
    status = read_status()
    history = read_history()
    cfg = read_monitor_config()

    now = time.time()
    heartbeat = float(status.get("heartbeat_ts") or 0)
    updated = float(status.get("updated_ts") or 0)
    alive = heartbeat > 0 and (now - heartbeat) < 120
    interval = cfg.get("interval_minutes", 10)

    # 距下次检测:以上一轮完成时间 + interval 估算(精确到分钟级别足够)
    next_in = None
    if updated > 0:
        remain = int(interval * 60 - (now - updated))
        next_in = max(remain, 0)

    events = list(reversed((history.get("events") or [])[-60:]))
    topics = history.get("last_topics") or []

    return {
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "alive": alive,
        "heartbeat_age_s": int(now - heartbeat) if heartbeat else None,
        "status": status,
        "last_fetch_time": history.get("last_fetch_time", ""),
        "topics": topics,
        "events": events,
        "log": read_log_tail(),
        "cfg": {
            "range_key": cfg.get("range_key", "current"),
            "interval_minutes": interval,
            "strategy": cfg.get("strategy", "auto"),
            "next_in_s": next_in,
            # 开关:非空字符串即视为已配置
            "feishu_webhook": bool(cfg.get("webhook_url")),
            "feishu_webhook_secret": bool(cfg.get("webhook_secret")),
            "feishu_bitable": bool(cfg.get("bitable_app_id")),
            "feishu_wiki": bool(cfg.get("bitable_wiki")),
            "has_token": bool(cfg.get("access_token")),
            "has_credentials": bool(cfg.get("user_name")),
            "edge_profile": bool(cfg.get("edge_profile")),
        },
    }


# ---------------------------------------------------------------------------
# HTML 页面
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CC98 热门话题监控面板</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f2f4f8; color: #1f2329; }
  header { background: #2b3a67; color: #fff; padding: 14px 22px;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .wrap { max-width: 1200px; margin: 18px auto; padding: 0 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
           gap: 12px; margin-bottom: 16px; }
  .card { background: #fff; border-radius: 10px; padding: 12px 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card .k { font-size: 12px; color: #8a9099; margin-bottom: 6px; }
  .card .v { font-size: 14px; line-height: 1.5; word-break: break-all; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         margin-right: 6px; vertical-align: middle; }
  .ok  { background: #34c759; }
  .bad { background: #ff3b30; }
  .warn{ background: #ff9500; }
  section { background: #fff; border-radius: 10px; padding: 14px 16px;
            margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  section h2 { font-size: 15px; margin: 0 0 10px; border-left: 4px solid #2b3a67;
               padding-left: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #eceff3; }
  th { color: #8a9099; font-weight: 500; }
  tr.rank1 td { background: #fff8e6; }
  a { color: #3370ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; }
  .b-on { background:#e8f7ee; color:#1a7f37; }
  .b-off{ background:#f1f2f4; color:#8a9099; }
  #log { background:#0f1419; color:#d7e2ec; border-radius:8px; padding:10px 12px;
         font: 12px/1.6 Consolas, monospace; height: 300px; overflow: auto; }
  #log .ERR,#log .CRITICAL { color:#ff6b6b; }
  #log .WARN { color:#ffd166; }
  #log .INFO { color:#9ad1ff; }
  .dim { color:#8a9099; font-size:12px; }
  footer { color:#8a9099; font-size:12px; text-align:center; padding: 10px 0 24px; }
</style>
</head>
<body>
<header>
  <h1>🏠 CC98 热门话题监控面板</h1>
  <span id="alive"><span class="dot ok"></span>…</span>
  <span id="clock" class="dim"></span>
</header>
<div class="wrap">

  <div class="cards" id="cards"></div>

  <section>
    <h2>当前榜单 Top10 <span id="rankmeta" class="dim"></span></h2>
    <table>
      <thead><tr><th style="width:50px">排名</th><th>标题</th>
             <th style="width:130px">帖子ID</th><th style="width:210px">链接</th></tr></thead>
      <tbody id="topics"></tbody>
    </table>
  </section>

  <section>
    <h2>新进榜事件流（最近）</h2>
    <table>
      <thead><tr><th style="width:150px">时间</th><th style="width:120px">榜单</th>
             <th>标题</th><th style="width:200px">链接</th></tr></thead>
      <tbody id="events"></tbody>
    </table>
  </section>

  <section>
    <h2>运行日志尾部（自动刷新）</h2>
    <pre id="log"></pre>
  </section>

  <footer>数据自动刷新中… 仅限本机访问 | 由 cc98_dashboard.py 提供</footer>
</div>

<script>
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function el(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function fmtAgo(sec){
  if (sec == null) return '—';
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.round(sec/60) + ' 分钟前';
  return Math.round(sec/3600) + ' 小时前';
}

function render(d){
  // 存活灯
  const aliveEl = document.getElementById('alive');
  aliveEl.innerHTML = '';
  aliveEl.appendChild(el('span','dot ' + (d.alive?'ok':'bad')));
  aliveEl.appendChild(document.createTextNode(d.alive ? '监控进程运行中' : '监控进程未运行'));
  document.getElementById('clock').textContent = '服务器时间 ' + d.server_time;

  const s = d.status || {};
  const ok = s.ok === true;
  const err = s.error || '';
  const cards = document.getElementById('cards');
  cards.innerHTML = '';
  const mk = (k, html) => { const c = el('div','card');
      c.appendChild(el('div','k',k)); const v = el('div','v'); v.innerHTML = html;
      c.appendChild(v); cards.appendChild(c); };
  mk('上次检测',
     (d.last_fetch_time || '—') + '<br><span class="dim">' +
     (s.strategy ? '策略 ' + esc(s.strategy) : '') +
     (s.top_count != null ? ' · 抓到 ' + s.top_count + ' 条' : '') +
     (s.new_count != null ? ' · 新增 ' + s.new_count : '') + '</span>');
  mk('本轮结果',
     ok ? '<span class="badge b-on">成功</span>'
        : (err ? '<span class="badge b-off">失败</span>' : '<span class="badge b-off">—</span>') +
          (err ? '<br><span style="color:#ff3b30">' + esc(err) + '</span>' : ''));
  const ni = d.cfg.next_in_s;
  mk('距下次检测', ni==null ? '—' : Math.round(ni/60) + ' 分钟 (约)');
  mk('飞书群机器人', d.cfg.feishu_webhook
      ? '<span class="badge b-on">已启用</span>' +
        (d.cfg.feishu_webhook_secret ? '<br><span class="dim">已加签</span>' : '')
      : '<span class="badge b-off">未启用</span>');
  mk('飞书多维表格', d.cfg.feishu_bitable
      ? '<span class="badge b-on">已启用</span>' +
        (d.cfg.feishu_wiki ? '<br><span class="dim">wiki 节点</span>' : '')
      : '<span class="badge b-off">未启用</span>');
  mk('访问凭据',
     '<span class="badge ' + (d.cfg.has_token ? 'b-on' : 'b-off') + '">' +
     (d.cfg.has_token ? '已配置 Token' : '匿名') + '</span>' +
     (d.cfg.edge_profile ? '<br><span class="dim">Edge 登录态可用</span>' : ''));
  mk('设置',
     '榜单: <b>' + esc(d.cfg.range_key) + '</b> · 间隔 ' +
     d.cfg.interval_minutes + ' 分钟<br>策略: ' + esc(d.cfg.strategy));

  // 榜单表
  const tb = document.getElementById('topics');
  tb.innerHTML = '';
  (d.topics || []).forEach(t => {
    const tr = el('tr', t.rank === 1 ? 'rank1' : '');
    const tdR = el('td', '', String(t.rank));
    const tdT = el('td');
    if (t.url) { const a = el('a','', t.title); a.href = t.url; a.target='_blank'; tdT.appendChild(a); }
    else tdT.textContent = t.title || '';
    const tdId = el('td','', t.id != null ? String(t.id) : '—');
    const tdU = el('td');
    if (t.url) { const a2 = el('a','', t.url.replace('https://www.cc98.org','')); a2.href=t.url; a2.target='_blank'; tdU.appendChild(a2); }
    else tdU.textContent = '—';
    tr.append(tdR, tdT, tdId, tdU);
    tb.appendChild(tr);
  });
  document.getElementById('rankmeta').textContent =
    '(共 ' + (d.topics || []).length + ' 条 · 最近更新 ' + (d.last_fetch_time||'—') + ')';

  // 事件流
  const ev = document.getElementById('events');
  ev.innerHTML = '';
  const evs = d.events || [];
  if (!evs.length) {
    const tr = el('tr'); const td = el('td','','暂无新进榜事件'); td.colSpan = 4;
    tr.appendChild(td); ev.appendChild(tr);
  }
  evs.forEach(e => {
    const tr = el('tr');
    const t1 = el('td','dim', e.time || '');
    const t2 = el('td','', e.range || '');
    const t3 = el('td');
    const ns = e.new_topics || [];
    t3.textContent = ns.map(x => '#' + x.rank + ' ' + (x.title || '')).join('\n');
    const t4 = el('td');
    const links = ns.filter(x => x.url).map(x => x.url);
    links.forEach((u, i) => { const a = el('a','', i + 1 + '.' + u.replace('https://www.cc98.org',''));
        a.href = u; a.target = '_blank'; a.style.display = 'block'; t4.appendChild(a); });
    tr.append(t1, t2, t3, t4); ev.appendChild(tr);
  });

  // 日志
  const logEl = document.getElementById('log');
  logEl.textContent = (d.log || []).join('');
  logEl.scrollTop = logEl.scrollHeight;
}

async function tick(){
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    render(d);
  } catch (e) { /* 服务器尚未就绪时静默重试 */ }
}
tick();
setInterval(tick, 15000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静默访问日志,避免刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html")
            elif path == "/api/data":
                data = collect_data()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            try:
                self._send(500, str(exc).encode("utf-8"), "text/plain")
            except Exception:
                pass


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="CC98 热门话题监控 · 本地可视化面板")
    ap.add_argument("--port", type=int, default=8788, help="监听端口(默认 8788)")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(默认仅本机 127.0.0.1)")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 60)
    print(" CC98 监控面板已启动: " + url)
    print(" 数据文件目录: " + HERE)
    print(" 按 Ctrl+C 退出;仅本机可访问。")
    print("=" * 60)

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url, new=2)
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已退出")
        srv.server_close()


if __name__ == "__main__":
    main()
