#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 0 9 * * *
new Env('创想云签到')

创想云国内站 · 自动签到脚本 · 青龙面板版
=================================================
站点   : https://www.crealitycloud.cn  （3D 打印模型平台，国内 CN 站）
登录   : 账号（手机号 / 邮箱）+ 密码
功能   : 每日签到（连续签到）
依赖   : 无（仅 Python 标准库 urllib）
适配   : 青龙面板（单文件、零依赖，支持订阅式部署与定时任务）

用法
-------------------------------------------------
1. 配置账号：设置环境变量  CREALITY_ACCOUNTS  或直接改下方 CONFIG_ACCOUNTS
   格式：每行「账号 密码」（账号与密码用空格分隔），多个账号用换行分隔
   - 手机号：直接填 11 位手机号（自动补 +86 区号）；也可填「8613800138000」这种已带区号
   - 邮箱  ：直接填邮箱地址

2. 青龙面板定时规则： 0 9 * * *   （每天 09:00 执行）
3. 运行：python3 crealitycloud_checkin.py

说明
-------------------------------------------------
- 登录走官方账号中心 id.creality.cn（OAuth 统一登录）
- 签到接口 api.crealitycloud.cn，认证 header 为 __CXY_TOKEN_ / __CXY_UID_
- 已内置「直接登录态签到 → OAuth 授权码换取」双路径降级，提升兼容性
- 脚本目录放置青龙自带 notify.py 后，运行结束自动推送中文报告
"""

import json
import os
import ssl
import sys
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
from http.cookiejar import CookieJar

# ---------------- 统一通知模块加载（青龙 notify.py） ----------------
hadsend = False
try:
    from notify import send
    hadsend = True
except ImportError:
    hadsend = False


def notify_user(title, content):
    """统一通知函数：有 notify.py 走青龙通知，否则仅打印。"""
    if hadsend:
        try:
            send(title, content)
        except Exception as e:  # noqa
            print(f"❌ 通知发送失败: {e}")
    else:
        print(f"📢 {title}")

# ============================================================
# 配置区（二选一：环境变量优先于下方配置）
# ============================================================

# 环境变量名（青龙面板中配置，格式：每行「账号 密码」，多账号换行分隔）
ENV_ACCOUNTS = "CREALITY_ACCOUNTS"

# 内置账号配置：仅当环境变量未设置时生效
# 格式：每行「账号 密码」（账号与密码用空格分隔）；手机号自动补 86 区号，邮箱直接填
CONFIG_ACCOUNTS = """
# 示例：
# 13800138000 你的密码
# your_email@example.com 你的密码
"""

# ============================================================
# 常量（接口地址、clientId、请求头模板）
# ============================================================

# 登录 / OAuth 授权 / 签到相关接口
LOGIN_URL = "https://id.creality.cn/api/cxy/account/v2/loginV2"          # 账号密码登录
AUTHORIZE_URL = "https://id.creality.cn/api/cxy/oauth2/authorize"        # OAuth 授权拿 code
OAUTH_LOGIN_URL = "https://api.crealitycloud.cn/api/cxy/account/v2/oauthLogin"  # code 换 token
CHECKIN_URL = "https://api.crealitycloud.cn/api/cxy/v2/task/checkinV2"    # 签到
LAST_CHECKIN_URL = "https://api.crealitycloud.cn/api/cxy/v2/task/getLastCheckin"  # 最近签到状态
USER_INFO_URL = "https://api.crealitycloud.cn/api/cxy/v2/user/getInfo"           # 用户信息（含总积分）

# 国内 CN 站 clientId（从官方前端环境配置提取）
CLIENT_ID = "8ea5010984fa52a298f12110af8b05d0"

# OAuth 回调地址（创想云 oauth 路由）
OAUTH_REDIRECT_URI = "https://www.crealitycloud.cn/oauth"

# 登录中心（id.creality.cn）与主站（api.crealitycloud.cn）的设备标识
APP_ID_SSO = "creality_account"   # 登录中心 appId
APP_ID_CN = "cxy-gen2"            # 国内站 appId
APP_VER_SSO = "0.0.1"
APP_VER_CN = "7.3.28"

# 稳定设备唯一标识（可自行修改，避免频繁变动触发风控）
DEVICE_DUID = "wb-checkin-duid-2026"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 错误码含义（官方前端提取）
ERR_MSG = {
    0: "成功",
    1: "参数错误",
    4: "Token 无效",
    5: "身份无效",
    9: "账号不存在",
    35: "用户被封禁",
}

# ============================================================
# 工具函数
# ============================================================


def _uuid():
    return str(uuid.uuid4())


def mask_account(account):
    """账号脱敏：手机号保留前3后2，邮箱只露前缀前2位与域名。"""
    account = account.strip()
    if "@" in account:
        local, _, domain = account.partition("@")
        head = local[:2] if len(local) >= 2 else local
        return f"{head}***@{domain}"
    if len(account) <= 6:
        return "****"
    return account[:3] + "****" + account[-2:]


def _build_opener():
    """构建带未验证 SSL 上下文 + cookie 管理的 opener"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(CookieJar()),
    )


