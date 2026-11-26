"""在本机配置登录目标，用当前 Windows 用户的 DPAPI 保护凭据和会话。"""

import argparse
import getpass
import ipaddress
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import SplitResult, unquote, urljoin, urlsplit
from uuid import UUID, uuid4


class SiteConfigError(Exception):
    """保存可直接向本机用户或 DSH 展示的固定错误说明。"""


def _path(value: str) -> str:
    """规范化同源路径，拒绝可能被代理或浏览器二次解释的分隔符与目录跳转。"""
    if not value.startswith("/") or re.search(r"%(?:2[fFeE]|5[cC]|25|3[fFbB]|23)|%(?![0-9a-fA-F]{2})", value):
        raise ValueError("路径编码无效")
    path = unquote(value, errors="strict")
    if ("//" in path or any(part in {".", ".."} for part in path.split("/"))
            or re.search(r"[\\?#;\x00-\x1f\x7f]", path)):
        raise ValueError("路径分隔无效")
    return path


def _address(url: str) -> tuple[SplitResult, tuple[str, str, int], str]:
    """按协议、主机、有效端口和规范化路径解析请求，凭据仅允许走 HTTPS 或回环。"""
    if not isinstance(url, str) or re.search(r"[\\\x00-\x20\x7f]", url):
        raise ValueError("地址含无效字符")
    parts = urlsplit(url)
    host = parts.hostname
    if (parts.scheme not in {"https", "http"} or not host or parts.username is not None
            or "%" in parts.netloc or parts.port == 0):
        raise ValueError("地址无效")
    host = host.encode("idna").decode("ascii").lower()
    if ":" not in host and not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("主机名无效")
    if parts.scheme == "http" and host != "localhost":
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("HTTP 仅用于回环")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts, (parts.scheme, host, port), _path(parts.path or "/")


@dataclass(frozen=True, kw_only=True, repr=False)
class SiteConfig:
    """保存单环境的页面检查点、账号标志、表单定位和明确授权的请求路径。"""

    environment: str
    login_url: str
    protected_url: str
    account_selector: str
    account_text: str
    post_paths: tuple[str, ...]
    read_paths: tuple[str, ...] = ()
    username_selector: str = "input[autocomplete=username], input[name=username], input[type=email]"
    password_selector: str = "input[type=password]"
    submit_selector: str = "button[type=submit], input[type=submit], form button:not([type])"
    challenge_selector: str = "input[autocomplete=one-time-code]"
    invalid_selector: str = ""
    config_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        """校验页面同源、授权列表和必要检查点，把登录与受保护路径加入读取范围。"""
        try:
            if str(UUID(self.config_id)) != self.config_id:
                raise ValueError
            login, origin, login_path = _address(self.login_url)
            protected, other_origin, protected_path = _address(self.protected_url)
            if (origin != other_origin or any((login.query, login.fragment, protected.query, protected.fragment))
                    or login_path == protected_path):
                raise ValueError
            for key in ("environment", "account_selector", "account_text", "username_selector",
                        "password_selector", "submit_selector"):
                if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                    raise ValueError
            if not all(isinstance(value, str) for value in (self.challenge_selector, self.invalid_selector)):
                raise ValueError
            if not all(isinstance(paths, (list, tuple)) for paths in (self.read_paths, self.post_paths)):
                raise ValueError
            reads = tuple(dict.fromkeys((login_path, protected_path, *(_path(p) for p in self.read_paths))))
            posts = tuple(dict.fromkeys(_path(p) for p in self.post_paths))
            if (not posts or any("*" in p for p in posts)
                    or any("*" in p and (not p.endswith("/*") or p.count("*") != 1) for p in reads)):
                raise ValueError
            object.__setattr__(self, "read_paths", reads)
            object.__setattr__(self, "post_paths", posts)
        except (AttributeError, TypeError, ValueError):
            raise SiteConfigError("站点配置无效。请核对同源的登录与受保护页面、授权路径及表单定位；远程站点需要 HTTPS。") from None

    def allows(self, url: str, method: str) -> bool:
        """按同一来源核对每次请求，读取可选目录通配，登录提交只允许明确的精确路径。"""
        try:
            _, origin, path = _address(url)
            _, configured_origin, _ = _address(self.login_url)
        except (TypeError, ValueError):
            return False
        if origin != configured_origin:
            return False
        if method == "POST":
            return path in self.post_paths
        if method in {"GET", "HEAD"}:
            return any(path.startswith(p[:-1]) if p.endswith("/*") else path == p for p in self.read_paths)
        return False

    def at_page(self, actual_url: str, configured_url: str) -> bool:
        """比较实际地址与配置检查点，统一处理根路径、国际化路径和浏览器规范化的端口。"""
        try:
            return self.allows(actual_url, "GET") and _address(actual_url)[1:] == _address(configured_url)[1:]
        except (TypeError, ValueError):
            return False


