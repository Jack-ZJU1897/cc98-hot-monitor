# CC98 Hot Monitor · CC98 热门话题监控

监控浙江大学 CC98 论坛首页「热门话题」板块的 **当前热门 Top10**，发现「新进榜」帖子时自动通知并留档，附带本地可视化面板。

> 核心卖点：**匿名调用官方只读接口即可工作**，默认不需要 Cookie / Token / 登录；支持本地个人电脑与云服务器两种运行模式。

---

## 功能特性

- ✅ 定时抓取「当前热门」Top10（也可切换：本周 / 本月 / 历史上的今天 / 今日热门）
- ✅ **去重监控**：只对「新进入」榜单的帖子提醒（严格对比上一轮）
- ✅ 通知渠道：控制台 + 日志 + Windows 提示音 + **飞书群机器人**（可选）
- ✅ **飞书多维表格留档**（可选）：新帖自动追加一行（镜像/增量两种思路可配置）
- ✅ **本地可视化面板**（纯 Python 标准库 Web 页面，自动刷新）：榜单、事件流、心跳、日志
- ✅ 稳健性：超时指数退避重试、登录态失效自动回退匿名、异常隔离不崩溃、每轮状态落盘
- ✅ 两种运行模式：
  - **本地模式**：个人电脑（校园网直连 / WebVPN 中转）
  - **服务器模式**：云服务器 7×24 常驻（经 [zju-connect](https://github.com/Mythologyli/zju-connect) RVPN 隧道进入校网，systemd 托管，崩溃/重启自愈）

## 数据来源（已核实）

新版 CC98 首页是 React SPA，「热门话题」板块下方默认展示的"当前热门"列表来自官方只读接口：

| 榜单 | 接口 | 说明 |
|---|---|---|
| 当前热门（首页板块默认） | `GET https://api.cc98.org/config/index` 的 `hotTopic` 字段 | 十大热门话题 |
| 本周热门 | `GET https://api.cc98.org/topic/hot-weekly` | |
| 本月热门 | `GET https://api.cc98.org/topic/hot-monthly` | |
| 历史上的今天 | `GET https://api.cc98.org/topic/hot-history` | |

实测这些榜单接口**匿名即可访问**（无需 CC98 登录态），因此默认配置不依赖任何凭据。

## 目录结构

```
cc98-hot-monitor/
├─ cc98_hot_monitor.py      # 监控主程序(单文件,本地+服务器通用,配置区开关)
├─ cc98_dashboard.py        # 本地可视化面板(标准库,无第三方依赖)
├─ cc98_grab_token.py       # (可选)一键抓取 access_token 的辅助脚本
├─ requirements.txt
├─ docs/
│  ├─ 本地模式.md            # 个人电脑上的用法
│  └─ 服务器模式.md          # 云服务器 7×24 部署(zju-connect 隧道 + systemd)
└─ deploy/
   ├─ zju-connect.service    # systemd:RVPN 隧道
   ├─ cc98-monitor.service   # systemd:监控
   ├─ cc98-dashboard.service # systemd:面板
   ├─ health_check.sh        # 一键体检脚本
   └─ server_setup.sh        # 服务器一键初始化(交互式,不落明文)
```

## 快速开始

### 本地模式（最快 3 步跑起来）

```bash
pip install -r requirements.txt
python cc98_hot_monitor.py --test      # 打印当前榜单(验证网络)
python cc98_hot_monitor.py             # 先跑一轮,之后每 10 分钟自动检查
```

详见 [`docs/本地模式.md`](docs/本地模式.md)。

### 服务器模式（7×24）

云服务器 + [zju-connect](https://github.com/Mythologyli/zju-connect) RVPN 隧道 + systemd 三服务，详见 [`docs/服务器模式.md`](docs/服务器模式.md)。

## 主要配置（`cc98_hot_monitor.py` 顶部配置区）

| 配置 | 说明 | 默认 |
|---|---|---|
| `RANGE_KEY` | 榜单：`current`(默认) / `week` / `month` / `history` | `current` |
| `TOP_N` | 取前多少条 | `10` |
| `CHECK_INTERVAL_MINUTES` | 检查间隔（CC98 有反爬，建议 ≥5） | `10` |
| `FETCH_STRATEGY` | `api`(推荐) / `selenium` / `requests` / `auto` | `auto` |
| `CC98_PROXY` | 服务器模式经 SOCKS5 隧道：`socks5h://127.0.0.1:1080` | 空(直连) |
| `FEISHU_WEBHOOK_URL/SECRET` | 飞书群机器人（空=关闭） | 空 |
| `FEISHU_BITABLE_*` | 飞书多维表格留档（空=关闭） | 空 |
| `WEBVPN_*` | 网页 WebVPN 中转（部分网络需要） | 关闭 |
| `CC98_ACCESS_TOKEN` 等 | 可选增强凭据（匿名即可用，不必填） | 空 |

## 致谢

- [ZJU-Connect](https://github.com/Mythologyli/zju-connect) —— RVPN 无头客户端，服务器模式的核心
- [EasierConnect](https://github.com/lyc8503/EasierConnect) —— zju-connect 的前身思路
- [cc98-mcp](https://github.com/EviterLesRoses2/cc98-mcp) / [CC98-CLI](https://github.com/Lucent-Snow/CC98-CLI) —— WebVPN 实现参考
- [ZJU-CC98/CC98-PWA](https://github.com/ZJU-CC98/CC98-PWA) 及线上前端 —— 接口与数据结构参考来源

## License

[MIT](LICENSE)

## 免责声明

本项目**仅用于学习与技术交流**。请使用者：
- 遵守浙江大学校园网/CC98 相关使用规定，尊重论坛反爬与负载；
- 默认抓取间隔已克制（≥5 分钟），请勿调高频率；
- 使用本项目造成的影响与后果由使用者自行承担（本项目按原样提供，不提供任何保证）。
