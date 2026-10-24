"""微软正版登录：设备码授权 → Xbox Live → XSTS → Minecraft。

完整链路（与官方启动器一致）：
    1. POST /consumers/oauth2/v2.0/devicecode              申请设备码（浏览器里输入 code）
    2. 轮询 POST /consumers/oauth2/v2.0/token               拿 Microsoft access_token / refresh_token
    3. POST user.auth.xboxlive.com/user/authenticate        换 Xbox Live token（XBL）
    4. POST xsts.auth.xboxlive.com/xsts/authorize           换 XSTS token
    5. POST api.minecraftservices.com/authentication/login_with_xbox  换 Minecraft token
    6. GET  api.minecraftservices.com/minecraft/profile     拿 uuid / 用户名

``client_id`` 必须是使用者自己在 Azure 门户注册的「移动和桌面应用」（启用
「允许公共客户端流」）——这是微软 OAuth 的硬性要求，任何第三方启动器都必须
自带一个应用 ID，所以这里不做内置，改由设置项 ``msa_client_id`` 提供。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..utils.errors import NetworkError, OperationCancelled
from ..utils.log import get as get_logger
from ..utils.net import session
from .account import Account

log = get_logger("auth.microsoft")

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBOX_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

SCOPE = "XboxLive.signin offline_access"
XBOX_RELYING_PARTY = "http://auth.xboxlive.com"
XSTS_RELYING_PARTY = "rp://api.minecraftservices.com/"
RPS_PREFIX = "d="
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: XSTS 的 XErr 错误码 → (原因, 建议)
XSTS_ERRORS: dict[str, tuple[str, str]] = {
    "2148916233": ("该微软账号还没有 Xbox 账号", "请先用这个账号登录 xbox.com 创建 Xbox 档案后重试"),
    "2148916235": ("该账号所在地区不支持 Xbox Live", "需要更换账号所在地区，或改用其它账号"),
    "2148916236": ("账号需要完成年龄验证", "请到 account.microsoft.com 完成成人验证"),
    "2148916237": ("账号需要完成年龄验证", "请到 account.microsoft.com 完成成人验证"),
    "2148916238": ("未成年账号需要加入家庭组", "请让家长账号把你加入 Microsoft 家庭组"),
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class DeviceCodeFlow:
    user_code: str
    device_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str = ""

    @property
    def expires_text(self) -> str:
        minutes = max(1, self.expires_in // 60)
        return f"{minutes} 分钟内有效"


@dataclass
class MinecraftProfile:
    id: str = ""
    name: str = ""
    skins: list[dict] = field(default_factory=list)
    capes: list[dict] = field(default_factory=list)


@dataclass
class SignInResult:
    """一次完整登录（或刷新）的结果，可直接转成 ``Account``。"""

    access_token: str = ""
    expires_at: float = 0.0
    refresh_token: str = ""
    profile: MinecraftProfile = field(default_factory=MinecraftProfile)
    xuid: str = ""
    ms_access_token: str = ""

    @property
    def username(self) -> str:
        return self.profile.name


# --------------------------------------------------------------------------- #
# 纯函数：请求体与响应解析（便于离线自检）
# --------------------------------------------------------------------------- #
def device_code_body(client_id: str) -> dict:
    return {"client_id": client_id, "scope": SCOPE}


def token_poll_body(client_id: str, device_code: str) -> dict:
    return {"client_id": client_id, "grant_type": DEVICE_GRANT, "device_code": device_code}


def refresh_body(client_id: str, refresh_token: str) -> dict:
    return {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }


def xbox_auth_body(ms_access_token: str) -> dict:
    return {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"{RPS_PREFIX}{ms_access_token}",
        },
        "RelyingParty": XBOX_RELYING_PARTY,
        "TokenType": "JWT",
    }


def xsts_body(xbl_token: str) -> dict:
    return {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": XSTS_RELYING_PARTY,
        "TokenType": "JWT",
    }


def minecraft_login_body(uhs: str, xsts_token: str) -> dict:
    return {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}


def parse_device_code(payload: dict) -> DeviceCodeFlow:
    """解析 devicecode 响应；结构不对时抛可读错误。"""
    if not isinstance(payload, dict) or not payload.get("device_code"):
        raise NetworkError("设备码响应异常", hint=str(payload)[:200])
    return DeviceCodeFlow(
        user_code=str(payload.get("user_code") or ""),
        device_code=str(payload["device_code"]),
        verification_uri=str(payload.get("verification_uri") or "https://www.microsoft.com/link"),
        expires_in=int(payload.get("expires_in") or 900),
        interval=int(payload.get("interval") or 5),
        message=str(payload.get("message") or ""),
    )


def parse_uhs(payload: dict) -> str:
    """取 ``DisplayClaims.xui[0].uhs``（Xbox / XSTS 响应里的 user hash）。"""
    claims = payload.get("DisplayClaims") if isinstance(payload, dict) else None
    if not isinstance(claims, dict):
        return ""
    xui = claims.get("xui")
    if not isinstance(xui, list) or not xui or not isinstance(xui[0], dict):
        return ""
    return str(xui[0].get("uhs") or "")


def parse_profile(payload: dict) -> MinecraftProfile:
    if not isinstance(payload, dict):
        return MinecraftProfile()
    skins = payload.get("skins") if isinstance(payload.get("skins"), list) else []
    capes = payload.get("capes") if isinstance(payload.get("capes"), list) else []
    return MinecraftProfile(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        skins=[item for item in skins if isinstance(item, dict)],
        capes=[item for item in capes if isinstance(item, dict)],
    )


def xsts_error(payload: dict) -> tuple[str, str] | None:
    """XSTS 401 响应里的 XErr → (原因, 建议)。"""
    if not isinstance(payload, dict):
        return None
    code = str(payload.get("XErr") or payload.get("xerr") or "")
    return XSTS_ERRORS.get(code)


def _json(response) -> dict:  # noqa: ANN001 - requests.Response
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _short(payload: dict, limit: int = 160) -> str:
    text = str(payload.get("error_description") or payload.get("error") or payload)
    text = " ".join(text.split())
    return text[:limit]


def _sleep_cancellable(seconds: float, should_cancel: Callable[[], bool] | None = None) -> None:
    """可被取消打断的 sleep（轮询间隔 5s 起步，关窗口时不该干等）。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("已取消登录")
        time.sleep(min(0.2, remaining))


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
@dataclass
class MicrosoftAuth:
    """微软设备码登录客户端。未配置 client_id 时所有网络方法都会给出明确提示。"""

    client_id: str = ""
    timeout: float = 20.0

    # ------------------------------------------------------------------ #
    def _require_client_id(self) -> str:
        client_id = (self.client_id or "").strip()
        if not client_id:
            raise NetworkError(
                "尚未配置微软应用 ID",
                hint="请在「设置 → 账号」填入 Azure 应用 ID（client_id），或在登录窗口里填写后重试",
            )
        return client_id

    def _post(self, url: str, body: dict, *, headers: dict | None = None, form: bool = False) -> tuple[int, dict]:
        """POST 一个请求并返回 (状态码, JSON)。

        ``form=True`` 用于 Azure AD 的 devicecode / token 端点——它们只接受
        ``application/x-www-form-urlencoded``；Xbox Live / XSTS / Minecraft 接口
        则要求 JSON。混用会被直接 400。
        """
        try:
            if form:
                response = session().post(url, data=body, headers=headers or {}, timeout=self.timeout)
            else:
                response = session().post(url, json=body, headers=headers or {}, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - 统一包装成 NetworkError
            raise NetworkError(f"请求失败：{url}", hint=str(exc)) from exc
        return response.status_code, _json(response)

    def _get(self, url: str, *, headers: dict | None = None) -> tuple[int, dict]:
        try:
            response = session().get(url, headers=headers or {}, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise NetworkError(f"请求失败：{url}", hint=str(exc)) from exc
        return response.status_code, _json(response)

    # ------------------------------------------------------------------ #
    # 设备码
    # ------------------------------------------------------------------ #
    def request_device_code(self) -> DeviceCodeFlow:
        client_id = self._require_client_id()
        status, payload = self._post(DEVICE_CODE_URL, device_code_body(client_id), form=True)
        if status != 200:
            raise NetworkError(
                f"申请设备码失败（HTTP {status}）",
                hint=_short(payload) or "请检查 client_id 是否正确、应用是否启用了公共客户端流",
            )
        flow = parse_device_code(payload)
        log.info("已获取设备码：%s（%s）", flow.user_code, flow.verification_uri)
        return flow

    def poll_token(self, flow: DeviceCodeFlow, *, should_cancel: Callable[[], bool] | None = None) -> dict:
        """轮询授权结果；返回微软 token 响应（含 access_token / refresh_token）。"""
        client_id = self._require_client_id()
        deadline = time.monotonic() + max(30, int(flow.expires_in or 900))
        interval = max(1, int(flow.interval or 5))
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("已取消登录")
            status, payload = self._post(TOKEN_URL, token_poll_body(client_id, flow.device_code), form=True)
            error = str(payload.get("error") or "")
            if status == 200 and payload.get("access_token"):
                log.info("微软账号授权成功")
                return payload
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    interval += 2
                _sleep_cancellable(interval, should_cancel)
                continue
            if error == "expired_token":
                raise NetworkError("设备码已过期", hint="请重新点击「开始登录」获取新的设备码")
            if error == "authorization_declined":
                raise NetworkError("你拒绝了授权", hint="如需登录，请重新开始并允许授权")
            if not error:
                raise NetworkError(f"授权接口返回异常（HTTP {status}）", hint=_short(payload))
            raise NetworkError(f"授权失败：{payload.get('error_description') or error}")
        raise NetworkError("等待授权超时", hint="请重新点击「开始登录」")

    # ------------------------------------------------------------------ #
    # 换票
    # ------------------------------------------------------------------ #
    def sign_in(self, flow: DeviceCodeFlow, *, should_cancel: Callable[[], bool] | None = None) -> SignInResult:
        tokens = self.poll_token(flow, should_cancel=should_cancel)
        return self._complete(
            ms_access_token=str(tokens.get("access_token") or ""),
            refresh_token=str(tokens.get("refresh_token") or ""),
            should_cancel=should_cancel,
        )

    def _complete(
        self,
        *,
        ms_access_token: str,
        refresh_token: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> SignInResult:
        if not ms_access_token:
            raise NetworkError("微软授权没有返回 access_token")
        xbl = self._xbox_authenticate(ms_access_token)
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("已取消登录")
        xsts = self._xsts_authorize(xbl["token"])
        mc = self._minecraft_login(xsts["uhs"], xsts["token"])
        profile = self._fetch_profile(mc["access_token"])
        expires_in = int(mc.get("expires_in") or 86400)
        log.info("正版登录成功：%s（%s）", profile.name, profile.id)
        return SignInResult(
            access_token=str(mc["access_token"]),
            expires_at=time.time() + max(60, expires_in),
            refresh_token=refresh_token,
            profile=profile,
            xuid=xsts["uhs"],
            ms_access_token=ms_access_token,
        )

    def _xbox_authenticate(self, ms_access_token: str) -> dict:
        status, payload = self._post(XBOX_AUTH_URL, xbox_auth_body(ms_access_token))
        if status == 401:
            raise NetworkError("Xbox Live 认证被拒绝", hint="微软令牌可能已失效，请重新登录")
        if status != 200:
            raise NetworkError(f"Xbox Live 认证失败（HTTP {status}）", hint=_short(payload))
        token = str(payload.get("Token") or "")
        uhs = parse_uhs(payload)
        if not token or not uhs:
            raise NetworkError("Xbox Live 认证响应缺少 Token / uhs", hint=_short(payload))
        return {"token": token, "uhs": uhs}

    def _xsts_authorize(self, xbl_token: str) -> dict:
        status, payload = self._post(XSTS_URL, xsts_body(xbl_token))
        if status == 401:
            reason = xsts_error(payload)
            if reason is not None:
                raise NetworkError(reason[0], hint=reason[1])
            raise NetworkError("XSTS 授权被拒绝", hint=_short(payload))
        if status != 200:
            raise NetworkError(f"XSTS 授权失败（HTTP {status}）", hint=_short(payload))
        token = str(payload.get("Token") or "")
        uhs = parse_uhs(payload)
        if not token or not uhs:
            raise NetworkError("XSTS 响应缺少 Token / uhs", hint=_short(payload))
        return {"token": token, "uhs": uhs}

    def _minecraft_login(self, uhs: str, xsts_token: str) -> dict:
        status, payload = self._post(MC_LOGIN_URL, minecraft_login_body(uhs, xsts_token))
        if status == 401:
            raise NetworkError(
                "这个账号还不能玩 Minecraft: Java 版",
                hint="请确认该微软账号已购买游戏；Xbox 档案地区设置也可能影响授权",
            )
        if status != 200 or not payload.get("access_token"):
            raise NetworkError(f"Minecraft 登录失败（HTTP {status}）", hint=_short(payload))
        return payload

    def _fetch_profile(self, mc_access_token: str) -> MinecraftProfile:
        status, payload = self._get(MC_PROFILE_URL, headers={"Authorization": f"Bearer {mc_access_token}"})
        if status == 404:
            raise NetworkError("该账号还没有 Minecraft 档案", hint="请到 minecraft.net 创建游戏档案后再登录")
        if status != 200:
            raise NetworkError(f"获取 Minecraft 档案失败（HTTP {status}）", hint=_short(payload))
        profile = parse_profile(payload)
        if not profile.name or not profile.id:
            raise NetworkError("Minecraft 档案缺少 id / name", hint=_short(payload))
        return profile

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh(self, refresh_token: str) -> dict:
        client_id = self._require_client_id()
        status, payload = self._post(TOKEN_URL, refresh_body(client_id, refresh_token), form=True)
        if status != 200 or not payload.get("access_token"):
            raise NetworkError("微软令牌刷新失败", hint="请在设置里重新登录正版账号")
        return payload

    def refresh_sign_in(self, refresh_token: str) -> SignInResult:
        tokens = self.refresh(refresh_token)
        return self._complete(
            ms_access_token=str(tokens.get("access_token") or ""),
            # 刷新时微软可能不给新的 refresh_token，此时沿用旧的
            refresh_token=str(tokens.get("refresh_token") or refresh_token),
        )

    # ------------------------------------------------------------------ #
    def to_account(self, result: SignInResult, *, client_id: str = "") -> Account:
        return Account(
            type="msa",
            username=result.profile.name,
            uuid=result.profile.id,
            access_token=result.access_token,
            user_type="msa",
            client_id=(client_id or self.client_id or "").strip(),
            xuid=result.xuid,
            refresh_token=result.refresh_token,
            expires_at=result.expires_at,
            properties={},
        )
