# -*- coding: utf-8 -*-
"""
==============================================================================
 CC98 论坛「热门话题」监控 Agent
==============================================================================
 监控目标 : 浙大 CC98 论坛首页「热门话题」板块默认列表 —— 即"当前热门"(十大热门话题)
            https://www.cc98.org/
 输出内容 : 与上次抓取相比"新进入"榜单的帖子(序号 + 标题 + 链接)
 持久化   : 历史记录保存在 cc98_hot_history.json

 ── 依赖安装 ─────────────────────────────────────────────────────────────
     Python 3.9+
     pip install requests beautifulsoup4 schedule
     # 方案二(浏览器渲染抓取)另需:
     pip install selenium   # Selenium 4.6+ 会自动下载/管理浏览器驱动(Edge/Chrome)

 ── 重要背景(已核实) ─────────────────────────────────────────────────────
     新版 CC98 是一个 React 单页应用(SPA),首页"热门话题"数据是页面加载后
     由 JS 异步请求 JSON 接口渲染出来的 —— 直接用 requests 抓 www.cc98.org
     的 HTML 只能拿到空壳(方案一因此作为"备选+诊断"存在)。
     榜单数据来自官方只读接口(已按线上 v3.5 前端代码核实):
         ◆ 当前热门 —— 首页「热门话题」板块下方默认展示的"十大"列表:
              GET https://api.cc98.org/config/index  取其 JSON 的 hotTopic 字段(10 条)
         ◆ 本周 / 本月 / 历史上的今天 —— 板块右上角链接对应的榜单页:
              GET https://api.cc98.org/topic/hot-weekly    本周热门
              GET https://api.cc98.org/topic/hot-monthly   本月热门
              GET https://api.cc98.org/topic/hot-history   历史上的今天
         ◆ 今日热门(旧版页面 Tab):
              GET https://api.cc98.org/topic/hot           今日热门
     每条含 id / title / boardName / authorName / replyCount / hitCount 等。
     因此本脚本提供三种抓取策略,互相兜底:
         ① strategy="api"      —— 纯 requests 直连官方 JSON 接口(推荐,最稳)
         ② strategy="selenium" —— 方案二:真实浏览器渲染 SPA 后抓取(见注释)
         ③ strategy="requests" —— 方案一:requests+BeautifulSoup 抓首页 HTML
                                     (仅当站点提供服务端渲染/静态内容时可用)
         strategy="auto"       —— 自动选择:api → selenium → requests
     提示:www.cc98.org / api.cc98.org 均需在校内网或 RVPN/WebVPN 环境下访问。

 ── 使用方法 ─────────────────────────────────────────────────────────────
     0) (推荐)先运行配套脚本 python cc98_grab_token.py:
        它会自动打开一个独立的 Edge/Chrome 窗口,你登录一次 CC98 后,
        自动抓取 access_token 写回本脚本配置,并建立可长期复用的登录配置目录;
     1) 或手动把登录 Cookie / (可选)Access Token / 论坛账号密码填进下方配置区;
     2) 命令行运行:
           python cc98_hot_monitor.py            # 先立刻监测一次,然后定时循环
           python cc98_hot_monitor.py --once     # 只监测一次后退出(便于调试)
           python cc98_hot_monitor.py --test     # 抓取+打印当前榜单,不比对不落盘
           python cc98_hot_monitor.py --interval 10   # 覆盖定时间隔(分钟)
==============================================================================
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:  # 让纯 --help 在缺依赖时也能工作
    import requests
    import schedule
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    requests = schedule = BeautifulSoup = None  # type: ignore
    print("[警告] 缺少依赖 requests / beautifulsoup4 / schedule，请先执行:\n"
          "       pip install requests beautifulsoup4 schedule")


# ============================================================================
# ① 配置区 —— 运行前请按注释修改这里
# ============================================================================

# ---------------------------------------------------------------------------
# 【手动获取 Cookie】(方案一/方案二使用,登录态来源)
# 1. 连接校园网或 RVPN,用浏览器(Chrome/Edge)打开 https://www.cc98.org/ 并登录
#    (CAS 登录无法脚本自动化,所以需要你手动登录一次);
# 2. 按 F12 打开开发者工具 → Network(网络) 面板 → 刷新页面;
# 3. 点击第一个 Document 请求(名称通常为 www.cc98.org)→ Headers → 往下找到
#    "Request Headers" 里的 Cookie: 一行,复制其后的全部内容(很长,要复制完整);
# 4. 粘贴到下面 CC98_COOKIE 的三引号字符串里,形如:
#    ".AspNetCore.Cookies=CfDJ8...; another=xxx; ..."
# 注意: Cookie 会过期,过期后脚本会明确提示你重新复制一次。
# ---------------------------------------------------------------------------
CC98_COOKIE: str = ""

# ---------------------------------------------------------------------------
# 【可选】Access Token(官方 JSON 接口方案建议填写,比 Cookie 更可靠)
# 获取方式与复制 Cookie 类似:
# 1. 浏览器登录 cc98.org 后 F12 → Network;
# 2. 随便点一个对 api-v2.cc98.org / api.cc98.org 的 XHR 请求(比如 hot-weekly);
# 3. 复制它的 Request Headers 中 Authorization: 一行的值(形如 "Bearer eyJhbGci..." 整串)。
# 或者 Console 里执行 localStorage.getItem('access_token'),若结果以 "str-" 开头
# 需去掉前缀 "str-" 后再粘贴(存的是 "Bearer xxx")。
# 留空 "" 则尝试匿名访问接口(部分榜单匿名可见),不行时再走 Selenium。
# ---------------------------------------------------------------------------
CC98_ACCESS_TOKEN: str = ""

# ---------------------------------------------------------------------------
# 【可选】论坛账号密码自动登录(OAuth2 密码模式,与官方前端同一 public client)
# 如果你知道 CC98 论坛自己的 用户名+密码(注册/设置过独立论坛密码的账号),
# 填上后脚本会自动换取 access_token,无需每次手动复制,也不依赖 Cookie。
# 不知道/不想用就都留空 ""。
# 注意: 该账号密码是"CC98 论坛密码"(可能和 CAS/统一身份认证密码不同)。
# ---------------------------------------------------------------------------
CC98_USERNAME: str = ""
CC98_PASSWORD: str = ""

# ---------------------------------------------------------------------------
# 抓取策略: "auto"(推荐) | "api" | "selenium" | "requests"
#   api      —— requests 直连官方 JSON 接口(最轻量稳定,推荐能用就用)
#   selenium —— 真实浏览器渲染 SPA 后抓取(需要本机装 Chrome + selenium)
#   requests —— requests + BeautifulSoup 抓首页 HTML(SPA 下通常抓不到,见方案一注释)
#   auto     —— 依次尝试 api → selenium → requests
# ---------------------------------------------------------------------------
FETCH_STRATEGY: str = "auto"

# 监测的榜单: "current"(当前热门=首页热门话题板块默认列表,默认,推荐)
#             | "week"(本周热门) | "month"(本月热门) | "history"(历史上的今天)
RANGE_KEY: str = "current"

# 每次取榜单前多少条(首页默认展示 Top10)
TOP_N: int = 10

# 定时检查间隔(分钟)。CC98 有反爬,建议 >= 5 分钟,默认 10 分钟
CHECK_INTERVAL_MINUTES: int = 10

# 网络超时(秒)、失败重试次数、重试退避基数
HTTP_TIMEOUT: Tuple[float, float] = (10.0, 20.0)
HTTP_RETRIES: int = 3
RETRY_BACKOFF: float = 2.0  # 每次重试 sleep = backoff ** attempt

# 请求伪装(UA + 来源,礼貌一点)
FAKE_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 站点地址与官方 API 地址(线上前端 /static/config.json 的 apiUrl=api.cc98.org;
# api-v2.cc98.org 为旧版兼容地址,均可用,按序自动切换)
HOME_URL: str = "https://www.cc98.org/"
API_HOSTS: List[str] = ["https://api.cc98.org", "https://api-v2.cc98.org"]

# 榜单 key -> (API 路径, 中文名)   表: 见上方 docstring
RANGE_TABLE: Dict[str, Tuple[str, str]] = {
    # "当前热门"= 首页热门话题板块下方默认展示的十大列表(取其 JSON 的 hotTopic 字段)
    "current": ("config/index",      "当前热门"),
    "today":   ("topic/hot",         "今日热门"),
    "week":    ("topic/hot-weekly",  "本周热门"),
    "month":   ("topic/hot-monthly", "本月热门"),
    "history": ("topic/hot-history", "历史上的今天"),
}
RANGE_LABEL: str = RANGE_TABLE[RANGE_KEY][1]

# ---------------------------------------------------------------------------
# 【可选】本地 SOCKS5 代理访问 CC98 —— 服务器在校外时,用 zju-connect 之类的
# 校网隧道把 CC98 流量送进校园网(绕开被 webvpn 拒绝机房 IP 的问题)。
#   1) 服务器上跑 zju-connect(浙大 RVPN 开源客户端): ./zju-connect ...
#      它会在本机 127.0.0.1:1080 起一个 SOCKS5;
#   2) 这里填: CC98_PROXY = "socks5h://127.0.0.1:1080"
#      (socks5h 的 h 表示域名解析也走代理,这样 api.cc98.org 能解析到校网内 10.x)
#   3) 需要支持库: pip install pysocks
#   留空 "" = 直连(校园网内/本地本机时用)。
# ---------------------------------------------------------------------------
CC98_PROXY: str = ""

# 日志: 级别 DEBUG/INFO/WARNING/ERROR;是否同时输出到控制台
LOG_LEVEL: str = "INFO"
LOG_FILE: str = "cc98_hot_monitor.log"
LOG_TO_CONSOLE: bool = True

# 历史/事件记录 JSON 文件(持久化)
HISTORY_FILE: str = "cc98_hot_history.json"
# 运行状态文件(每轮/心跳写入,供可视化面板 cc98_dashboard.py 读取)
STATUS_FILE: str = "cc98_status.json"
# 可选:Access Token 自动登录的本地缓存文件
TOKEN_CACHE_FILE: str = "cc98_token_cache.json"

# 首次运行是否把整个榜单当作"新增"通知(False = 仅建立基线,不出通知)
NOTIFY_FIRST_RUN_AS_NEW: bool = False
# True = 同一个帖子一旦进过历史榜单(所有时间),之后再次进入都不再重复提醒;
# False = 严格按需求"只与上次抓取对比",掉榜后再上会再次提醒
ALL_TIME_SUPPRESS: bool = False

# Selenium 方案配置
# 用哪个浏览器渲染抓取: "edge"(推荐,Win 自带 Edge) 或 "chrome"
SELENIUM_BROWSER: str = "edge"
# 浏览器"独立登录配置目录"。填了可免重复登录:
#   推荐先运行配套脚本 cc98_grab_token.py,它会自动建立一个独立 Edge/Chrome 配置目录
#   并帮你登录一次,然后把该目录路径自动写回这里(目录示例: r"D:\cc98_edge_profile")。
# 留空时,则每次把 CC98_COOKIE 注入临时会话,若仍被登录墙挡住会弹窗等你手动登录。
EDGE_USER_DATA_DIR: str = ""
CHROME_USER_DATA_DIR: str = ""
SELENIUM_HEADLESS: bool = False         # True 则无头运行(首次登录时请保持 False 便于手动登录)
SELENIUM_WAIT_SECONDS: int = 15         # 等待 SPA 渲染/接口返回的最长秒数
INJECT_COOKIE_ALWAYS: bool = True       # 每次启动 selenium 都注入 CC98_COOKIE

# 调试:True 时把 requests 抓到的首页 HTML 存到本地,便于排查页面结构
DEBUG_DUMP_HTML: bool = False

# ---------------------------------------------------------------------------
# 【可选】飞书群机器人 Webhook 推送 —— 发现新帖时发消息到飞书群
# 开启步骤:
#   1) 飞书 → 目标群 → 右上角设置 → 群机器人 → 添加机器人 → 自定义机器人;
#   2) 复制"Webhook 地址"填入 FEISHU_WEBHOOK_URL(留空 "" 表示不启用);
#   3) 若添加时勾选了"签名校验",把"签名密钥"填入 FEISHU_WEBHOOK_SECRET。
# ---------------------------------------------------------------------------
FEISHU_WEBHOOK_URL: str = ""
FEISHU_WEBHOOK_SECRET: str = ""

# ---------------------------------------------------------------------------
# 【可选】飞书多维表格(Bitable)留档 —— 每轮把"新进入"的帖子追加为一行记录
# 开启步骤:
#   1) open.feishu.cn → 开发者后台 → 创建"企业自建应用",记下 App ID / App Secret;
#   2) 权限管理开通: bitable:app(查看、编辑多维表格),并发布版本;
#   3) 创建一张多维表格,分享链接形如:
#        https://xxx.feishu.cn/base/{app_token}?table={table_id}&view=vew...
#      把 {app_token} 填入 FEISHU_BITABLE_APP_TOKEN;
#      {table_id} 填入 FEISHU_BITABLE_TABLE_ID(留空则自动使用该库第一张表);
#   4) 建好表头(建议全部用"文本"列,名称要与 FEISHU_BITABLE_FIELD_MAP 的"值"一致):
#       检测时间 | 榜单 | 序号 | 帖子ID | 标题 | 链接 | 作者
# ---------------------------------------------------------------------------
FEISHU_BITABLE_APP_ID: str = ""
FEISHU_BITABLE_APP_SECRET: str = ""
FEISHU_BITABLE_APP_TOKEN: str = ""
# 若你给的是知识库(wiki)分享链接(形如 .../wiki/XXXXX?...),把其中的节点 token XXXXX
# 填到 FEISHU_BITABLE_WIKI_TOKEN,脚本会自动解析成对应的多维表格 app_token;
# 若是多维表格直链(形如 .../base/{app_token}?...),则填 FEISHU_BITABLE_APP_TOKEN。
FEISHU_BITABLE_WIKI_TOKEN: str = ""
FEISHU_BITABLE_TABLE_ID: str = ""
FEISHU_BITABLE_FIELD_MAP: Dict[str, str] = {
    # 键:本脚本内部字段;值:你多维表格里的实际列名
    "time": "检测时间", "range": "榜单", "rank": "序号", "id": "帖子ID",
    "title": "标题", "link": "链接", "author": "作者",
}

# OAuth 公共客户端参数(取自官网前端 src/utils/logIn.ts,公开只读用途,勿改)
OIDC_TOKEN_URL: str = "https://openid.cc98.org/connect/token"
OAUTH_CLIENT_ID: str = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
OAUTH_CLIENT_SECRET: str = "8b53f727-08e2-4509-8857-e34bf92b27f2"
OAUTH_SCOPE: str = "cc98-api openid offline_access"

# ---------------------------------------------------------------------------
# 【可选】WebVPN 通道 —— 服务器在校外、无法直连 api.cc98.org 时走浙大 WebVPN 中转
#   开启条件: WEBVPN_ENABLED=True 且填好统一身份认证账号密码(学号/工号+密码)。
#   模式 WEBVPN_MODE:
#     auto    —— 先尝试直连,网络失败/超时后自动改走 WebVPN(推荐)
#     direct  —— 永远只直连(忽略 WebVPN)
#     vpn     —— 永远只走 WebVPN
#   注意:
#     1) 首次在新设备登录 WebVPN 可能需要短信/推送"确认"或验证码:
#        先在任意浏览器登录一次 https://webvpn.zju.edu.cn 完成确认,之后再让脚本登录;
#     2) 需要 pycryptodome(仅此功能用到): pip install pycryptodome
#     3) 也可手动把浏览器里 webvpn.zju.edu.cn 的 Cookie 串填到 WEBVPN_COOKIE_STRING
#        (此时可不填账号密码),会话 Cookie 会缓存到 WEBVPN_COOKIE_FILE 自动复用。
# ---------------------------------------------------------------------------
WEBVPN_ENABLED: bool = False
WEBVPN_HOST: str = "https://webvpn.zju.edu.cn"
WEBVPN_USERNAME: str = ""
WEBVPN_PASSWORD: str = ""
WEBVPN_MODE: str = "auto"  # auto | direct | vpn
WEBVPN_COOKIE_STRING: str = ""
WEBVPN_COOKIE_FILE: str = "cc98_webvpn_cookies.json"


# ============================================================================
# ② 日志与通用工具
# ============================================================================

logger = logging.getLogger("cc98_monitor")


def setup_logging() -> None:
    """配置日志:文件(UTF-8, Rotating) + 可选控制台。"""
    # 兼容 Windows 旧终端,尽量用 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        except Exception:
            pass

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    try:
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:  # 日志文件写不了不能让它崩
        print(f"[警告] 初始化日志文件失败(不影响运行): {exc}")

    if LOG_TO_CONSOLE:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)


def now_str() -> str:
    """当前时间字符串,用于通知与落盘。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_cookie(raw: str) -> Dict[str, str]:
    """
    把浏览器里复制出来的 Cookie 字符串解析成 dict。
    支持两种输入:
      1) 原始请求头串: "k1=v1; k2=v2; ..."
      2) JSON 字符串:  '{"k1": "v1", ...}'
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    cookies: Dict[str, str] = {}
    for seg in raw.split(";"):
        seg = seg.strip()
        if "=" in seg:
            k, _, v = seg.partition("=")
            if k.strip():
                cookies[k.strip()] = v.strip()
    return cookies


def clean_text(text: Optional[str]) -> str:
    """清洗标题文本:去空白、换行、控制字符。"""
    if not text:
        return ""
    text = re.sub(r"[\r\n\t]+", " ", str(text))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def ordinal(n: int) -> str:
    """1~10 输出带圈数字序号,其余退化为普通数字。"""
    circles = "①②③④⑤⑥⑦⑧⑨⑩"
    return circles[n - 1] if 1 <= n <= len(circles) else f"({n})"


# ---------------- 自定义异常(便于上层做"是否登录失效"判断) ----------------

class MonitorError(Exception):
    """监控器通用异常基类。"""


class CookieExpiredError(MonitorError):
    """Web 页面登录墙 / Cookie 过期。"""


class AuthRequiredError(MonitorError):
    """JSON 接口返回 401/403,需要有效登录凭据。"""


class StrategyUnavailableError(MonitorError):
    """所选抓取策略在当前环境不可用(缺库/缺浏览器等)。"""


class FetchEmptyError(MonitorError):
    """抓到了页面/响应,但没有解析出任何榜单条目。"""


# ---------------- 带重试的 HTTP GET ----------------

def http_get_text(url: str, *, cookie: Dict[str, str] = None,
                  token_header: str = "", timeout: Tuple[float, float] = HTTP_TIMEOUT,
                  retries: int = HTTP_RETRIES) -> Any:
    """
    带指数退避重试的 GET。重试范围:网络异常与 5xx(服务端抖动);
    4xx 不重试(那是逻辑问题,由调用方判断,如登录失效)。
    (返回类型写 Any 而非 requests.Response,避免依赖缺失时注解求值报错。)
    """
    if requests is None:  # pragma: no cover
        raise StrategyUnavailableError("缺少 requests 库,请先 pip install requests")

    headers = dict(FAKE_HEADERS)
    if cookie:
        # Cookie 只拼进 header(不塞 session.cookies,避免串到其他域名)
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie.items())
    if token_header:
        headers["Authorization"] = token_header

    # 可选 SOCKS5 代理(经 zju-connect 校网隧道);proxies=None=直连
    proxies = None
    if CC98_PROXY.strip():
        # "socks5h://" 让域名解析也走代理(才能解析到校网内 10.x 地址)
        proxies = {"http": CC98_PROXY.strip(), "https": CC98_PROXY.strip()}

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
            if 500 <= resp.status_code < 600:
                raise requests.exceptions.ConnectionError(
                    f"HTTP {resp.status_code} 服务端错误,第 {attempt} 次"
                )
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            logger.warning("请求失败(%s) %s —— 第 %d/%d 次",
                           exc.__class__.__name__, url, attempt, retries)
            if attempt < retries:
                time.sleep(min(RETRY_BACKOFF ** attempt, 15) + random.random())
    raise MonitorError(f"网络请求多次重试仍失败: {url} -> {last_exc}")


# ============================================================================
# ③ 核心抓取:三种策略 + 结果规范化
# ============================================================================

# 规范化后的单条帖子结构:
#   {"rank": int, "id": int/str, "title": str,
#    "url": str, "author": str, "reply_count": int, "board_id": int}


def make_url(topic_id) -> str:
    """由帖子 id 构造论坛页面链接(与官网路由一致)。"""
    return f"https://www.cc98.org/topic/{topic_id}"


def normalize_items(raw_list: Any, range_key: str) -> List[Dict[str, Any]]:
    """
    把 API 返回的 JSON 数组规范化成统一字典列表,并对字段做防御性读取。
    raw_list 每项至少含: id, title (见官网 @cc98/api 的 ITopic 类型)。
    """
    items: List[Dict[str, Any]] = []
    if not isinstance(raw_list, list):
        return items
    for i, it in enumerate(raw_list, start=1):
        if not isinstance(it, dict):
            continue
        tid = it.get("id")
        title = clean_text(it.get("title"))
        if tid is None or not title:  # 缺关键字段就跳过,不让一条坏数据毁掉整轮
            continue
        try:
            reply = int(it.get("replyCount", it.get("reply_count", 0)) or 0)
        except (TypeError, ValueError):
            reply = 0
        items.append({
            "rank": i,
            "id": tid,
            "title": title,
            "url": make_url(tid),
            "author": clean_text(it.get("userName") or it.get("authorName") or "[匿名]"),
            "reply_count": reply,
            "board_id": it.get("boardId"),
        })
    return items[:TOP_N]


def strip_token_prefix(raw_token: str) -> str:
    """localStorage 取出的 token 可能带 'str-' 前缀,去掉。"""
    raw_token = (raw_token or "").strip()
    if raw_token.startswith("str-"):
        raw_token = raw_token[4:]
    if raw_token.startswith("obj-"):
        # obj- 说明存的是对象,不是纯字符串,无法直接当 header 用
        return ""
    return raw_token


# ---------------- 3.1 Access Token 管理(自动登录可选) ----------------

class TokenManager:
    """
    管理官方 JSON 接口所需的 Bearer Token。
    优先级:手动 ACCESS_TOKEN > 本地缓存 > refresh_token 续期 > 账号密码换取。
    全都没有 → 匿名访问(header 为空)。
    """

    def __init__(self, username: str, password: str, manual_token: str) -> None:
        self.username = username.strip()
        self.password = password
        self.manual = strip_token_prefix(manual_token)
        self._cache: Dict[str, Any] = {}
        self._load_cache()

    # ---------- 缓存读写 ----------
    def _load_cache(self) -> None:
        try:
            if os.path.exists(TOKEN_CACHE_FILE):
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as fp:
                    self._cache = json.load(fp)
        except Exception as exc:
            logger.debug("读取 token 缓存失败(忽略): %s", exc)
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as fp:
                json.dump(self._cache, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("写入 token 缓存失败(不影响本轮运行): %s", exc)

    # ---------- OAuth 密码/刷新 ----------
    def _post_token(self, form: Dict[str, str]) -> Optional[Dict[str, Any]]:
        if requests is None:  # pragma: no cover
            return None
        data = {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            **form,
        }
        try:
            resp = requests.post(
                OIDC_TOKEN_URL, data=data, headers={"User-Agent": FAKE_HEADERS["User-Agent"]},
                timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("换取 token 网络失败: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("换取 token 失败: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.warning("换取 token 响应不是 JSON")
            return None
        if not data.get("access_token"):
            return None
        now = time.time()
        return {
            "token_type": data.get("token_type", "Bearer"),
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": now + int(data.get("expires_in", 3600)) - 60,  # 提前 60s 过期
        }

    def _obtain(self) -> Dict[str, Any]:
        """依次尝试:手动 token→缓存未过期→refresh→账号密码。"""
        # 1) 手动粘贴的 token
        if self.manual:
            logger.info("使用配置中手动粘贴的 Access Token")
            return {"token_type": "", "access_token": self.manual,
                    "refresh_token": "", "expires_at": time.time() + 3600}

        cached = self._cache
        now = time.time()
        # 2) 缓存未过期
        if cached.get("access_token") and cached.get("expires_at", 0) > now:
            return cached

        # 3) refresh_token 续期
        refresh = cached.get("refresh_token") or ""
        if refresh:
            token = self._post_token({
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": OAUTH_SCOPE,
            })
            if token:
                logger.info("使用 refresh_token 自动续期成功")
                self._cache = token
                self._save_cache()
                return token
            logger.warning("refresh_token 续期失败,尝试账号密码登录")

        # 4) 账号密码(OAuth2 密码模式)
        if self.username and self.password:
            token = self._post_token({
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": OAUTH_SCOPE,
            })
            if token:
                logger.info("账号密码自动登录成功")
                self._cache = token
                self._save_cache()
                return token

        # 5) 匿名(不加 Authorization,部分只读接口允许)
        return {"token_type": "", "access_token": "", "refresh_token": "",
                "expires_at": 0}

    def auth_header(self) -> str:
        """返回可直接放进请求头的 Authorization 值,匿名时为空串。"""
        token = self._obtain()
        at = token.get("access_token") or ""
        if not at:
            return ""
        token_type = token.get("token_type") or "Bearer"
        return f"{token_type} {at}" if not at.lower().startswith("bearer ") else at


# ---------------- WebVPN 通道(校外服务器访问浙大内网用) ----------------

class WebVpnError(MonitorError):
    """WebVPN 登录/请求失败。"""


def webvpn_aes_encrypt(plaintext: str, key: str, iv: str) -> str:
    """AES-128-CFB 加密并转小写 hex(与 CC98-Desktop / cc98-mcp 兼容)。
    需要 pycryptodome: pip install pycryptodome"""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise WebVpnError("缺少 pycryptodome —— WebVPN 需要它加密密码,请执行: "
                          "pip install pycryptodome")
    key_b = (key + " " * 16)[:16].encode("utf-8")
    iv_b = (iv + " " * 16)[:16].encode("utf-8")
    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    if pad_len != 16:  # 与 TS 一致:只有不足整块时才补零
        data += b"\x00" * pad_len
    cipher = AES.new(key_b, AES.MODE_CFB, iv_b, segment_size=128)
    return cipher.encrypt(data).hex().lower()


def webvpn_build_password(prefix: str, plaintext: str) -> str:
    """BuildPassword:ascii(prefix)的 hex + 加密结果截断到 2*len(明文) 位 hex。"""
    prefix_hex = prefix.encode("ascii").hex()
    full = webvpn_aes_encrypt(plaintext, prefix, prefix)
    return prefix_hex + full[: 2 * len(plaintext)]


class WebVpnClient:
    """
    浙大 WebVPN 客户端(requests 实现,逻辑对齐 cc98-mcp / CC98-CLI):
      - convert_url: 把内网 URL 转成 webvpn.zju.edu.cn 的加密路径;
      - 登录:GET /login 取 _csrf → AES 加密密码 → POST /do-login;
      - 会话 Cookie 缓存到文件,重启后可复用,失效自动重新登录。
    """

    def __init__(self) -> None:
        self.host = WEBVPN_HOST.rstrip("/")
        self.enabled = bool(WEBVPN_ENABLED)
        self.session = requests.Session() if requests is not None else None
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
            "Referer": self.host,
        })
        self.logged_in = False
        self._load_cookies()

    # ---------- Cookie 存取 ----------
    def _load_cookies(self) -> None:
        jar: Dict[str, str] = {}
        try:
            if WEBVPN_COOKIE_FILE and os.path.exists(WEBVPN_COOKIE_FILE):
                with open(WEBVPN_COOKIE_FILE, "r", encoding="utf-8") as fp:
                    saved = json.load(fp)
                if isinstance(saved, dict):
                    jar.update({str(k): str(v) for k, v in saved.items()})
        except Exception as exc:
            logger.debug("读取 WebVPN Cookie 文件失败(忽略): %s", exc)
        if not jar:  # 手动粘贴的 Cookie 串(兜底,不需要账号密码)
            jar = parse_cookie(WEBVPN_COOKIE_STRING)
        if jar:
            self.session.cookies.update(jar)
            self.logged_in = True

    def _save_cookies(self) -> None:
        try:
            if WEBVPN_COOKIE_FILE:
                with open(WEBVPN_COOKIE_FILE, "w", encoding="utf-8") as fp:
                    json.dump(dict(self.session.cookies), fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.debug("保存 WebVPN Cookie 失败(忽略): %s", exc)

    # ---------- 登录 ----------
    def login(self) -> None:
        """账号密码登录 WebVPN。需统一身份认证账号 + (首次)浏览器确认一次。"""
        if not (WEBVPN_USERNAME and WEBVPN_PASSWORD):
            raise WebVpnError("WebVPN 未配置:请填 WEBVPN_USERNAME / WEBVPN_PASSWORD"
                              "(或提供 WEBVPN_COOKIE_STRING)")
        try:
            # 1) GET /login 提取 _csrf
            page = self.session.get(f"{self.host}/login", timeout=(10, 20))
            m = re.search(r'name="_csrf"[^>]*value="([^"]+)"', page.text)
            if not m:
                raise WebVpnError("无法从 WebVPN 登录页提取 _csrf(可能已登录或页面结构变化)")
            csrf = m.group(1)

            # 2) AES 加密密码并提交
            enc_pwd = webvpn_build_password("wrdvpnisawesome!", WEBVPN_PASSWORD)
            form = {
                "_csrf": csrf,
                "auth_type": "local",
                "sms_code": "",
                "captcha": "",
                "needCaptcha": "false",
                "captcha_id": "",
                "username": WEBVPN_USERNAME,
                "password": enc_pwd,
            }
            resp = self.session.post(f"{self.host}/do-login", data=form,
                                     timeout=(10, 30))
            try:
                result = resp.json()
            except ValueError:
                raise WebVpnError(f"WebVPN 登录响应异常: HTTP {resp.status_code},"
                                  f" 前 120 字: {resp.text[:120]}")
        except requests.exceptions.RequestException as exc:
            raise WebVpnError(f"WebVPN 登录网络失败: {exc}")

        if result.get("success"):
            self.logged_in = True
            self._save_cookies()
            logger.info("WebVPN 登录成功(%s)", WEBVPN_USERNAME)
            return
        err = result.get("error") or result.get("message") or "未知错误"
        if err == "NEED_CONFIRM":
            # 尝试直接确认一次(部分账号首次登录会要求二次确认)
            try:
                c = self.session.post(f"{self.host}/do-confirm-login", data="",
                                      timeout=(10, 20))
                if c.json().get("success"):
                    self.logged_in = True
                    self._save_cookies()
                    logger.info("WebVPN 二次确认成功")
                    return
            except Exception:
                pass
            raise WebVpnError(
                "WebVPN 需要二次确认:请在任意浏览器登录一次 webvpn.zju.edu.cn"
                "完成确认后重试本脚本(之后自动登录即可通过)"
            )
        if err == "CAPTCHA_FAILED":
            raise WebVpnError("WebVPN 要求验证码:请在浏览器登录一次 webvpn.zju.edu.cn"
                              "完成人机验证后再重试")
        raise WebVpnError(f"WebVPN 登录失败: {err}")

    def ensure_login(self) -> None:
        if not self.enabled:
            return
        if not self.logged_in:
            if WEBVPN_USERNAME and WEBVPN_PASSWORD:
                self.login()
            else:
                raise WebVpnError("WebVPN 已启用但无可用会话与凭据:请配置账号密码或 Cookie")
        self.logged_in = True  # 会话内已带 Cookie 即可,不反复校验

    # ---------- URL 转换 ----------
    def convert_url(self, url: str) -> str:
        """把 https://api.cc98.org/... 转成 webvpn 加密地址(与 cc98-mcp 相同规则)。"""
        if url.startswith(self.host):
            return url
        m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?(/.*)?$", url)
        if not m:
            return url
        scheme, host, port, rest = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        port_num = int(port) if port else 0
        special = bool(port_num) and not (
            (scheme == "http" and port_num == 80)
            or (scheme == "https" and port_num == 443)
        )
        prop = f"{scheme}-{port_num}" if special else scheme
        enc_host = webvpn_build_password("wrdvpnisthebest!", host)
        return f"{self.host}/{prop}/{enc_host}{rest}"

    # ---------- 请求 ----------
    def request(self, method: str, url: str, **kwargs: Any):
        """发请求:自动转 URL、手动跟随重定向、失败时判断会话失效并重登一次。"""
        if self.enabled:
            url = self.convert_url(url)
        timeout = kwargs.pop("timeout", (10, 30))
        headers = kwargs.pop("headers", None) or {}
        method = method.upper()

        for attempt in (1, 2):  # 第二次用于"会话失效→重新登录→重试"
            cur = url
            for _ in range(10):  # 手动跟随重定向上限
                resp = self.session.request(method, cur, headers=headers,
                                            allow_redirects=False,
                                            timeout=timeout, **kwargs)
                loc = resp.headers.get("Location") or resp.headers.get("location")
                if resp.status_code in (301, 302, 303, 307, 308) and loc:
                    from urllib.parse import urljoin
                    nxt = urljoin(resp.url, loc)
                    if self.enabled and not nxt.startswith(self.host):
                        nxt = self.convert_url(nxt)
                    if resp.status_code in (301, 302, 303) and method not in ("GET", "HEAD"):
                        method = "GET"
                        kwargs.pop("data", None)
                        kwargs.pop("json", None)
                    cur = nxt
                    continue
                break

            # 会话失效判定:被重定向回登录页
            if cur.rstrip("/").endswith("/login") or "/login?" in cur:
                logger.warning("WebVPN 会话失效,尝试重新登录…")
                self.logged_in = False
                if attempt == 1:
                    try:
                        self.ensure_login()
                        continue
                    except WebVpnError as exc:
                        raise
                raise WebVpnError("WebVPN 会话失效且重新登录失败")
            self._save_cookies()
            return resp
        raise WebVpnError("WebVPN 请求异常")  # pragma: no cover


