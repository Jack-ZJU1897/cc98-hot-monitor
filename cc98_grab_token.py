# -*- coding: utf-8 -*-
"""
==============================================================================
 cc98_grab_token.py —— 从"独立浏览器窗口"一键抓取 CC98 access_token 并写回配置
==============================================================================
 用途:
   主脚本 cc98_hot_monitor.py 的官方 JSON 接口方案需要 Bearer Token。
   本助手会:
     1) 启动一个【全新的独立 Edge/Chrome 窗口】(使用独立配置目录,不影响你日常浏览器);
     2) 自动打开 https://www.cc98.org/ 登录页;
     3) 你只需在弹出的窗口里【手动登录一次】CC98(CAS 无法自动化,必须手动这一步);
     4) 脚本自动检测到登录成功 → 读取 localStorage 里的 access_token / refresh_token;
     5) 自动把 token 写回 cc98_hot_monitor.py 的 CC98_ACCESS_TOKEN,
        并把该"已登录的独立配置目录"写回 EDGE_USER_DATA_DIR / CHROME_USER_DATA_DIR
        (以后主脚本的 selenium 方案可直接复用这个登录态,SPA 会自动续期);
     6) 额外把 refresh_token 存入 cc98_token_cache.json,便于将来自动续期。

 使用方法:
     pip install selenium
     python cc98_grab_token.py                 # Edge(推荐)
     python cc98_grab_token.py --browser chrome
     python cc98_grab_token.py --dry-run       # 只检查要修改的配置项,不启动浏览器

 说明:
     - 只读取、不修改你正在使用的日常浏览器;独立窗口的数据与日常浏览器完全隔离。
     - token 属敏感凭据,本脚本不会把完整 token 打印到屏幕(只显示前 20 个字符)。
     - 独立配置目录默认建在脚本同目录下: cc98_edge_profile / cc98_chrome_profile
==============================================================================
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

# 与主脚本同目录存放
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MONITOR = os.path.join(HERE, "cc98_hot_monitor.py")
DEFAULT_CACHE = os.path.join(HERE, "cc98_token_cache.json")
EDGE_PROFILE_DEFAULT = os.path.join(HERE, "cc98_edge_profile")
CHROME_PROFILE_DEFAULT = os.path.join(HERE, "cc98_chrome_profile")

CC98_HOME = "https://www.cc98.org/"
# 当前线上站点的登录入口(实测为 /logOn;旧版 PWA 曾用 /logIn)
CC98_LOGIN_URL = "https://www.cc98.org/logOn"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def strip_prefix(raw: str) -> str:
    """去掉 PWA 存 localStorage 时的 'str-' / 'obj-' 前缀。"""
    raw = (raw or "").strip()
    if raw.startswith("str-"):
        raw = raw[4:]
    if raw.startswith("obj-"):
        return ""
    return raw


# ---------------------------------------------------------------------------
# 配置回写:把抓到的值写入 cc98_hot_monitor.py
# ---------------------------------------------------------------------------

def rewrite_monitor_config(monitor_path: str, *,
                           access_token: str = "",
                           user_data_dir: str = "",
                           user_data_var: str = "EDGE_USER_DATA_DIR") -> list:
    """
    用正则原地修改主脚本配置区:
      - CC98_ACCESS_TOKEN: str = "..."   → 新的 token(完整 "Bearer xxx")
      - EDGE_USER_DATA_DIR / CHROME_USER_DATA_DIR: str = "" → 新的独立配置目录
    返回被修改的配置项名列表。
    """
    with open(monitor_path, "r", encoding="utf-8") as fp:
        text = fp.read()

    changed: list = []

    def _replace(line_key: str, new_value: str) -> None:
        """把 `line_key: str = "旧值"` 整行替换为新值(用切片替换,避免 re.sub
        对替换串里反斜杠的转义处理把 Windows 路径搞坏)。"""
        nonlocal text
        literal = json.dumps(new_value)  # 生成合法的 Python/JSON 字符串字面量
        pat = re.compile(
            r"^(?P<indent>[ \t]*)" + re.escape(line_key) + r': str = "[^"]*"[ \t]*$',
            re.MULTILINE,
        )
        m = pat.search(text)
        if m is None:
            raise SystemExit(
                f"[错误] 在 {os.path.basename(monitor_path)} 中找不到配置项 {line_key},"
                f"请确认使用的是配套的 cc98_hot_monitor.py"
            )
        new_text = (
            text[: m.start()]
            + f"{m.group('indent')}{line_key}: str = {literal}"
            + text[m.end():]  # 保留原行尾的换行
        )
        text = new_text
        changed.append(line_key)

    if access_token:
        _replace("CC98_ACCESS_TOKEN", access_token)
    if user_data_dir:
        _replace(user_data_var, user_data_dir)

    with open(monitor_path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return changed


def write_token_cache(cache_path: str, *, access_token: str, refresh_token: str,
                      access_exp_sec: int, refresh_exp_sec: int) -> None:
    """
    写 refresh_token 缓存(格式与主脚本 TokenManager 一致)。
    将来若 CC98_ACCESS_TOKEN 留空,主脚本会优先用缓存里的 refresh_token 自动续期。
    access_token 形如 "Bearer <jwt>",缓存里只存裸 JWT + token_type。
    """
    raw_jwt = access_token
    if raw_jwt.lower().startswith("bearer "):
        raw_jwt = raw_jwt[7:]
    now_sec = int(time.time())
    data = {
        "token_type": "Bearer",
        "access_token": raw_jwt,
        "refresh_token": refresh_token or "",
        "expires_at": float(access_exp_sec or now_sec + 3600),
        "saved_at": now_str(),
    }
    with open(cache_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 浏览器内抓取
# ---------------------------------------------------------------------------

def _read_token_state(driver) -> dict:
    """读取当前页面 localStorage 中与 CC98 相关的 token 状态(跨域安全地取)。
    兼容两代前端:新版(3.x)用 accessToken / accessToken_expirationTime(秒);
    旧版(CC98-PWA)用 access_token / access_token_expirationTime(毫秒)。"""
    js = r"""
        const out = { url: location.href, at: null, rt: null, atExp: 0, rtExp: 0 };
        try {
            const g = k => localStorage.getItem(k);
            const strip = v => {
                if (!v) return null;
                if (v.startsWith('str-')) return v.slice(4);
                if (v.startsWith('obj-')) return null;
                return v;
            };
            // access token:新版 accessToken,旧版 access_token
            out.at = strip(g('accessToken')) || strip(g('access_token'));
            out.rt = strip(g('refresh_token'));
            // 过期时间:新版存"秒",旧版存"毫秒",统一换算成秒
            const toSec = v => {
                const n = parseInt(v || '0', 10) || 0;
                return n > 1e12 ? Math.floor(n / 1000) : n;
            };
            out.atExp = toSec(g('accessToken_expirationTime')) || toSec(g('access_token_expirationTime'));
            out.rtExp = toSec(g('refresh_token_expirationTime'));
        } catch (e) { /* 其他域名的页面读不到 cc98 的 localStorage,属正常 */ }
        return out;
    """
    try:
        return driver.execute_script(js) or {}
    except Exception as exc:
        return {"url": "", "at": None, "rt": None, "atExp": 0, "rtExp": 0, "err": str(exc)}


def wait_for_login(driver, timeout_seconds: int) -> dict:
    """
    轮询等待用户在浏览器窗口里完成 CC98 登录。
    成功条件:localStorage.access_token 存在且像有效 JWT(长度 > 40)。
    返回含 at / rt / atExp / rtExp 的状态 dict。
    """
    deadline = time.time() + timeout_seconds
    last_note = 0.0
    while time.time() < deadline:
        st = _read_token_state(driver)
        at = st.get("at") or ""
        if len(at) > 40:
            return st

        # 每 15 秒打印一次提示,让用户知道脚本在等什么
        if time.time() - last_note > 15:
            url = (st.get("url") or "").split("?")[0]
            print(f"[{now_str()}] 尚未检测到登录(当前页面: {url})")
            print("          请在【弹出的独立浏览器窗口】的登录表单里输入 CC98 用户名和密码,"
                  "点击「登录」")
            print("          (登录成功后页面会自动跳转,脚本随即自动抓取 token,无需再操作)")
            last_note = time.time()
        time.sleep(3)
    return {"at": None, "rt": None, "atExp": 0, "rtExp": 0}


def launch_and_grab(browser: str, profile_dir: str, timeout_seconds: int) -> dict:
    """启动独立浏览器窗口,等待登录,返回 token 状态。"""
    try:
        from selenium import webdriver
        if browser == "chrome":
            from selenium.webdriver.chrome.options import Options
            opts = Options()
            driver_factory = webdriver.Chrome
        else:
            from selenium.webdriver.edge.options import Options
            opts = Options()
            driver_factory = webdriver.Edge
    except ImportError:
        print("[错误] 缺少 selenium,请先执行: pip install selenium")
        sys.exit(1)

    os.makedirs(profile_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--start-maximized")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = None
    try:
        driver = driver_factory(options=opts)
        driver.set_page_load_timeout(60)
        print(f"[{now_str()}] 独立浏览器窗口已打开(配置目录: {profile_dir})")
        # 直接打开 CC98 的"用户名+密码"登录页(走 OAuth 密码模式,无需 CAS)
        print(f"[{now_str()}] 正在打开登录页 {CC98_LOGIN_URL} …")
        driver.get(CC98_LOGIN_URL)

        # 万一目录里已有登录态,直接读取
        st = _read_token_state(driver)
        if len(st.get("at") or "") > 40:
            print(f"[{now_str()}] 该配置目录已有登录态,直接使用")
            return st

        st = wait_for_login(driver, timeout_seconds)
        if not (st.get("at") or ""):
            raise RuntimeError("等待登录超时 —— 请重跑本脚本并尽快在独立窗口完成登录;"
                               "\n   若登录页被自动化检测拦截,可改为手动: 在页面按 F12 → Console →"
                               "\n   输入 localStorage.getItem('access_token') 并自行粘贴到主脚本。")
        return st
    finally:
        if driver is not None:
            try:
                driver.quit()  # 关闭窗口;配置目录(含登录态)会保留
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="从独立浏览器窗口抓取 CC98 access_token 并写回配置")
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge",
                        help="用哪个浏览器打开独立窗口(默认 edge)")
    parser.add_argument("--profile", default="",
                        help="独立配置目录(默认: 脚本目录下的 cc98_edge_profile / cc98_chrome_profile)")
    parser.add_argument("--monitor", default=DEFAULT_MONITOR,
                        help=f"要写回的主脚本路径(默认: {DEFAULT_MONITOR})")
    parser.add_argument("--timeout", type=int, default=8,
                        help="等待手动登录的最长分钟数(默认 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查主脚本配置项是否存在,不启动浏览器、不写文件")
    args = parser.parse_args()

    if not os.path.exists(args.monitor):
        print(f"[错误] 找不到主脚本: {args.monitor}")
        sys.exit(1)

    user_data_var = "EDGE_USER_DATA_DIR" if args.browser == "edge" else "CHROME_USER_DATA_DIR"
    profile = args.profile or (EDGE_PROFILE_DEFAULT if args.browser == "edge"
                               else CHROME_PROFILE_DEFAULT)
    profile = os.path.abspath(profile)

    # 先校验主脚本里待改的配置项都在(避免启动半天浏览器后才发现写不回去)
    try:
        with open(args.monitor, "r", encoding="utf-8") as fp:
            text = fp.read()
        missing = [k for k in ("CC98_ACCESS_TOKEN", user_data_var)
                   if not re.search(r"^%s: str =" % re.escape(k), text, re.MULTILINE)]
        if missing:
            print(f"[错误] 主脚本 {os.path.basename(args.monitor)} 缺少配置项: {missing}")
            sys.exit(1)
    except OSError as exc:
        print(f"[错误] 无法读取主脚本: {exc}")
        sys.exit(1)

    if args.dry_run:
        print("[dry-run] 配置项检查通过,将修改:")
        print(f"          - CC98_ACCESS_TOKEN  ← (抓到的 token)")
        print(f"          - {user_data_var} ← {profile}")
        print("[dry-run] 未启动浏览器、未写任何文件")
        return

    print("=" * 64)
    print(f"[{now_str()}] 即将打开独立的{args.browser}窗口用于 CC98 登录")
    print("          该窗口使用独立配置目录,与你日常使用的浏览器完全隔离。")
    print("          请在窗口内登录一次 CC98;脚本会自动检测并抓取 token。")
    print("=" * 64)

    st = launch_and_grab(args.browser, profile, args.timeout * 60)

    at = st.get("at") or ""
    rt = st.get("rt") or ""
    at_exp = int(st.get("atExp") or 0)
    rt_exp = int(st.get("rtExp") or 0)

    # 隐私:只显示 token 前 20 个字符
    shown = at[:20] + ("…" if len(at) > 20 else "")
    print(f"\n[{now_str()}] 抓取成功! access_token 前 20 字符: {shown}")

    changed = rewrite_monitor_config(
        args.monitor,
        access_token=at,
        user_data_dir=profile,
        user_data_var=user_data_var,
    )
    write_token_cache(DEFAULT_CACHE, access_token=at, refresh_token=rt,
                      access_exp_sec=at_exp, refresh_exp_sec=rt_exp)
    print(f"[{now_str()}] 已写入主脚本 {os.path.basename(args.monitor)}: {changed}")
    print(f"[{now_str()}] 已写入 refresh_token 缓存: {os.path.basename(DEFAULT_CACHE)}")
    print(f"[{now_str()}] 已建立独立登录配置目录(可长期复用): {profile}")
    print("-" * 64)
    print("后续使用:")
    print(f"   python {os.path.basename(args.monitor)} --test     # 验证抓取")
    print(f"   python {os.path.basename(args.monitor)}            # 正式监控")
    print("说明:手动 token 有效期通常较短;主脚本 FETCH_STRATEGY='auto' 时,")
    print("     token 失效后会自动改用 selenium 方案复用上面这个已登录的独立窗口配置,")
    print("     或在把 CC98_ACCESS_TOKEN 清空后,由缓存中的 refresh_token 自动续期。")


if __name__ == "__main__":
    main()
