import base64
import gzip
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nanobot.channels.feishu import runtime as feishu_module
from nanobot.channels.feishu.runtime import FeishuChannel
from nanobot.config import loader
from nanobot.config.schema import Config
from nanobot.pairing import store as pairing_store


def _decode_addons(encoded: str) -> dict:
    payload = json.loads(gzip.decompress(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))
    return payload


def _default_feishu_instance(data: dict) -> dict:
    return data["channels"]["feishu"]["instances"][0]


@pytest.mark.asyncio
async def test_feishu_login_writes_credentials_to_active_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config = Config()
    config.channels.feishu = {"enabled": False, "domain": "feishu"}
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(
        feishu_module,
        "qr_register",
        lambda initial_domain="feishu", addons=None: {
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "lark",
        },
    )
    monkeypatch.setattr(
        feishu_module,
        "fetch_feishu_app_identity",
        lambda app_id, app_secret, domain: {
            "displayName": "Voraflare Bot",
            "avatarUrl": "https://example.com/avatar.png",
            "identityFetchedAt": "2026-07-06T00:00:00Z",
        },
    )

    channel = FeishuChannel({"enabled": False, "domain": "feishu"}, None)

    assert await channel.login() is True
    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = _default_feishu_instance(data)
    assert instance["id"] == "default"
    assert instance["appId"] == "cli_app"
    assert instance["appSecret"] == "secret"
    assert instance["domain"] == "lark"
    assert instance["identityKey"] == "lark:cli_app"
    assert instance["enabled"] is True
    assert instance["displayName"] == "Voraflare Bot"
    assert instance["avatarUrl"] == "https://example.com/avatar.png"
    assert instance["identityFetchedAt"] == "2026-07-06T00:00:00Z"


def test_begin_registration_requires_login_url(monkeypatch):
    monkeypatch.setattr(
        feishu_module,
        "_post_registration",
        lambda _base_url, _body: {"device_code": "device"},
    )

    with pytest.raises(RuntimeError, match="login URL"):
        feishu_module._begin_registration()


def test_begin_registration_preserves_login_url(monkeypatch):
    login_url = "https://accounts.feishu.cn/login?device_code=device"
    monkeypatch.setattr(
        feishu_module,
        "_post_registration",
        lambda _base_url, _body: {
            "device_code": "device",
            "verification_uri_complete": login_url,
        },
    )

    assert feishu_module._begin_registration()["qr_url"] == login_url


def test_qr_register_returns_none_on_network_error(monkeypatch):
    def raise_connect_error(_base_url, _body):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(feishu_module, "_post_registration", raise_connect_error)

    assert feishu_module.qr_register() is None