# WebVPN 客户端单例(懒加载)
_webvpn_client: Optional[WebVpnClient] = None


def get_webvpn_client() -> WebVpnClient:
    global _webvpn_client
    if _webvpn_client is None:
        _webvpn_client = WebVpnClient()
    return _webvpn_client


def webvpn_allowed() -> bool:
    """WebVPN 是否启用且(有账号密码或有 Cookie 会话)。"""
    if not (WEBVPN_ENABLED and WEBVPN_HOST):
        return False
    mode = (WEBVPN_MODE or "auto").strip().lower()
    if mode == "direct":
        return False
    has_creds = bool(WEBVPN_USERNAME and WEBVPN_PASSWORD)
    has_cookies = bool(WEBVPN_COOKIE_STRING) or bool(WEBVPN_COOKIE_FILE and
                                                     os.path.exists(WEBVPN_COOKIE_FILE))
    return has_creds or has_cookies


def webvpn_mode() -> str:
    return (WEBVPN_MODE or "auto").strip().lower()


# ---------------- 3.2 方案 A:requests 直连官方 JSON 接口(支持 WebVPN 兜底) ----------------

def fetch_via_api(token_mgr: TokenManager, range_key: str,
                  cookie: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    """
    调用官方 JSON 接口(www.cc98.org 首页热门话题同款数据源)。
    网络路径: 默认直连 api.cc98.org;若 WEBVPN 已启用且直连失败(或 MODE=vpn),
             自动改经浙大 WebVPN 中转请求同一地址。
    返回 (条目列表, 实际使用的 API 地址/说明)。
    """
    path, _ = RANGE_TABLE[range_key]
    use_vpn = webvpn_allowed()
    mode = webvpn_mode()

    errors: List[str] = []

    def _parse(resp, url: str):
        """公共解析:校验状态码/JSON、取 hotTopic 字段、规范化。成功返回条目。"""
        # 401/403 → 若带的是手动 token,可能已过期:回退"匿名"再试一次
        # (实测四个热门榜单匿名即可读,回退通常能成功)
        if resp.status_code in (401, 403):
            return None  # 由调用方统一决定(带 token 时回退匿名 / 无 token 则报登录错)
        if resp.status_code != 200:
            raise MonitorError(f"接口异常 HTTP {resp.status_code}: {url}")
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            logger.warning("接口响应不是合法 JSON(%s): %s", url, resp.text[:150])
            raise FetchEmptyError(f"{url} 返回非 JSON 内容,可能被反爬拦截")
        # "当前热门"的 config/index 接口返回的是对象,十大榜单在其 hotTopic 字段里
        if range_key == "current" and isinstance(data, dict):
            data = data.get("hotTopic") or []
        items = normalize_items(data, range_key)
        if not items:
            raise FetchEmptyError(f"接口 {url} 返回空榜单(可能需要登录或榜单暂无数据)")
        return items

    def _auth_error(url: str) -> AuthRequiredError:
        return AuthRequiredError(
            f"接口 {url} 返回 401/403:缺少或过期登录凭据。\n"
            "   请更新 CC98_ACCESS_TOKEN(或 CC98_USERNAME/PASSWORD 自动登录);\n"
            "   若接口本就要求登录,也可改用 strategy='selenium' 复用浏览器登录态。"
        )

    for host in API_HOSTS:
        url = f"{host}/{path}"
        token_header = token_mgr.auth_header()

        # ---------- 1) 直连 ----------
        if mode != "vpn":
            try:
                resp = http_get_text(url, cookie=cookie, token_header=token_header)
                if resp.status_code in (401, 403) and token_header:
                    logger.warning("%s 返回 %s:凭据可能已过期,回退匿名重试一次",
                                   url, resp.status_code)
                    resp = http_get_text(url, cookie=cookie, token_header="")
                if resp.status_code in (401, 403):
                    raise _auth_error(url)
                items = _parse(resp, url)
                return items, url
            except AuthRequiredError:
                raise
            except MonitorError as exc:
                errors.append(f"[直连 {host}] {exc}")
                logger.warning("直连 %s 失败(%s),尝试备用通道…", host, exc)
            except Exception as exc:  # pragma: no cover
                errors.append(f"[直连 {host}] 未预期异常: {exc!r}")

        # ---------- 2) WebVPN 兜底 ----------
        if use_vpn:
            try:
                client = get_webvpn_client()
                client.ensure_login()
                headers = {"Accept": "application/json"}
                if token_header:
                    headers["Authorization"] = token_header
                # 手动 Cookie 只在直连时对 www 域有效;API 接口无需 CC98 Cookie
                resp = client.request("GET", url, headers=headers)
                if resp.status_code in (401, 403) and token_header:
                    logger.warning("%s(经WebVPN) 返回 %s,回退匿名重试一次",
                                   url, resp.status_code)
                    resp = client.request("GET", url,
                                          headers={"Accept": "application/json"})
                if resp.status_code in (401, 403):
                    raise _auth_error(f"{url}(经WebVPN)")
                items = _parse(resp, url)
                logger.info("已经 WebVPN 获取: %s", url)
                return items, f"{url} (WebVPN)"
            except (WebVpnError, MonitorError) as exc:
                errors.append(f"[WebVPN] {exc}")
                logger.warning("WebVPN 通道失败: %s", exc)

    if errors:
        raise MonitorError("; ".join(errors[:6]))
    raise MonitorError("所有 API 通道均不可用")


# ---------------- 3.3 方案 B:requests + BeautifulSoup 抓首页 HTML ----------------
# 【方案一】实现说明:
#   CC98 新版首页是 React SPA,直接 GET 到的 HTML 只有 <div id="root"></div> 空壳,
#   热门话题列表不在其中 —— 这是"页面动态加载"的典型情况。因此本函数:
#     1) 负责探测并明确给出"内容由 JS 渲染"的诊断;
#     2) 兼容未来若官网提供 SSR/静态版本时,能按通用锚点 <a href="/topic/.."> 解析;
#     3) 失败时不抛致命异常,返回空列表并提示改用 api / selenium 策略。
#   (它保留的意义:满足"requests+bs4"的代码骨架 + 作为调试辅助,比如配合
#    浏览器"另存为"的 HTML 离线解析时,可把 SELECTOR 按实际 DOM 微调。)

# 尝试解析的候选容器/选择器(按顺序尝试;需要时可在此微调)
HOME_TOPIC_SELECTORS: List[str] = [
    "a[href*='/topic/']",          # 通用:任何指向帖子详情页的链接
    ".hot-topic a[href*='/topic/']",
    "[class*='TopicListItem'] a",
]


def _looks_like_login_page(html: str) -> bool:
    """粗判当前 HTML 是不是"登录墙/跳转登录页"。"""
    text = html[:20000].lower()
    markers = (
        "统一身份认证", "zjuam", "login.cc98.org", "openid.cc98.org/connect/authorize",
        "登录", "sign in", "log in", "cas.zju",
    )
    hits = sum(1 for m in markers if m in text)
    return hits >= 2 or "zjuam.zju.edu.cn" in html[:3000]


def fetch_via_home_html(cookie: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    """
    方案一:requests 抓首页 + BeautifulSoup 解析(尽力而为)。
    返回 (条目列表, 说明信息)。
    """
    if BeautifulSoup is None:  # pragma: no cover
        raise StrategyUnavailableError("缺少 beautifulsoup4,请先 pip install beautifulsoup4")

    resp = http_get_text(HOME_URL, cookie=cookie)

    # --- Cookie 过期 / 被踢回登录页的识别 ---
    final_url = resp.url or HOME_URL
    if _looks_like_login_page(resp.text) or "login" in final_url.lower() \
            or "zjuam" in final_url.lower():
        raise CookieExpiredError(
            "访问 www.cc98.org 被重定向到登录页,说明 Cookie 缺失或已过期。\n"
            "   请重新按配置区注释的方法复制最新 Cookie 填到 CC98_COOKIE。"
        )
    if resp.status_code == 200 and "热门" not in resp.text and "/topic/" not in resp.text:
        # SPA 空壳判定
        if DEBUG_DUMP_HTML:
            try:
                with open("cc98_home_debug.html", "w", encoding="utf-8") as fp:
                    fp.write(resp.text)
                logger.info("首页 HTML 已存为 cc98_home_debug.html 供排查")
            except Exception as exc:
                logger.warning("保存调试 HTML 失败: %s", exc)
        raise FetchEmptyError(
            "首页 HTML 中找不到任何热门话题内容 —— CC98 新版首页为 JS 动态渲染(SPA)。\n"
            "   请改用 strategy='api'(推荐)或 strategy='selenium'(方案二)。\n"
            "   若你确认站点已提供静态 HTML,请检查 HOME_TOPIC_SELECTORS 是否匹配实际 DOM。"
        )

    # --- 通用锚点解析(应对 SSR/静态版本) ---
    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for selector in HOME_TOPIC_SELECTORS:
        for a in soup.select(selector):
            href = a.get("href") or ""
            m = re.search(r"/topic/(\d+)", href)
            if not m:
                continue
            tid = int(m.group(1))
            if tid in seen:
                continue
            seen.add(tid)
            items.append({
                "rank": len(items) + 1,
                "id": tid,
                "title": clean_text(a.get_text()),
                "url": make_url(tid),
                "author": "",
                "reply_count": 0,
                "board_id": None,
            })
        if items:
            break
    return items[:TOP_N], "首页静态 HTML 解析"


# ---------------- 3.4 方案 C(方案二):Selenium 渲染 SPA 后抓取 ----------------

SELENIUM_LOGIN_MARKERS = ("登录", "统一身份认证", "zjuam", "sign in")


def _is_login_wall(driver: Any) -> bool:
    """用当前页面 URL + 少量文本判断是否被踢到登录墙。"""
    url = (driver.current_url or "").lower()
    if "zjuam" in url or "login" in url:
        return True
    try:
        body = driver.find_element("tag name", "body").text[:4000]
    except Exception:
        return False
    if "历史上的今天" in body or "热门话题" in body:
        return False  # 已进入站点主界面
    score = sum(1 for m in SELENIUM_LOGIN_MARKERS if m in body)
    return score >= 2


def _click_range_tab(driver: Any, range_key: str) -> None:
    """点击榜单 Tab(本周/本月/…),找不到就忽略(接口抓取并不依赖点击)。"""
    label = RANGE_TABLE[range_key][1].replace("热门", "")
    try:
        tabs = driver.find_elements("css selector", '[role="tab"]')
        for tb in tabs:
            if label and label in (tb.text or ""):
                tb.click()
                logger.info("已点击 Tab: %s", tb.text)
                return
    except Exception as exc:
        logger.debug("点击 Tab 失败(忽略): %s", exc)


def _fetch_topics_in_browser(driver: Any, host: str, path: str,
                             wait_seconds: int) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """
    在已登录的浏览器页面上下文里用 fetch 直取官方 JSON 接口
    (带 localStorage 里的 access_token —— 这就是"复用浏览器登录态"的落点)。
    返回 (条目列表 or None, 状态说明)。
    """
    script = r"""
        const done = arguments[arguments.length - 1];
        (async () => {
            try {
                // 与官网 SPA 相同的读取逻辑:值可能是 "str-Bearer xxx" 或 "obj-..."
                let raw = localStorage.getItem('access_token');
                let at = null;
                if (raw) {
                    if (raw.startsWith('str-')) at = raw.slice(4);
                    else if (raw.startsWith('obj-')) at = null;
                    else at = raw;
                }
                const headers = { 'Accept': 'application/json' };
                if (at) headers['Authorization'] = at;
                const r = await fetch(host, { headers, credentials: 'include' });
                const text = await r.text();
                return { status: r.status, body: text };
            } catch (e) {
                return { status: 0, body: String(e) };
            }
        })().then(done);
    """
    import json as _json

    url = f"{host}/{path}"
    try:
        result = driver.execute_async_script(script, url, wait_seconds)
    except Exception as exc:
        logger.warning("浏览器内 fetch 失败(将回退 DOM 解析): %s", exc)
        return None, "浏览器 fetch 异常"
    if not isinstance(result, dict):
        return None, "浏览器 fetch 返回异常"
    status = result.get("status")
    body = result.get("body") or ""
    if status == 401 or status == 403:
        logger.warning("浏览器内接口返回 %s —— 该榜单可能要求登录", status)
        return None, f"接口需要登录(HTTP {status})"
    if status == 0 or status not in (200,):
        logger.warning("浏览器内接口请求失败: status=%s body=%s", status, body[:200])
        return None, "浏览器内请求失败"
    try:
        data = _json.loads(body)
    except _json.JSONDecodeError:
        return None, "响应非 JSON"
    # 与 fetch_via_api 保持一致的解析:"当前热门"的 config/index 返回对象,榜单在 hotTopic 字段
    if RANGE_KEY == "current" and isinstance(data, dict):
        data = data.get("hotTopic") or []
    items = normalize_items(data, RANGE_KEY)
    if not items:
        return None, "接口返回空"
    return items, "浏览器会话内直连官方接口"


def _parse_dom_items(driver: Any) -> List[Dict[str, Any]]:
    """
    DOM 兜底解析:从渲染出的 MUI ListItem 行里取标题。
    (注意:官网列表行是整行 onClick 路由跳转,没有 <a href>,DOM 里拿不到帖子 id,
     所以该方法只能给出标题、无法给出可靠链接;链接为空时通知里会标注"仅标题"。)
    若你实测发现 DOM 里有可用 href,可自行扩展本函数。
    """
    try:
        js = r"""
            const out = [];
            document.querySelectorAll('li[class*="MuiListItem"], [role="listitem"]')
                .forEach(li => {
                    const lines = (li.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                    if (lines.length) out.push(lines[0]);
                });
            return out.slice(0, 10);
        """
        titles = driver.execute_script(js) or []
    except Exception as exc:
        logger.warning("DOM 解析失败: %s", exc)
        return []
    items: List[Dict[str, Any]] = []
    for i, title in enumerate(titles, start=1):
        items.append({
            "rank": i, "id": None, "title": clean_text(title), "url": "",
            "author": "", "reply_count": 0, "board_id": None,
        })
    return items


def fetch_via_selenium(range_key: str, cookie: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    方案二:用 Selenium 启动真实浏览器(Edge/Chrome,见 SELENIUM_BROWSER)渲染 SPA。
    抓取顺序:浏览器会话内 fetch 官方接口(首选,能拿到 id→完整链接)
             → DOM 文本兜底(仅标题)。
    登录态来源(二选一,推荐 a):
      a) EDGE_USER_DATA_DIR / CHROME_USER_DATA_DIR 指向一个"已登录过 CC98"的独立
         配置目录 —— 建议直接用配套脚本 cc98_grab_token.py 自动创建并登录,之后
         每次启动该配置的浏览器都天然带登录态(localStorage 的 token 也在,SPA
         会自己续期,相当于长期有效的"Cookie 登录");
      b) 没有配置目录时,脚本会把 CC98_COOKIE 注入浏览器;若仍被踢到登录墙,
         会弹出一个窗口等你手动登录一次(之后本轮及后续同配置即可复用)。
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.edge.options import Options as EdgeOptions
    except ImportError:
        raise StrategyUnavailableError(
            "缺少 selenium 库 —— 方案二需要: pip install selenium\n"
            "   同时本机需安装 Edge 或 Chrome 浏览器(Selenium 4.6+ 会自动处理驱动)"
        )

    browser = (SELENIUM_BROWSER or "edge").strip().lower()
    if browser == "chrome":
        opts: Any = ChromeOptions()
        user_data_dir = CHROME_USER_DATA_DIR.strip()
        driver_factory = webdriver.Chrome
    else:  # 默认 edge
        opts = EdgeOptions()
        user_data_dir = EDGE_USER_DATA_DIR.strip()
        driver_factory = webdriver.Edge
        logger.debug("使用 Edge 渲染抓取")

    if user_data_dir:
        opts.add_argument(f"--user-data-dir={user_data_dir}")
    if SELENIUM_HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-infobars")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    path, _ = RANGE_TABLE[range_key]
    driver = None
    try:
        driver = driver_factory(options=opts)
        driver.set_page_load_timeout(max(SELENIUM_WAIT_SECONDS * 2, 30))
        driver.get(HOME_URL)

        # 注入 Cookie(若配置了且用户要求)
        if INJECT_COOKIE_ALWAYS and cookie:
            try:
                for name, value in cookie.items():
                    driver.add_cookie({"name": name, "value": value,
                                       "domain": ".cc98.org"})
                driver.refresh()  # 带 Cookie 重新加载
                time.sleep(3)
            except Exception as exc:
                logger.debug("注入 Cookie 失败(继续): %s", exc)

        # 登录墙?若开了可视窗口则等待用户手动登录一次
        if _is_login_wall(driver):
            logger.warning("当前会话未登录或 Cookie 失效,自动等待手动登录(最多 3 分钟)…")
            deadline = time.time() + 180
            while time.time() < deadline:
                time.sleep(5)
                if not _is_login_wall(driver):
                    break
            else:
                raise CookieExpiredError(
                    "浏览器会话始终处于登录墙。请在该 Chrome 窗口里手动完成 CC98 登录,\n"
                    "  或更新 CC98_COOKIE / 配置 CHROME_USER_DATA_DIR 登录态目录。"
                )

        _click_range_tab(driver, range_key)

        # 等 SPA 完成渲染 + 数据请求
        deadline = time.time() + SELENIUM_WAIT_SECONDS
        while time.time() < deadline:
            items = []
            for host in API_HOSTS:
                got, note = _fetch_topics_in_browser(driver, host, path,
                                                     max(int(deadline - time.time()), 3))
                if got:
                    logger.info("Selenium 抓取成功(%s):%s", host, note)
                    return got
                items = _parse_dom_items(driver)
                if items:
                    break
            time.sleep(2)

        # 全失败 → DOM 兜底结果
        dom_items = _parse_dom_items(driver)
        if dom_items:
            logger.warning("接口抓取失败,使用 DOM 文本兜底(注意:此类条目没有可靠链接)")
        return dom_items
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------- 3.5 策略分发 ----------------

def fetch_hot_topics(range_key: str, cookie: Dict[str, str],
                     token_mgr: TokenManager) -> Tuple[List[Dict[str, Any]], str]:
    """
    按 FETCH_STRATEGY 抓取当前榜单。始终返回非空列表,或抛出 MonitorError 子类。
    返回 (规范化的帖子列表, 实际使用策略说明)。
    """
    strategy = (FETCH_STRATEGY or "auto").strip().lower()
    if strategy not in ("auto", "api", "selenium", "requests"):
        raise MonitorError(f"FETCH_STRATEGY 配置不合法: {FETCH_STRATEGY}")

    attempts = {"api": ["api"], "selenium": ["selenium"],
                "requests": ["requests"],
                "auto": ["api", "selenium", "requests"]}[strategy]

    errors: List[str] = []
    auth_errors: List[Exception] = []  # 登录类错误单独收集(auto 模式不立即中断)
    for name in attempts:
        try:
            if name == "api":
                items, note = fetch_via_api(token_mgr, range_key, cookie)
            elif name == "selenium":
                if items := fetch_via_selenium(range_key, cookie):
                    note = "Selenium(方案二)"
                else:
                    raise FetchEmptyError("Selenium 未解析出任何条目")
            else:
                items, note = fetch_via_home_html(cookie)
            logger.info("本次抓取成功,策略=%s(%s),共 %d 条", name, note, len(items))
            return items, name
        except StrategyUnavailableError as exc:
            errors.append(f"[{name}] 环境不可用: {exc}")
        except (CookieExpiredError, AuthRequiredError) as exc:
            # 登录类问题:auto 模式下换下一个策略再试(如 selenium 里的浏览器会话可能仍有效)
            auth_errors.append(exc)
            errors.append(f"[{name}] 登录态问题: {exc}")
            if strategy != "auto":
                raise
        except MonitorError as exc:
            errors.append(f"[{name}] {exc}")
        except Exception as exc:  # 兜底:任何未预期异常都不能让进程崩溃
            logger.exception("策略 %s 发生未预期异常", name)
            errors.append(f"[{name}] 未预期异常: {exc!r}")

    # 全部失败:若存在登录类错误优先抛出(便于外层给出"更新 Cookie/Token"提示)
    if auth_errors:
        raise auth_errors[0]
    raise MonitorError("全部抓取策略失败:\n  " + "\n  ".join(errors))


# ============================================================================
# ④ 去重监控与持久化
# ============================================================================

def empty_history() -> Dict[str, Any]:
    return {"last_fetch_time": "", "last_ids": [], "last_topics": [], "events": []}


def load_history() -> Dict[str, Any]:
    """读取历史 JSON;文件不存在/损坏时返回空历史(不崩溃)。"""
    if not os.path.exists(HISTORY_FILE):
        return empty_history()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return empty_history()
        data.setdefault("last_ids", [])
        data.setdefault("last_topics", [])
        data.setdefault("events", [])
        return data
    except Exception as exc:
        logger.warning("读取历史文件失败(%s),按首次运行处理: %s", HISTORY_FILE, exc)
        return empty_history()


def save_history(history: Dict[str, Any]) -> None:
    """写回历史 JSON(UTF-8,带缩进,方便人肉查看)。"""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fp:
            json.dump(history, fp, ensure_ascii=False, indent=2)
        logger.info("历史记录已保存 -> %s", HISTORY_FILE)
    except Exception as exc:
        logger.error("保存历史文件失败: %s", exc)


# ---------- 运行状态文件(供可视化面板读取) ----------

# 模块内记忆上次写心跳的时间,避免每秒钟都写盘
_last_heartbeat_ts: float = 0.0


def write_status_file(**fields: Any) -> None:
    """
    把本轮/心跳状态写入 STATUS_FILE(JSON)。多次调用会合并字段,
    便于面板展示: 进程心跳、上次检测时间、是否成功、策略、错误信息等。
    """
    try:
        data: Dict[str, Any] = {}
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as fp:
                prev = json.load(fp)
            if isinstance(prev, dict):
                data.update(prev)
        data.update(fields)
        data["updated_at"] = now_str()
        data["updated_ts"] = round(time.time(), 1)
        with open(STATUS_FILE, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.debug("写状态文件失败(忽略): %s", exc)


def heartbeat_status() -> None:
    """调度循环内每约 10 秒刷一次心跳,让面板能判断进程是否存活。"""
    global _last_heartbeat_ts
    now = time.time()
    if now - _last_heartbeat_ts >= 10:
        _last_heartbeat_ts = now
        write_status_file(heartbeat_ts=now)


def filter_new(current: List[Dict[str, Any]], history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    与"上次抓取结果"对比,挑出"新进入"榜单的帖子(严格按需求:只对比上一次)。
    ALL_TIME_SUPPRESS=True 时,额外用 events 中全部历史 id 抑制重复提醒。
    """
    last_ids = {str(x) for x in history.get("last_ids", [])}
    new_list = [t for t in current if str(t.get("id")) not in last_ids]

    if ALL_TIME_SUPPRESS:
        seen: set = set()
        for ev in history.get("events", []):
            for nt in ev.get("new_topics", []):
                if nt.get("id") is not None:
                    seen.add(str(nt["id"]))
        new_list = [t for t in new_list if str(t.get("id")) not in seen]
    return new_list


def print_new_notice(range_label: str, new_list: List[Dict[str, Any]]) -> None:
    """
    以清晰格式输出通知:时间、序号、标题、链接。
    """
    bar = "=" * 64
    print("\n" + bar)
    print(f"  检测时间 : {now_str()}")
    print(f"  榜单     : {range_label}  (Top{TOP_N})")
    if not new_list:
        print("  结果     : 无新进入热门榜单的帖子")
    else:
        print(f"  结果     : 发现 {len(new_list)} 个「新进入」热门榜单的帖子")
        print("-" * 64)
        for i, t in enumerate(new_list, start=1):
            title = t.get("title") or "(无标题)"
            url = t.get("url") or ""
            print(f"  {ordinal(i)}  [{now_str()}]  {title}")
            if url:
                print(f"     链接: {url}")
            else:
                print(f"     链接: (DOM 兜底结果无链接,请改用 api/selenium 官方接口抓取)")
    print(bar + "\n")


def update_history(history: Dict[str, Any], current: List[Dict[str, Any]],
                   new_list: List[Dict[str, Any]]) -> None:
    """刷新"上次快照"并把本轮事件追加进 events(仅保留最近 MAX_EVENTS 条)。"""
    history["last_fetch_time"] = now_str()
    history["last_ids"] = [t.get("id") for t in current]
    history["last_topics"] = [
        {k: t.get(k) for k in ("rank", "id", "title", "url", "author")} for t in current
    ]
    history["events"].append({
        "time": now_str(),
        "range": RANGE_LABEL,
        "new_count": len(new_list),
        "new_topics": [
            {k: t.get(k) for k in ("rank", "id", "title", "url", "author")}
            for t in new_list
        ],
    })
    # 防止 events 无限膨胀
    history["events"] = history["events"][-500:]


def beep_alert() -> None:
    """Windows 下可选提示音(通知有新帖时用);其他平台静默跳过。"""
    try:
        import winsound
        winsound.Beep(880, 200)
        winsound.Beep(1175, 200)
    except Exception:
        pass


# ============================================================================
# ④⑨ 飞书通知(群机器人 Webhook) + 飞书多维表格留档 —— 均为可选,未配置自动禁用
# ============================================================================

def feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人"加签"算法:HMAC-SHA256(timestamp\\nsecret) 后 Base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def build_feishu_message(range_label: str, new_list: List[Dict[str, Any]]) -> str:
    """把本轮新增条目拼成一条飞书文本消息(标题行 + 若干"序号 标题/链接")。"""
    lines = [
        f"【CC98 监控】{now_str()}",
        f"榜单: {range_label} 发现 {len(new_list)} 个新进入热门榜单的帖子",
        "-" * 30,
    ]
    for i, t in enumerate(new_list, start=1):
        title = t.get("title") or "(无标题)"
        url = t.get("url") or ""
        lines.append(f"{ordinal(i)} {title}")
        if url:
            lines.append(url)
    text = "\n".join(lines)
    # 飞书文本消息长度上限较宽松,但保守截断避免被拒
    if len(text) > 3500:
        text = text[:3500] + "\n…(内容过长,已截断)"
    return text


def send_feishu_webhook(text: str) -> bool:
    """发送飞书群机器人消息。未配置 URL 返回 False;失败只记日志不影响主流程。"""
    url = FEISHU_WEBHOOK_URL.strip()
    if not url:
        return False
    if requests is None:  # pragma: no cover
        logger.error("缺少 requests,无法发送飞书消息")
        return False
    headers = {"Content-Type": "application/json"}
    secret = FEISHU_WEBHOOK_SECRET.strip()
    if secret:
        ts = str(int(time.time()))
        headers["X-Lark-Request-Timestamp"] = ts
        headers["X-Lark-Sign"] = feishu_sign(secret, ts)
    body = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=(5, 15))
    except requests.exceptions.RequestException as exc:
        logger.error("飞书 Webhook 发送失败: %s", exc)
        return False
    if resp.status_code != 200:
        logger.error("飞书 Webhook HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if data.get("code") not in (0, None):
        logger.error("飞书 Webhook 返回错误 code=%s msg=%s",
                     data.get("code"), data.get("msg"))
        return False
    logger.info("飞书群消息已发送(共 %d 个字符)", len(text))
    return True


# 租户 token 简单内存缓存:避免每轮都去换
_feishu_tenant_cache: Dict[str, Any] = {"token": "", "expires_at": 0.0}


def feishu_tenant_access_token() -> str:
    """获取飞书 tenant_access_token(自建应用凭证)。失败抛 MonitorError。"""
    if not (FEISHU_BITABLE_APP_ID and FEISHU_BITABLE_APP_SECRET):
        raise MonitorError("未配置 FEISHU_BITABLE_APP_ID / FEISHU_BITABLE_APP_SECRET")
    now = time.time()
    if _feishu_tenant_cache["token"] and _feishu_tenant_cache["expires_at"] > now + 60:
        return _feishu_tenant_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        resp = requests.post(url, json={
            "app_id": FEISHU_BITABLE_APP_ID,
            "app_secret": FEISHU_BITABLE_APP_SECRET,
        }, timeout=(5, 15))
    except requests.exceptions.RequestException as exc:
        raise MonitorError(f"获取飞书 tenant_access_token 网络失败: {exc}")
    try:
        data = resp.json()
    except ValueError:
        raise MonitorError("飞书 token 接口返回非 JSON")
    if data.get("code") != 0:
        raise MonitorError(f"飞书 token 换取失败: code={data.get('code')} "
                           f"msg={data.get('msg')}")
    _feishu_tenant_cache["token"] = data["tenant_access_token"]
    _feishu_tenant_cache["expires_at"] = now + int(data.get("expire", 7200))
    return _feishu_tenant_cache["token"]


def feishu_effective_app_token(token: str) -> str:
    """
    确定要操作的多维表格 app_token:
    - 填了 FEISHU_BITABLE_WIKI_TOKEN(知识库 wiki 链接里的节点 token):
      调用 wiki 接口解析出其实际对象(须是"多维表格" obj_type=22)的 obj_token;
    - 否则直接用 FEISHU_BITABLE_APP_TOKEN。
    """
    wiki = FEISHU_BITABLE_WIKI_TOKEN.strip()
    if not wiki:
        app = FEISHU_BITABLE_APP_TOKEN.strip()
        if not app:
            raise MonitorError("未配置多维表格:请填 FEISHU_BITABLE_APP_TOKEN(直链 app_token)"
                               "或 FEISHU_BITABLE_WIKI_TOKEN(wiki 节点 token)")
        return app
    url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node" \
          f"?token={wiki}"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                            timeout=(5, 15))
        data = resp.json()
    except Exception as exc:
        raise MonitorError(f"解析 wiki 节点失败: {exc}")
    if data.get("code") != 0:
        raise MonitorError(f"解析 wiki 节点失败 code={data.get('code')} "
                           f"msg={data.get('msg')} —— 请确认应用已加入该知识库且开通 "
                           f"wiki 与 bitable 权限")
    node = data.get("data", {}).get("node") or {}
    # obj_type 可能是数字 22 或字符串 "bitable",统一归一化判断
    obj_type = str(node.get("obj_type", "")).lower()
    if obj_type not in ("bitable", "22"):
        raise MonitorError(f"该 wiki 节点不是多维表格(obj_type={node.get('obj_type')},"
                           f"标题={node.get('title')});请在 wiki 里新建/打开一张多维表格"
                           f"并把它的链接发来")
    obj = node.get("obj_token") or ""
    logger.info("wiki 节点(%s) → 多维表格 app_token=%s", node.get("title"), obj)
    return obj


def feishu_bitable_table_id(token: str) -> str:
    """若未填 TABLE_ID,自动取该 base 的第一张表;返回表 id 或抛 MonitorError。"""
    tid = FEISHU_BITABLE_TABLE_ID.strip()
    if tid:
        return tid
    app = feishu_effective_app_token(token)
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app}/tables"
           f"?page_size=50")
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                            timeout=(5, 15))
        data = resp.json()
    except Exception as exc:
        raise MonitorError(f"获取多维表格列表失败: {exc}")
    if data.get("code") != 0 or not data.get("data", {}).get("items"):
        raise MonitorError(f"多维表格列表为空或失败: code={data.get('code')} "
                           f"msg={data.get('msg')} —— 请检查应用权限与 app_token")
    first = data["data"]["items"][0]
    logger.info("未配置 TABLE_ID,自动选用第一张表: %s(%s)",
                first.get("name"), first.get("table_id"))
    return first["table_id"]


def append_feishu_bitable(new_list: List[Dict[str, Any]]) -> bool:
    """把本轮"新进入"的帖子逐条写成多维表格的一行。成功返回 True。"""
    if not (FEISHU_BITABLE_APP_ID and FEISHU_BITABLE_APP_SECRET):
        return False
    if requests is None:  # pragma: no cover
        logger.error("缺少 requests,无法写入飞书多维表格")
        return False
    try:
        token = feishu_tenant_access_token()
        table_id = feishu_bitable_table_id(token)
    except MonitorError as exc:
        logger.error("飞书多维表格准备失败: %s", exc)
        return False

    fm = FEISHU_BITABLE_FIELD_MAP or {}
    rows = []
    for t in new_list:
        fields: Dict[str, Any] = {}
        mapping = {
            "time": now_str(),
            "range": RANGE_LABEL,
            "rank": str(t.get("rank", "")),
            "id": str(t.get("id", "")),
            "title": t.get("title", ""),
            "link": t.get("url", ""),
            "author": t.get("author", ""),
        }
        for key, col in fm.items():
            val = mapping.get(key)
            if col and val not in (None, ""):
                fields[col] = str(val)
        rows.append({"fields": fields})

    if not rows:
        return False
    try:
        app = feishu_effective_app_token(token)  # 支持 wiki 节点 token 自动解析
    except MonitorError as exc:
        logger.error("飞书多维表格准备失败: %s", exc)
        return False
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app}"
           f"/tables/{table_id}/records/batch_create")
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"},
                             json={"records": rows}, timeout=(5, 20))
        data = resp.json()
    except Exception as exc:
        logger.error("飞书多维表格写入失败: %s", exc)
        return False
    if data.get("code") != 0:
        # 常见原因:列名与表头不一致 / 缺权限 / table_id 错误
        logger.error("飞书多维表格写入失败 code=%s msg=%s —— 请核对表头列名"
                     "是否与 FEISHU_BITABLE_FIELD_MAP 一致", data.get("code"), data.get("msg"))
        return False
    logger.info("飞书多维表格已追加 %d 行记录", len(rows))
    return True


def feishu_self_test() -> None:
    """--feishu-test:验证飞书 Webhook 与多维表格配置(不发正式数据)。"""
    print("== 飞书连接自检 ==")
    if FEISHU_WEBHOOK_URL.strip():
        ok = send_feishu_webhook(
            f"【CC98 监控】连接测试 {now_str()}\n"
            f"如果看到这条消息,说明群机器人 Webhook 配置正确。"
        )
        print("群机器人 Webhook:", "✅ 已发送(请到群里确认)" if ok else "❌ 发送失败,看上方日志")
    else:
        print("群机器人 Webhook: 未配置(跳过)")
    if FEISHU_BITABLE_APP_ID and FEISHU_BITABLE_APP_SECRET:
        try:
            token = feishu_tenant_access_token()
            table_id = feishu_bitable_table_id(token)
            print(f"多维表格: ✅ 鉴权成功,目标表 table_id={table_id}")
        except MonitorError as exc:
            print(f"多维表格: ❌ {exc}")
    else:
        print("多维表格: 未配置(跳过)")


# ============================================================================
# ⑤ 定时调度
# ============================================================================

def monitor_once(print_list: bool = False) -> bool:
    """
    执行一轮完整监测:抓取 → 对比去重 → 通知 → 持久化。
    返回 True 表示本轮成功结束(无论有无新帖),False 表示本轮失败(不崩溃)。
    """
    cookie = parse_cookie(CC98_COOKIE)
    token_mgr = TokenManager(CC98_USERNAME, CC98_PASSWORD, CC98_ACCESS_TOKEN)
    if not cookie and not token_mgr.manual and not (token_mgr.username and token_mgr.password):
        logger.warning("CC98_COOKIE / ACCESS_TOKEN / 账号密码均为空,将尝试匿名访问")

    try:
        current, strategy = fetch_hot_topics(RANGE_KEY, cookie, token_mgr)
    except (CookieExpiredError, AuthRequiredError) as exc:
        logger.error("登录态问题: %s\n%s", exc,
                     ">>> 请更新 Cookie / Access Token 后重试(不会影响下次定时任务)")
        write_status_file(ok=False, range_key=RANGE_KEY, error=f"登录态问题: {exc}")
        return False
    except MonitorError as exc:
        logger.error("抓取失败: %s", exc)
        write_status_file(ok=False, range_key=RANGE_KEY, error=f"抓取失败: {exc}")
        return False
    except Exception:
        logger.exception("发生未预期异常(本轮跳过,进程继续运行)")
        write_status_file(ok=False, range_key=RANGE_KEY, error="发生未预期异常,见日志")
        return False

    if print_list:  # --test 模式:打印当前榜单即可
        print(f"\n[test] {now_str()} 当前 {RANGE_LABEL} Top{TOP_N} 榜单:")
        for t in current:
            print(f"  {ordinal(t['rank'])} {t['title']}   {t['url']}")
        return True

    history = load_history()

    # 首次运行(无任何历史)只建基线,默认不通知
    if not history.get("last_ids") and not history.get("events"):
        if NOTIFY_FIRST_RUN_AS_NEW:
            new_list = list(current)
        else:
            new_list = []
            logger.info("首次运行:已建立基线(%d 条),之后出现的新帖才会提醒", len(current))
    else:
        new_list = filter_new(current, history)

    # 输出通知(有新帖才打印,顺带提示音 + 飞书推送/留档)
    if new_list:
        print_new_notice(RANGE_LABEL, new_list)
        beep_alert()
        logger.info("发现 %d 个新进入热门榜单的帖子", len(new_list))
        # --- 飞书(可选):群机器人推送 + 多维表格留档;失败只记日志,不影响监控 ---
        try:
            send_feishu_webhook(build_feishu_message(RANGE_LABEL, new_list))
            append_feishu_bitable(new_list)
        except Exception:
            logger.exception("飞书推送/留档发生异常(已忽略,不影响本轮监控)")
    else:
        logger.info("本轮无新增热门帖(当前榜单 %d 条)", len(current))

    # 持久化(无论有无新增都更新快照)
    update_history(history, current, new_list)
    save_history(history)
    write_status_file(ok=True, range_key=RANGE_KEY, strategy=strategy,
                      top_count=len(current), new_count=len(new_list),
                      error=None)
    return True


def run_scheduler(interval_minutes: int) -> None:
    """
    立即执行第一轮,然后按 interval 定时循环。
    全程异常隔离:任何一轮失败都不会让调度进程退出。
    """
    if schedule is None:  # pragma: no cover
        raise StrategyUnavailableError("缺少 schedule 库,请先 pip install schedule")

    logger.info("=" * 50)
    logger.info("CC98 热门话题监控启动 | 榜单: %s | 间隔: %d 分钟 | 策略: %s",
                RANGE_LABEL, interval_minutes, FETCH_STRATEGY)
    logger.info("历史文件: %s | 日志文件: %s", HISTORY_FILE, LOG_FILE)
    logger.info("=" * 50)

    # 启动即跑一轮(让基线尽早建立)
    try:
        monitor_once()
    except Exception:
        logger.exception("首轮监测异常(继续进入定时循环)")

    # 注册定时任务
    schedule.every(interval_minutes).minutes.do(
        lambda: _scheduled_safe(monitor_once)
    ).tag("cc98_monitor")

    logger.info("定时任务已注册,每 %d 分钟检查一次。按 Ctrl+C 退出。", interval_minutes)
    while True:
        try:
            heartbeat_status()  # 每 ~10 秒刷心跳,供可视化面板判断存活
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C,正常退出")
            break
        except Exception:
            logger.exception("调度主循环异常(自动恢复)")


def _scheduled_safe(func) -> None:
    """schedule 任务的外层保险:回调内部异常不中断调度器。"""
    try:
        func()
    except Exception:
        logger.exception("定时任务执行异常(已隔离)")


# ============================================================================
# ⑥ 主程序入口
# ============================================================================

def main() -> None:
    global RANGE_KEY, RANGE_LABEL  # 允许命令行覆盖模块级榜单配置
    parser = argparse.ArgumentParser(
        description="CC98 论坛热门话题监控 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python cc98_hot_monitor.py              # 先监测一次再定时循环\n"
            "  python cc98_hot_monitor.py --once       # 只跑一轮后退出\n"
            "  python cc98_hot_monitor.py --test       # 抓取并打印当前榜单(不比对/不落盘)\n"
            "  python cc98_hot_monitor.py --interval 20\n"
        ),
    )
    parser.add_argument("--once", action="store_true",
                        help="只监测一次后退出(建议先跑一次验证配置)")
    parser.add_argument("--test", action="store_true",
                        help="抓取并打印当前榜单,不比对历史、不写文件")
    parser.add_argument("--feishu-test", action="store_true",
                        help="测试飞书连接(群机器人发测试消息 / 多维表格鉴权),不抓取")
    parser.add_argument("--feishu-seed", action="store_true",
                        help="把当前榜单 Top10 整批写入飞书多维表格(初始留档/补写用)")
    parser.add_argument("--webvpn-test", action="store_true",
                        help="测试 WebVPN:登录 → 经中转抓 config/index 前几条(部署自检用)")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_MINUTES,
                        help=f"定时检查间隔(分钟),默认 {CHECK_INTERVAL_MINUTES}")
    parser.add_argument("--range", default=RANGE_KEY,
                        choices=list(RANGE_TABLE.keys()),
                        help="监测榜单: current(当前热门=首页板块默认)/week(本周)/month(本月)"
                             f"/history(历史上的今天), 默认 {RANGE_KEY}")
    args = parser.parse_args()

    # 允许命令行覆盖全局配置(便于不改代码切换榜单/间隔)
    RANGE_KEY = args.range
    RANGE_LABEL = RANGE_TABLE[RANGE_KEY][1]

    setup_logging()

    if args.interval < 5:
        logger.warning("检查间隔 %d 分钟 < 5 分钟,有被封 IP 风险,已自动抬升为 5 分钟", args.interval)
        args.interval = 5

    if args.feishu_test:
        feishu_self_test()
        return

    if args.webvpn_test:
        # 部署自检:WebVPN 登录 → 经中转抓 config/index 前几条
        print("== WebVPN 通道自检 ==")
        if not WEBVPN_ENABLED:
            print("WEBVPN_ENABLED=False —— 跳过(若服务器在校外且无法直连,请先配置启用)")
            return
        if not webvpn_allowed():
            print("WebVPN 未配置凭据:需要 WEBVPN_USERNAME/PASSWORD 或 WEBVPN_COOKIE_STRING")
            return
        try:
            client = get_webvpn_client()
            client.ensure_login()
            print("✅ 登录/会话 OK")
            resp = client.request(
                "GET", "https://api.cc98.org/config/index",
                headers={"Accept": "application/json"},
            )
            print(f"经 WebVPN 请求 HTTP {resp.status_code}, 长度 {len(resp.text)}")
            if resp.status_code == 200:
                data = resp.json()
                hot = data.get("hotTopic") or [] if isinstance(data, dict) else []
                print(f"✅ 成功经 WebVPN 获取榜单,hotTopic {len(hot)} 条")
                for t in hot[:5]:
                    print("   ", t.get("id"), "|", (t.get("title") or "")[:40])
                print("自检通过:WebVPN 通道可用,监控将自动经此访问。")
            else:
                print("❌ HTTP 状态非 200,请查看上方日志。")
        except MonitorError as exc:
            print(f"❌ WebVPN 自检失败: {exc}")
            sys.exit(1)
        return

    if args.feishu_seed:
        try:
            cookie = parse_cookie(CC98_COOKIE)
            token_mgr = TokenManager(CC98_USERNAME, CC98_PASSWORD, CC98_ACCESS_TOKEN)
            current, strategy = fetch_hot_topics(RANGE_KEY, cookie, token_mgr)
        except MonitorError as exc:
            logger.error("抓取失败,无法写入种子数据: %s", exc)
            sys.exit(1)
        ok = append_feishu_bitable(current)
        if ok:
            print(f"[seed] 已将当前 {RANGE_LABEL} Top{len(current)} 写入飞书多维表格"
                  f"(检测时间 {now_str()})")
        else:
            print("[seed] 写入失败,请看上方日志")
        sys.exit(0 if ok else 1)

    if args.test:
        try:
            cookie = parse_cookie(CC98_COOKIE)
            token_mgr = TokenManager(CC98_USERNAME, CC98_PASSWORD, CC98_ACCESS_TOKEN)
            current, strategy = fetch_hot_topics(RANGE_KEY, cookie, token_mgr)
            print(f"[test] 策略={strategy} | {now_str()} {RANGE_LABEL} Top{TOP_N}:")
            for t in current:
                print(f"  {ordinal(t['rank'])} {t['title']}   {t['url']}")
        except MonitorError as exc:
            logger.error("测试抓取失败: %s", exc)
            sys.exit(1)
        return

    if args.once:
        ok = monitor_once()
        sys.exit(0 if ok else 1)

    run_scheduler(args.interval)


if __name__ == "__main__":
    main()