def _build_sso_headers(extra=None):
    """登录中心（id.creality.cn）请求头"""
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": UA,
        "__CXY_REQUESTID_": _uuid(),
        "__CXY_APP_ID_": APP_ID_SSO,
        "__CXY_APP_VER_": APP_VER_SSO,
        "__CXY_OS_LANG_": "1",
        "__CXY_PLATFORM_": "pc",
        "__CXY_DUID_": DEVICE_DUID,
        "__CXY_TIMEZONE_": "GMT+08:00",
    }
    if extra:
        h.update(extra)
    return h


def _build_cn_headers(token="", uid="", extra=None):
    """国内主站（api.crealitycloud.cn）请求头"""
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": UA,
        "__CXY_REQUESTID_": _uuid(),
        "__CXY_APP_ID_": APP_ID_CN,
        "__CXY_APP_VER_": APP_VER_CN,
        "__CXY_OS_LANG_": "1",
        "__CXY_PLATFORM_": "2",
        "__CXY_DUID_": DEVICE_DUID,
        "__CXY_TIMEZONE_": "GMT+08:00",
        "__CXY_TOKEN_": token or "",
        "__CXY_UID_": str(uid or ""),
    }
    if extra:
        h.update(extra)
    return h


def http_request(url, method="POST", headers=None, data=None, opener=None):
    """通用 HTTP 请求，返回 (status, body_dict, resp_headers)"""
    if headers is None:
        headers = {}
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    handler = opener if opener else _build_opener()
    try:
        resp = handler.open(req, timeout=30)
        raw = resp.read().decode("utf-8", "ignore")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        return e.code, parsed, dict(e.headers)
    except Exception as e:  # noqa
        return -1, {"_error": str(e)}, {}


# ============================================================
# 核心业务
# ============================================================


def _ensure_api_ok(status, body, action):
    """校验 HTTP 状态与返回体，异常时抛出带上下文的 RuntimeError。

    http_request 网络异常时 status=-1 且 body 含 _error；
    HTTP 非 200 或返回非 dict 均视为异常，避免上层拿到 code=None 无法定位。
    """
    if status == -1:
        err = body.get("_error", "未知网络错误") if isinstance(body, dict) else body
        raise RuntimeError(f"{action} 网络异常: {err}")
    if status != 200:
        raise RuntimeError(f"{action} HTTP 状态异常: status={status}, body={body}")
    if not isinstance(body, dict):
        raise RuntimeError(f"{action} 返回格式异常: {body}")


def parse_account(account):
    """解析账号，返回 (type, 标准账号)

    type: 1=手机号  2=邮箱
    """
    account = account.strip()
    if "@" in account:
        return 2, account
    # 手机号：去掉可能存在的 +、空格、- 后应得到 11 位（或 86+11=13 位）
    phone = account.replace("+", "").replace(" ", "").replace("-", "")
    if not phone.isdigit():
        raise ValueError(f"账号无法识别（非邮箱且含非数字字符）: {account}")
    if len(phone) == 11:
        return 1, "86" + phone           # 纯 11 位手机号，补 +86 区号
    if len(phone) == 13 and phone.startswith("86"):
        return 1, phone                   # 已带 86 区号
    raise ValueError(f"手机号位数异常（应 11 位或 86+11 位）: {account}")


def login(account, password):
    """账号密码登录，返回 (token, userId, opener)

    直接返回 loginV2 的 token 与 userId；opener 保留登录态 cookie 供 OAuth 降级使用。
    """
    type_, std_account = parse_account(account)
    kind = "手机号" if type_ == 1 else "邮箱"

    opener = _build_opener()

    payload = {
        "type": type_,
        "account": std_account,
        "password": password,
        "clientId": CLIENT_ID,
    }
    status, body, headers = http_request(
        LOGIN_URL,
        method="POST",
        headers=_build_sso_headers(),
        data=payload,
        opener=opener,
    )

    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"[{kind}] 登录请求异常: status={status}, body={body}")

    code = body.get("code", -1)
    if code != 0:
        msg = body.get("msg") or ERR_MSG.get(code, "未知错误")
        raise RuntimeError(f"[{kind}] 登录失败 code={code}: {msg}")

    result = body.get("result") or {}
    token = result.get("token", "")
    userId = result.get("userId", "")
    if not token:
        raise RuntimeError(f"[{kind}] 登录成功但未返回 token")

    return token, userId, opener