def _dpapi(data: bytes, purpose: str, *, encrypt: bool) -> bytes:
    """调用当前用户 DPAPI 密封或打开数据，以配置编号和用途隔离配置与会话密文。"""
    if sys.platform != "win32":
        raise SiteConfigError("站点凭据和会话需要 Windows 当前用户的本地加密存储。")
    import pywintypes
    import win32crypt

    entropy = ("WebTest Atlas/" + purpose).encode("ascii")
    try:
        # CRYPTPROTECT_UI_FORBIDDEN 避免宿主后台进程因等待系统对话框而挂起。
        if encrypt:
            return win32crypt.CryptProtectData(data, None, entropy, None, None, 1)
        return win32crypt.CryptUnprotectData(data, entropy, None, None, 1)[1]
    except pywintypes.error:
        raise SiteConfigError("无法打开或保存本机加密数据。请使用原 Windows 用户，或重新配置测试站点。") from None


def _database_path() -> Path:
    """从当前项目或显式本机目录定位站点保险库。"""
    return Path(os.environ.get("WEBTEST_ATLAS_SITE_HOME", ".local/site")) / "site.sqlite3"


@contextmanager
def _database(path: Path, *, create: bool = False) -> Iterator[sqlite3.Connection]:
    """读写单行保险库，同一事务中替换配置并清除上一份会话。"""
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise SiteConfigError("尚未配置测试站点。请先在本机运行站点配置命令。")
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS current_site (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    config_id TEXT NOT NULL, profile BLOB NOT NULL, state BLOB
                )""")
            yield db
    except (OSError, sqlite3.Error):
        raise SiteConfigError("无法读写本机站点配置。请核对目录权限、可用空间与配置文件。") from None


def _seal(value: dict, purpose: str) -> bytes:
    """先在内存序列化对象，再把 DPAPI 密文交给数据库。"""
    return _dpapi(json.dumps(value, ensure_ascii=False).encode("utf-8"), purpose, encrypt=True)


def _unseal(ciphertext: bytes, purpose: str) -> dict:
    """用当前 Windows 用户打开指定用途的密文，损坏时仅公开固定原因。"""
    try:
        value = json.loads(_dpapi(ciphertext, purpose, encrypt=False))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise SiteConfigError("本机加密数据格式无效。请重新配置测试站点。") from None


@dataclass(frozen=True)
class SiteProfile:
    """供执行器使用一个配置版本，账号、密码和页面检查点不会进入对象摘要。"""

    config: SiteConfig = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    _path: Path = field(repr=False)

    def load_state(self) -> dict | None:
        """只读取当前配置编号的会话，过期执行器不得使用新配置下的账号状态。"""
        with _database(self._path) as db:
            row = db.execute("SELECT state FROM current_site WHERE config_id = ?",
                             (self.config.config_id,)).fetchone()
        if row is None:
            raise SiteConfigError("站点配置已变更。请按新配置重新发起登录。")
        return None if row[0] is None else _unseal(row[0], self.config.config_id + "/state")

    def save_state(self, state: dict) -> None:
        """以配置版本为条件提交已核对的会话，重配后的旧运行不能覆盖新状态。"""
        encrypted = _seal(state, self.config.config_id + "/state")
        with _database(self._path) as db:
            cursor = db.execute("UPDATE current_site SET state = ? WHERE config_id = ?",
                                (encrypted, self.config.config_id))
            if cursor.rowcount != 1:
                raise SiteConfigError("站点配置已变更。请按新配置重新发起登录。")


def save_profile(config: SiteConfig, username: str, password: str) -> SiteProfile:
    """生成新的配置编号，原子保存账号、配置密文并清除旧会话。"""
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise SiteConfigError("请填写测试账号和密码。")
    config = replace(config, config_id=str(uuid4()))
    encrypted = _seal({"config": asdict(config), "username": username, "password": password},
                      config.config_id + "/profile")
    path = _database_path()
    with _database(path, create=True) as db:
        db.execute("INSERT OR REPLACE INTO current_site VALUES (1, ?, ?, NULL)", (config.config_id, encrypted))
    return SiteProfile(config, username, password, path)


def load_profile() -> SiteProfile:
    """加载当前配置和凭据，传回执行器；普通错误从不回显账号或配置内容。"""
    path = _database_path()
    with _database(path) as db:
        row = db.execute("SELECT config_id, profile FROM current_site WHERE singleton = 1").fetchone()
    if not row:
        raise SiteConfigError("尚未配置测试站点。请先在本机运行站点配置命令。")
    try:
        config_id = str(UUID(row[0]))
        payload = _unseal(row[1], config_id + "/profile")
        config = SiteConfig(**payload["config"])
        username, password = payload["username"], payload["password"]
        if config.config_id != config_id or not all(isinstance(v, str) and v for v in (username, password)):
            raise ValueError
        return SiteProfile(config, username, password, path)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SiteConfigError("本机站点配置格式无效。请重新配置测试站点。") from None


def _ask(label: str, default: str = "") -> str:
    """读取本机终端的一项非密码配置，回车沿用可见默认值。"""
    hint = f" [{default}]" if default else ""
    return input(f"{label}{hint}：").strip() or default


def _paths(label: str, default: str = "") -> tuple[str, ...]:
    """接收用户用逗号分隔的同源授权路径，保留逐项校验所需的原文。"""
    return tuple(p.strip() for p in _ask(label, default).replace("，", ",").split(",") if p.strip())


def demo_config(username: str) -> SiteConfig:
    """按 Compose 当前回环端口预填可重复验收的站点范围，期望账号为输入的合成用户。"""
    try:
        port = int(os.environ.get("WEBTEST_ATLAS_DEMO_PORT", "18765"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SiteConfigError("示例站端口无效，请核对 WEBTEST_ATLAS_DEMO_PORT。") from None
    origin = f"http://127.0.0.1:{port}"
    return SiteConfig(environment="本地示例", login_url=origin + "/login", protected_url=origin + "/app",
                      username_selector="#username", password_selector="#password",
                      submit_selector="button[type=submit]", account_selector="#account", account_text=username,
                      challenge_selector="#challenge", invalid_selector="#login-error",
                      post_paths=("/login", "/challenge"), read_paths=("/challenge",))


def _custom_config(username: str) -> SiteConfig:
    """从用户指定的普通表单站点收集最少检查点，可按需补充自定义定位及请求范围。"""
    environment = _ask("环境名称", "测试环境")
    login_url = _ask("完整登录页面地址")
    if not login_url:
        raise SiteConfigError("请填写完整登录页面地址。")
    try:
        _, _, login_path = _address(login_url)
        protected_url = urljoin(login_url, _ask("仅登录后可访问的页面地址或路径"))
    except ValueError:
        raise SiteConfigError("页面地址无效。请填写 HTTPS 站点或本机回环站点的完整登录地址。") from None
    account_selector = _ask("受保护页面上显示当前账号的 CSS 选择器")
    account_text = _ask("该元素的完整可见文字", username)
    read_paths = _paths("另需读取的同源路径，用逗号分隔，可用 /assets/*，无需补充时回车")
    post_paths = _paths("允许提交登录或人工挑战的精确路径，用逗号分隔", login_path)
    selectors = {}
    if _ask("自定义表单、凭据错误或人工挑战的定位？输入 y 开启", "n").lower() == "y":
        for name, label in (("username_selector", "账号输入框"), ("password_selector", "密码输入框"),
                            ("submit_selector", "登录按钮"), ("challenge_selector", "验证码或 MFA 提示"),
                            ("invalid_selector", "凭据错误提示")):
            selectors[name] = _ask(label + " CSS 选择器", getattr(SiteConfig, name))
    return SiteConfig(environment=environment, login_url=login_url, protected_url=protected_url,
                      account_selector=account_selector, account_text=account_text,
                      read_paths=read_paths, post_paths=post_paths, **selectors)


def main() -> int:
    """执行独立于对话的本机配置命令，密码只在可隐藏输入的交互终端读取。"""
    parser = argparse.ArgumentParser(description="配置 Atlas 登录站点与测试账号")
    parser.add_argument("--demo", action="store_true", help="预填本地 Compose 合成站点")
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise SiteConfigError("请在本机交互终端中运行配置命令，以便隐藏密码输入。")
        username = _ask("测试账号", "atlas-demo" if args.demo else "")
        config = demo_config(username) if args.demo else _custom_config(username)
        password = getpass.getpass("测试账号密码（隐藏输入）：")
        save_profile(config, username, password)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消站点配置。", file=sys.stderr)
        return 1
    except SiteConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("站点配置已保存。在 DSH 中输入“登录测试后台”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
