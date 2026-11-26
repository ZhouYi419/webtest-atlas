"""用合成表单、账号和会话核对配置范围及 Windows 当前用户加密存储。"""

import sqlite3
import sys
from contextlib import closing
from dataclasses import replace

import pytest

from webtest_atlas import site_config as site


def test_custom_site_scope():
    """自定义域名按来源和路径分配权限，登录提交与页面读取互相独立。"""
    config = site.SiteConfig(environment="合成环境", login_url="https://qa.example.test/login",
                             protected_url="https://qa.example.test/list", account_selector=".account",
                             account_text="测试员", post_paths=("/session",), read_paths=("/assets/*",))
    origin = "https://qa.example.test"
    assert config.allows(origin + "/login", "GET")
    assert config.allows(origin + ":443/list", "HEAD")
    assert config.allows(origin + "/assets/theme/main.css?v=1", "GET")
    assert config.allows(origin + "/session", "POST")
    for method, url in [
        ("GET", origin + "/session"), ("POST", origin + "/assets/main.js"),
        ("POST", origin + "/session/child"), ("GET", origin + "/assetsevil/main.js"),
        ("GET", origin + ":444/login"), ("GET", "https://qa.example.test.invalid/login"),
        ("GET", "https://user@qa.example.test/login"), ("GET", origin + "/assets/../outside"),
        ("GET", origin + "/assets/%2E%2e/outside"), ("GET", origin + "/assets%2foutside"),
        ("GET", origin + "/assets/%252e%252e/outside"), ("GET", origin + "/assets/%5coutside"),
        ("GET", origin + "/assets//outside"), ("GET", origin + "/assets/..;/outside"),
        ("GET", origin + "/assets/%00outside"), ("DELETE", origin + "/session"),
    ]:
        assert not config.allows(url, method)


@pytest.mark.parametrize("override", [
    {"protected_url": "https://example.test/app"}, {"login_url": "http://example.test/login"},
    {"protected_url": "http://127.0.0.1:18765/app#session"}, {"post_paths": ("/write/*",)},
    {"read_paths": ("https://untrusted.test/assets/*",)}, {"read_paths": ("/assets*",)},
    {"post_paths": ()}, {"account_selector": ""}, {"login_url": "http://user@127.0.0.1:18765/login"},
])
def test_invalid_config(override):
    """拒绝不同源的凭据接收地址、歧义路径和缺少账号证据的配置。"""
    with pytest.raises(site.SiteConfigError):
        replace(site.demo_config("fixture"), **override)


def test_missing_profile_does_not_create_storage(monkeypatch, tmp_path):
    """登录前未配置时只给出本机操作提示，没有站点文件的目录维持原状。"""
    home = tmp_path / "unset"
    monkeypatch.setenv("WEBTEST_ATLAS_SITE_HOME", str(home))
    with pytest.raises(site.SiteConfigError, match="尚未配置"):
        site.load_profile()
    assert not home.exists()


def test_normalized_page_checkpoints():
    """浏览器规范化的 Unicode 路径、默认端口和根路径均能与原配置核对。"""
    config = replace(site.demo_config("fixture"), login_url="https://example.test:443/登录",
                     protected_url="https://example.test")
    assert config.at_page("https://example.test/%E7%99%BB%E5%BD%95", config.login_url)
    assert config.at_page("https://example.test/?active=1", config.protected_url)
    assert not config.at_page("https://example.test/other", config.protected_url)
    assert not config.at_page("https://other.example.test/", config.protected_url)


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 当前用户 DPAPI")
def test_encrypted_profile_state_and_reconfiguration(monkeypatch, tmp_path):
    """核对真实加解密、重新加载、旧配置失效及不同代次密文互换被拒。"""
    monkeypatch.setenv("WEBTEST_ATLAS_SITE_HOME", str(tmp_path))
    username, password = "fixture-private-account", "synthetic-test-password"
    data = {"cookies": [{"value": "synthetic-session-secret"}], "origins": []}
    original = site.save_profile(site.demo_config(username), username, password)
    assert original.load_state() is None
    original.save_state(data)
    reopened = site.load_profile()
    assert reopened.config == original.config and reopened.load_state() == data
    assert (reopened.username, reopened.password) == (username, password)
    vault = tmp_path / "site.sqlite3"
    for value in (username, password, "synthetic-session-secret"):
        assert value.encode() not in vault.read_bytes()
        assert value not in repr(reopened)
    with closing(sqlite3.connect(vault)) as db:
        encrypted_state, = db.execute("SELECT state FROM current_site").fetchone()
    latest = site.save_profile(original.config, username, password)
    assert latest.config.config_id != original.config.config_id and latest.load_state() is None
    with pytest.raises(site.SiteConfigError, match="配置已变更"):
        original.load_state()
    with pytest.raises(site.SiteConfigError, match="配置已变更"):
        original.save_state(data)
    # 把前一代的密文拷到当前行，当前代的加密上下文必须拒绝解密。
    with closing(sqlite3.connect(vault)) as db, db:
        db.execute("UPDATE current_site SET state = ?", (encrypted_state,))
    with pytest.raises(site.SiteConfigError, match="加密数据"):
        latest.load_state()


@pytest.mark.skipif(sys.platform != "win32", reason="验证本机凭据录入与 DPAPI")
def test_demo_cli_uses_hidden_prompt(monkeypatch, tmp_path, capsys):
    """通过配置命令的正常交互函数录入合成凭据，输出仅含下一步操作。"""
    monkeypatch.setenv("WEBTEST_ATLAS_SITE_HOME", str(tmp_path))
    monkeypatch.setenv("WEBTEST_ATLAS_DEMO_PORT", "18769")
    monkeypatch.setattr(sys, "argv", ["webtest_atlas.site_config", "--demo"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(site.getpass, "getpass", lambda prompt: "synthetic-hidden")
    assert site.main() == 0
    loaded = site.load_profile()
    assert loaded.username == "atlas-demo" and loaded.password == "synthetic-hidden"
    assert loaded.config.protected_url == "http://127.0.0.1:18769/app"
    output = capsys.readouterr()
    assert "登录测试后台" in output.out and not output.err
    assert "synthetic-hidden" not in output.out


@pytest.mark.skipif(sys.platform != "win32", reason="验证用户站点配置命令与 DPAPI")
def test_custom_site_cli_preserves_user_choices(monkeypatch, tmp_path):
    """命令行可配置用户提供的 HTTPS 站点、独立登录提交路径和自定义表单定位。"""
    monkeypatch.setenv("WEBTEST_ATLAS_SITE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["webtest_atlas.site_config"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(("fixture-account", "QA", "https://qa.example.test/team/login", "/orders/list",
                    ".current-account", "测试员", "/assets/*，/api/me", "/session", "y",
                    "#name", "#pass", "#submit", "#challenge", "#login-failed"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(site.getpass, "getpass", lambda prompt: "fixture-private")
    assert site.main() == 0
    config = site.load_profile().config
    assert config.protected_url == "https://qa.example.test/orders/list"
    assert config.account_text == "测试员" and config.account_selector == ".current-account"
    assert config.allows("https://qa.example.test/session", "POST")
    assert config.allows("https://qa.example.test/assets/theme.css?v=1", "GET")
    assert config.username_selector == "#name" and config.invalid_selector == "#login-failed"