def oauth_exchange_token(token, opener):
    """OAuth 降级路径：用登录态拿授权码 code，再换主站 token。

    返回 (token, userId)。当 loginV2 的 token 无法直接用于签到接口时调用。
    """
    # 1) authorize 拿 code（依赖 loginV2 设置的同域登录态 cookie）
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "state": "checkin",
        "timestamp": int(time.time() * 1000),
        "uid": _uuid(),
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    status, body, _ = http_request(
        url,
        method="GET",
        headers=_build_sso_headers(),
        opener=opener,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"OAuth 授权失败: status={status}, body={body}")

    code = body.get("code", -1)
    if code != 0:
        msg = body.get("msg") or ERR_MSG.get(code, "未知错误")
        raise RuntimeError(f"OAuth 授权失败 code={code}: {msg}")

    # 授权码从 result.location 的 URL 参数中提取
    location = (body.get("result") or {}).get("location", "")
    auth_code = ""
    if location:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            auth_code = (qs.get("code") or [""])[0]
        except Exception:
            auth_code = ""
    if not auth_code:
        raise RuntimeError(f"OAuth 授权未返回 code: {body}")

    # 2) oauthLogin 用 code 换主站 token
    payload = {
        "code": auth_code,
        "clientId": CLIENT_ID,
        "redirecturi": OAUTH_REDIRECT_URI,
    }
    status, body, _ = http_request(
        OAUTH_LOGIN_URL,
        method="POST",
        headers=_build_cn_headers(),
        data=payload,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"OAuth 换 token 失败: status={status}, body={body}")

    code = body.get("code", -1)
    if code != 0:
        msg = body.get("msg") or ERR_MSG.get(code, "未知错误")
        raise RuntimeError(f"OAuth 换 token 失败 code={code}: {msg}")

    result = body.get("result") or {}
    token = result.get("token", "")
    userId = result.get("userId", "")
    if not token:
        raise RuntimeError(f"OAuth 换 token 未返回有效 token: {body}")
    return token, userId


def get_last_checkin(token, uid):
    """查询最近签到状态"""
    status, body, _ = http_request(
        LAST_CHECKIN_URL,
        method="POST",
        headers=_build_cn_headers(token=token, uid=uid),
        data={},
    )
    _ensure_api_ok(status, body, "查询签到状态")
    return body


def do_checkin(token, uid):
    """执行签到"""
    status, body, _ = http_request(
        CHECKIN_URL,
        method="POST",
        headers=_build_cn_headers(token=token, uid=uid),
        data={},
    )
    _ensure_api_ok(status, body, "执行签到")
    return body


def get_user_info(token, uid):
    """查询用户信息（含总积分 kwbeans / 经验 expPoints / 创想币 coin）"""
    status, body, _ = http_request(
        USER_INFO_URL,
        method="POST",
        headers=_build_cn_headers(token=token, uid=uid),
        data={},
    )
    _ensure_api_ok(status, body, "查询用户信息")
    return body


def _extract_points(info_body):
    """从 getInfo 返回提取积分信息，返回 dict（缺字段时对应键为 None）。

    实测字段（result.userInfo）：
      kwbeans    总积分（签到累计的主体）
      expPoints  经验值
      coin       创想币（真金白银，通常为 0）
    """
    ui = (info_body.get("result") or {}).get("userInfo") or {}
    return {
        "points": ui.get("kwbeans"),
        "exp": ui.get("expPoints"),
        "coin": ui.get("coin"),
    }


def _append_points(msgs, token, uid):
    """查询用户积分并追加到 msgs；查询失败仅静默跳过，不影响签到结果。"""
    try:
        pts = _extract_points(get_user_info(token, uid))
    except Exception as e:  # noqa
        msgs.append(f"⚠️ 查询积分异常: {e}")
        return
    if pts.get("points") is not None:
        msgs.append(f"💰 当前总积分: {pts['points']}")
    if pts.get("exp") is not None:
        msgs.append(f"⭐ 经验值: {pts['exp']}")
    if pts.get("coin") is not None and pts["coin"] > 0:
        msgs.append(f"🪙 创想币: {pts['coin']}")


# 签到奖励类型（实测 rewards 数组元素为 {rewardType, rewardValue}）
REWARD_TYPE = {
    1: "积分",
    2: "经验",
}


def _append_reward(msgs, last_checkin):
    """从 lastCheckin.rewards 提取并追加本次签到奖励。"""
    rewards = last_checkin.get("rewards")
    if not isinstance(rewards, list) or not rewards:
        return
    parts = []
    for r in rewards:
        if not isinstance(r, dict):
            continue
        label = REWARD_TYPE.get(r.get("rewardType"), f"类型{r.get('rewardType')}")
        parts.append(f"{label}+{r.get('rewardValue')}")
    if parts:
        msgs.append("🎁 本次奖励: " + "，".join(parts))


