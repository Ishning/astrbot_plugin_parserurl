import asyncio
import json
from collections.abc import AsyncGenerator
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import qrcode
from bilibili_api import Credential
from bilibili_api.login_v2 import QrCodeLoginEvents
from curl_cffi.requests import AsyncSession

from astrbot.api import logger

from ...config import PluginConfig


QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class BilibiliQrCodeLogin:
    """插件项目内二维码登录实现，上游库因没有了现自行维护一套"""

    def __init__(self):
        self.qrcode_key = ""
        self.qrcode_picture = b""
        self.session = AsyncSession(headers=QR_LOGIN_HEADERS, impersonate="chrome131")

    async def generate_qrcode(self) -> None:
        response = await self.session.get(
            QR_GENERATE_URL, params={"source": "main-fe-header"}
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {payload.get('message') or payload}")

        data = payload.get("data") or {}
        self.qrcode_key = data.get("qrcode_key", "")
        qr_url = data.get("url", "")
        if not self.qrcode_key or not qr_url:
            raise RuntimeError("生成二维码失败: 响应缺少 url 或 qrcode_key")

        image = qrcode.make(qr_url)
        buffer = BytesIO()
        image.save(buffer, "PNG")
        self.qrcode_picture = buffer.getvalue()

    def get_qrcode_picture(self) -> bytes:
        return self.qrcode_picture

    async def check_state(self) -> tuple[QrCodeLoginEvents, Credential | None]:
        response = await self.session.get(
            QR_POLL_URL,
            params={"qrcode_key": self.qrcode_key, "source": "main-fe-header"},
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"二维码状态查询失败: {payload.get('message') or payload}")

        data = payload.get("data") or {}
        code = data.get("code")
        if code == 86101:
            return QrCodeLoginEvents.SCAN, None
        if code == 86090:
            return QrCodeLoginEvents.CONF, None
        if code == 86038:
            return QrCodeLoginEvents.TIMEOUT, None
        if code != 0:
            return QrCodeLoginEvents.TIMEOUT, None

        cookies = self._parse_set_cookie_headers(response.headers.get_list("Set-Cookie"))
        cookies.update(self._cookies_from_url(data.get("url", "")))
        if not cookies.get("SESSDATA"):
            cookies.update(await self._exchange_ticket(data.get("url", "")))

        credential = Credential(
            sessdata=cookies.get("SESSDATA", ""),
            bili_jct=cookies.get("bili_jct", ""),
            buvid3=cookies.get("buvid3", ""),
            buvid4=cookies.get("buvid4", ""),
            dedeuserid=cookies.get("DedeUserID", cookies.get("dedeuserid", "")),
            ac_time_value=data.get("refresh_token", ""),
        )
        return QrCodeLoginEvents.DONE, credential

    @staticmethod
    def _parse_set_cookie_headers(headers: list[str | None]) -> dict[str, str]:
        cookies = {}
        for header in headers:
            if not header:
                continue
            pair = header.split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value.strip()
        return cookies

    @staticmethod
    def _cookies_from_url(url: str) -> dict[str, str]:
        query = parse_qs(urlsplit(url).query)
        return {
            name: query[name][0]
            for name in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")
            if query.get(name)
        }

    async def _exchange_ticket(self, login_url: str) -> dict[str, str]:
        """跟随登录跳转 URL，作为轮询响应未下发 Cookie 时的兜底处理"""
        if not login_url:
            return {}

        response = await self.session.get(
            login_url,
            allow_redirects=True,
        )
        cookies = self._parse_set_cookie_headers(response.headers.get_list("Set-Cookie"))
        cookies.update(
            {
                name: value
                for name, value in self.session.cookies.get_dict().items()
                if value
            }
        )
        return cookies

    async def close(self) -> None:
        await self.session.close()


class BilibiliLogin:
    """哔哩哔哩登录类"""

    def __init__(self, config: PluginConfig):
        self.credential_file = config.data_dir / "cookies" / "bilibili_credential.json"
        self.raw_cookies = config.parser.bilibili.cookies
        self._credential: Credential | None = None

    def _save_credential(self):
        """存储哔哩哔哩登录凭证"""
        if self._credential is None:
            return

        self.credential_file.write_text(
            json.dumps(self._credential.get_cookies(), ensure_ascii=False)
        )

    def _load_credential(self):
        """从文件加载哔哩哔哩登录凭证"""
        if not self.credential_file.exists():
            return

        credential = Credential.from_cookies(
            json.loads(self.credential_file.read_text())
        )
        if credential.has_sessdata():
            self._credential = credential
        else:
            self._credential = None
            logger.warning("哔哩哔哩凭证文件缺少有效的 SESSDATA, 将使用未登录状态")

    async def login_with_qrcode(self) -> bytes:
        """通过二维码登录获取哔哩哔哩登录凭证"""
        self._qr_login = BilibiliQrCodeLogin()
        await self._qr_login.generate_qrcode()

        return self._qr_login.get_qrcode_picture()

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """检查二维码登录状态"""
        scan_tip_pending = True

        for _ in range(30):
            state, credential = await self._qr_login.check_state()
            match state:
                case QrCodeLoginEvents.DONE:
                    assert credential is not None
                    if not credential.has_sessdata():
                        self._credential = None
                        logger.warning("二维码登录未获取到有效的 SESSDATA")
                        yield "登录失败，未获取到有效的 B 站凭证，请重试"
                        break

                    self._credential = credential
                    self._save_credential()
                    await self._qr_login.close()
                    yield "登录成功"
                    break
                case QrCodeLoginEvents.CONF:
                    if scan_tip_pending:
                        yield "二维码已扫描, 请确认登录"
                        scan_tip_pending = False
                case QrCodeLoginEvents.TIMEOUT:
                    yield "二维码过期, 请重新生成"
                    break
            await asyncio.sleep(2)
        else:
            yield "二维码登录超时, 请重新生成"

    def _cookies_to_dict(self, cookies_str: str) -> dict[str, str]:
        """将 cookies 字符串转换为字典"""
        res = {}
        for cookie in cookies_str.split(";"):
            name, value = cookie.strip().split("=", 1)
            res[name] = value
        return res

    async def _init_credential(self):
        """初始化哔哩哔哩登录凭证"""
        if not self.raw_cookies:
            self._load_credential()
            return

        credential = Credential.from_cookies(self._cookies_to_dict(self.raw_cookies))
        if await credential.check_valid():
            logger.info(f"`parser_bili_ck` 有效, 保存到 {self.credential_file}")
            self._credential = credential
            self._save_credential()
        else:
            logger.info(f"`parser_bili_ck` 已过期, 尝试从 {self.credential_file} 加载")
            self._load_credential()

    @property
    async def credential(self) -> Credential | None:
        """哔哩哔哩登录凭证"""

        if self._credential is None:
            await self._init_credential()
            return self._credential

        if not await self._credential.check_valid():
            logger.warning("哔哩哔哩凭证已过期, 请重新配置")
            return None

        if await self._credential.check_refresh():
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                logger.info(f"哔哩哔哩凭证刷新成功, 保存到 {self.credential_file}")
                self._save_credential()
            else:
                logger.warning(
                    "哔哩哔哩凭证刷新需要包含 `SESSDATA`, `ac_time_value` 项"
                )

        return self._credential