def test_save_registration_result_keeps_credentials_when_identity_fetch_fails(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    loader.save_config(Config(), config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)

    def fail_identity(_app_id, _app_secret, _domain):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(feishu_module, "fetch_feishu_app_identity", fail_identity)

    feishu_module.save_registration_result({
        "app_id": "cli_app",
        "app_secret": "secret",
        "domain": "feishu",
    })

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = _default_feishu_instance(data)
    assert instance["appId"] == "cli_app"
    assert instance["appSecret"] == "secret"
    assert instance["identityKey"] == "feishu:cli_app"
    assert "displayName" not in instance
    assert "avatarUrl" not in instance


def test_save_registration_result_reuses_existing_app_instance(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "default",
                "instanceId": "default",
                "name": "nanobot",
                "enabled": True,
                "appId": "cli_same",
                "appSecret": "old-secret",
                "domain": "feishu",
                "identityKey": "feishu:cli_same",
                "allowFrom": ["approved-user"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(feishu_module, "fetch_feishu_app_identity", lambda *_args: {})

    effective_id = feishu_module.save_registration_result(
        {
            "app_id": "cli_same",
            "app_secret": "rotated-secret",
            "domain": "feishu",
        },
        instance_id="assistant-new",
        name="nanobot assistant-new",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instances = data["channels"]["feishu"]["instances"]
    assert effective_id == "default"
    assert len(instances) == 1
    assert instances[0]["id"] == "default"
    assert instances[0]["name"] == "nanobot"
    assert instances[0]["appSecret"] == "rotated-secret"
    assert instances[0]["allowFrom"] == ["approved-user"]


def test_save_registration_result_resets_access_when_instance_app_changes(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    pairing_path = tmp_path / "pairing.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "assistant-test",
                "instanceId": "assistant-test",
                "name": "old assistant",
                "enabled": True,
                "appId": "cli_old",
                "appSecret": "old-secret",
                "identityKey": "feishu:cli_old",
                "allowFrom": ["old-open-id"],
                "allow_from": ["old-snake-open-id"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(pairing_store, "_store_path", lambda: pairing_path)
    monkeypatch.setattr(feishu_module, "fetch_feishu_app_identity", lambda *_args: {})

    approved_code = pairing_store.generate_code("feishu.assistant-test", "paired-user")
    pairing_store.approve_code(approved_code)
    pending_code = pairing_store.generate_code("feishu.assistant-test", "pending-user")

    feishu_module.save_registration_result(
        {
            "app_id": "cli_new",
            "app_secret": "new-secret",
            "domain": "feishu",
        },
        instance_id="assistant-test",
        name="new assistant",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = data["channels"]["feishu"]["instances"][0]
    assert instance["appId"] == "cli_new"
    assert instance["appSecret"] == "new-secret"
    assert instance["identityKey"] == "feishu:cli_new"
    assert instance["allowFrom"] == []
    assert instance["allow_from"] == []
    assert pairing_store.is_approved("feishu.assistant-test", "paired-user") is False
    assert pairing_store.approve_code(pending_code) is None


def test_save_registration_result_keeps_access_when_only_secret_rotates(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    pairing_path = tmp_path / "pairing.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "assistant-test",
                "instanceId": "assistant-test",
                "name": "same assistant",
                "enabled": True,
                "appId": "cli_same",
                "appSecret": "old-secret",
                "domain": "feishu",
                "identityKey": "feishu:cli_same",
                "allowFrom": ["old-open-id"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(pairing_store, "_store_path", lambda: pairing_path)
    monkeypatch.setattr(feishu_module, "fetch_feishu_app_identity", lambda *_args: {})

    approved_code = pairing_store.generate_code("feishu.assistant-test", "paired-user")
    pairing_store.approve_code(approved_code)
    pending_code = pairing_store.generate_code("feishu.assistant-test", "pending-user")

    feishu_module.save_registration_result(
        {
            "app_id": "cli_same",
            "app_secret": "new-secret",
            "domain": "feishu",
        },
        instance_id="assistant-test",
        name="same assistant",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = data["channels"]["feishu"]["instances"][0]
    assert instance["appSecret"] == "new-secret"
    assert instance["identityKey"] == "feishu:cli_same"
    assert instance["allowFrom"] == ["old-open-id"]
    assert pairing_store.is_approved("feishu.assistant-test", "paired-user") is True
    assert pairing_store.approve_code(pending_code) == (
        "feishu.assistant-test",
        "pending-user",
    )


def test_save_registration_result_resets_access_when_domain_changes(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    pairing_path = tmp_path / "pairing.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "assistant-test",
                "instanceId": "assistant-test",
                "name": "lark assistant",
                "enabled": True,
                "appId": "cli_same",
                "appSecret": "old-secret",
                "domain": "lark",
                "identityKey": "lark:cli_same",
                "allowFrom": ["old-open-id"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(pairing_store, "_store_path", lambda: pairing_path)
    monkeypatch.setattr(feishu_module, "fetch_feishu_app_identity", lambda *_args: {})

    approved_code = pairing_store.generate_code("feishu.assistant-test", "paired-user")
    pairing_store.approve_code(approved_code)

    feishu_module.save_registration_result(
        {
            "app_id": "cli_same",
            "app_secret": "new-secret",
            "domain": "feishu",
        },
        instance_id="assistant-test",
        name="feishu assistant",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = data["channels"]["feishu"]["instances"][0]
    assert instance["domain"] == "feishu"
    assert instance["identityKey"] == "feishu:cli_same"
    assert instance["allowFrom"] == []
    assert pairing_store.is_approved("feishu.assistant-test", "paired-user") is False


def test_sync_saved_identity_boundary_resets_access_after_manual_app_change(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    pairing_path = tmp_path / "pairing.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "assistant-test",
                "instanceId": "assistant-test",
                "name": "manual assistant",
                "enabled": True,
                "appId": "cli_new",
                "appSecret": "secret",
                "domain": "feishu",
                "identityKey": "feishu:cli_old",
                "allowFrom": ["old-open-id"],
                "allow_from": ["old-snake-open-id"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(pairing_store, "_store_path", lambda: pairing_path)

    approved_code = pairing_store.generate_code("feishu.assistant-test", "paired-user")
    pairing_store.approve_code(approved_code)
    pending_code = pairing_store.generate_code("feishu.assistant-test", "pending-user")

    assert feishu_module.sync_saved_feishu_identity_boundary(
        instance_id="assistant-test",
        app_id="cli_new",
        domain="feishu",
    ) is True

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = data["channels"]["feishu"]["instances"][0]
    assert instance["identityKey"] == "feishu:cli_new"
    assert instance["allowFrom"] == []
    assert instance["allow_from"] == []
    assert pairing_store.is_approved("feishu.assistant-test", "paired-user") is False
    assert pairing_store.approve_code(pending_code) is None


def test_sync_saved_identity_boundary_backfills_marker_without_resetting_access(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    pairing_path = tmp_path / "pairing.json"
    config = Config()
    config.channels.feishu = {
        "instances": [
            {
                "id": "assistant-test",
                "instanceId": "assistant-test",
                "name": "existing assistant",
                "enabled": True,
                "appId": "cli_existing",
                "appSecret": "secret",
                "domain": "feishu",
                "allowFrom": ["old-open-id"],
            }
        ]
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)
    monkeypatch.setattr(pairing_store, "_store_path", lambda: pairing_path)

    approved_code = pairing_store.generate_code("feishu.assistant-test", "paired-user")
    pairing_store.approve_code(approved_code)

    assert feishu_module.sync_saved_feishu_identity_boundary(
        instance_id="assistant-test",
        app_id="cli_existing",
        domain="feishu",
    ) is False

    data = json.loads(config_path.read_text(encoding="utf-8"))
    instance = data["channels"]["feishu"]["instances"][0]
    assert instance["identityKey"] == "feishu:cli_existing"
    assert instance["allowFrom"] == ["old-open-id"]
    assert pairing_store.is_approved("feishu.assistant-test", "paired-user") is True


def test_sync_saved_identity_boundary_preserves_legacy_flat_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config = Config()
    config.channels.feishu = {
        "enabled": True,
        "appId": "cli_existing",
        "appSecret": "secret",
        "domain": "feishu",
        "allowFrom": ["old-open-id"],
    }
    loader.save_config(config, config_path)
    monkeypatch.setattr(loader, "_current_config_path", config_path)

    assert feishu_module.sync_saved_feishu_identity_boundary(
        instance_id="default",
        app_id="cli_existing",
        domain="feishu",
    ) is False

    saved = json.loads(config_path.read_text(encoding="utf-8"))["channels"]["feishu"]
    assert saved["appId"] == "cli_existing"
    assert saved["appSecret"] == "secret"
    assert saved["identityKey"] == "feishu:cli_existing"
    assert saved["allowFrom"] == ["old-open-id"]
    assert "instances" not in saved


@pytest.mark.asyncio
async def test_feishu_login_creates_missing_active_config(monkeypatch, tmp_path):
    missing_config = tmp_path / "missing.json"
    monkeypatch.setattr(loader, "_current_config_path", missing_config)
    monkeypatch.setattr(
        feishu_module,
        "qr_register",
        lambda initial_domain="feishu", addons=None: {
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "feishu",
        },
    )

    channel = FeishuChannel({}, None)

    assert await channel.login() is True
    assert missing_config.exists()
    data = json.loads(missing_config.read_text(encoding="utf-8"))
    instance = _default_feishu_instance(data)
    assert instance["id"] == "default"
    assert instance["appId"] == "cli_app"
    assert instance["appSecret"] == "secret"
    assert instance["domain"] == "feishu"


def test_begin_registration_appends_addons(monkeypatch):
    login_url = "https://accounts.feishu.cn/login?device_code=device"
    monkeypatch.setattr(
        feishu_module,
        "_post_registration",
        lambda _base_url, _body: {
            "device_code": "device",
            "verification_uri_complete": login_url,
        },
    )
    addons = {
        "scopes": {"tenant": ["cardkit:card:write"]},
        "events": {"items": {"tenant": ["im.message.recalled_v1"]}},
    }
    begin = feishu_module._begin_registration(addons=addons)
    assert begin["qr_url"].startswith(login_url)
    query = parse_qs(urlparse(begin["qr_url"]).query)
    assert len(query["addons"]) == 1
    assert _decode_addons(query["addons"][0]) == addons


def test_begin_registration_without_addons_preserves_url(monkeypatch):
    login_url = "https://accounts.feishu.cn/login?device_code=device"
    monkeypatch.setattr(
        feishu_module,
        "_post_registration",
        lambda _base_url, _body: {
            "device_code": "device",
            "verification_uri_complete": login_url,
        },
    )
    assert feishu_module._begin_registration()["qr_url"] == login_url


def test_resolve_qr_login_addons_defaults():
    addons = feishu_module._resolve_qr_login_addons(["cardkit:card:write"], ["im.message.recalled_v1"])
    assert addons == {
        "scopes": {"tenant": ["cardkit:card:write"]},
        "events": {"items": {"tenant": ["im.message.recalled_v1"]}},
    }


def test_resolve_qr_login_addons_overrides():
    addons = feishu_module._resolve_qr_login_addons(["a"], ["b", "c"])
    assert addons == {
        "scopes": {"tenant": ["a"]},
        "events": {"items": {"tenant": ["b", "c"]}},
    }


def test_resolve_qr_login_addons_empty_returns_none():
    assert feishu_module._resolve_qr_login_addons([], []) is None
    assert feishu_module._resolve_qr_login_addons(["a"], []) == {"scopes": {"tenant": ["a"]}}


def test_login_addons_from_config_object_defaults():
    from nanobot.channels.feishu.config import FeishuConfig

    conf = FeishuConfig()
    addons = feishu_module._login_addons_from_config(conf)
    assert addons == {
        "scopes": {"tenant": ["cardkit:card:write"]},
        "events": {"items": {"tenant": ["im.message.recalled_v1"]}},
    }


def test_login_addons_from_config_object_disables_category():
    from nanobot.channels.feishu.config import FeishuConfig

    conf = FeishuConfig(qr_login_events=[])
    addons = feishu_module._login_addons_from_config(conf)
    assert addons == {"scopes": {"tenant": ["cardkit:card:write"]}}


def test_login_addons_from_config_dict_camel_and_empty():
    addons = feishu_module._login_addons_from_config(
        {"qrLoginScopes": [], "qrLoginEvents": []}
    )
    assert addons is None


def test_login_addons_from_config_dict_custom():
    addons = feishu_module._login_addons_from_config(
        {"qrLoginEvents": ["im.message.receive_v1"], "qrLoginScopes": ["im:message.group_msg"]}
    )
    assert addons == {
        "scopes": {"tenant": ["im:message.group_msg"]},
        "events": {"items": {"tenant": ["im.message.receive_v1"]}},
    }