def run_single(account, password):
    """单个账号完整签到流程，返回 (是否成功, 结果消息列表)"""
    msgs = []

    try:
        token, uid, opener = login(account, password)
        msgs.append(f"✅ 登录成功 uid={uid}")

        # 1) 查询最近签到状态（判断今天是否已签到）
        last = get_last_checkin(token, uid)
        # 兜底：若查询返回 token 无效，走 OAuth 换 token 重试
        if last.get("code") == 4:
            msgs.append("⚠️ 直接登录态 Token 无效，切换 OAuth 授权码方式…")
            token, uid = oauth_exchange_token(token, opener)
            last = get_last_checkin(token, uid)

        if last.get("code") != 0:
            msgs.append(f"❌ 查询签到状态失败 code={last.get('code')}: {last.get('msg')}")
            return False, msgs

        result = last.get("result") or {}
        # 实测字段：result.hasDid 表示今日是否已签到
        if result.get("hasDid"):
            msgs.append("ℹ️ 今日已签到，无需重复")
            last_checkin = result.get("lastCheckin") or {}
            days = last_checkin.get("continueStep")
            if days is not None:
                msgs.append(f"📅 已连续签到 {days} 天")
            _append_reward(msgs, last_checkin)
            _append_points(msgs, token, uid)
            return True, msgs

        # 2) 执行签到
        resp = do_checkin(token, uid)
        if resp.get("code") == 4:
            # token 无效，走 OAuth 换 token 再签
            msgs.append("⚠️ 直接登录态 Token 无效，切换 OAuth 授权码方式…")
            token, uid = oauth_exchange_token(token, opener)
            resp = do_checkin(token, uid)

        if resp.get("code") != 0:
            msgs.append(f"❌ 签到失败 code={resp.get('code')}: {resp.get('msg')}")
            return False, msgs

        # 3) 签到成功，回查最近状态拿权威的连续天数与奖励
        msgs.append("🎉 签到成功")
        try:
            last2 = get_last_checkin(token, uid)
            if last2.get("code") == 0:
                last_checkin = (last2.get("result") or {}).get("lastCheckin") or {}
                days = last_checkin.get("continueStep")
                if days is not None:
                    msgs.append(f"📅 已连续签到 {days} 天")
                _append_reward(msgs, last_checkin)
            else:
                msgs.append(f"⚠️ 回查签到状态失败 code={last2.get('code')}")
        except Exception as e:  # noqa
            # 签到本身已成功，回查失败不影响结果判定，仅提示
            msgs.append(f"⚠️ 回查签到状态异常: {e}")
        # 4) 查询并显示当前总积分（失败不影响签到结果判定）
        _append_points(msgs, token, uid)
        return True, msgs

    except Exception as e:  # noqa
        msgs.append(f"❌ 异常: {e}")
        return False, msgs


def load_accounts():
    """从环境变量或内置配置读取账号列表

    格式：每行「账号 密码」（账号与密码用空格分隔），多账号用换行分隔。
    """
    raw = os.environ.get(ENV_ACCOUNTS, "").strip()
    if not raw:
        raw = CONFIG_ACCOUNTS.strip()

    accounts = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 账号与密码用空格分隔（split(None,1) 兼容多个连续空格）
        parts = line.split(None, 1)
        if len(parts) == 2:
            accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts


def main():
    print("=" * 56)
    print("创想云国内站 · 自动签到")
    print("=" * 56)

    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号！请设置环境变量 %s 或修改脚本内 CONFIG_ACCOUNTS" % ENV_ACCOUNTS)
        print("   格式示例：每行「账号 密码」（空格分隔），多账号换行分隔")
        return 1

    print(f"共 {len(accounts)} 个账号待签到\n")

    summary = []
    detail_lines = []
    for i, (acc, pwd) in enumerate(accounts, 1):
        masked = mask_account(acc)
        print(f"[{i}/{len(accounts)}] 账号 {masked} …")
        ok, msgs = run_single(acc, pwd)
        for m in msgs:
            print("   " + m)
        summary.append((masked, ok))
        detail_lines.append(f"  {'✅' if ok else '❌'} {masked}：" + "；".join(msgs))
        print()

    print("-" * 56)
    succ = sum(1 for _, ok in summary if ok)
    fail = len(summary) - succ
    print(f"签到结果：成功 {succ} 个，失败 {fail} 个")
    for masked, ok in summary:
        print(f"   {'✅' if ok else '❌'} {masked}")
    print("=" * 56)

    # 汇总推送（青龙 notify.py 存在时自动推送，否则仅打印标题）
    report = "创想云签到结果：成功 %d 个，失败 %d 个\n%s" % (
        succ, fail, "\n".join(detail_lines))
    notify_user("创想云签到", report)

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
