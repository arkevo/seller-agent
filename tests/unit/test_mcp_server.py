# Author: Green Mountain Systems AI Inc.
# Donated to IAB Tech Lab

"""Unit tests for MCP Server tools.

Covers:
- get_setup_status reports incomplete when name is "Default Publisher"
- get_setup_status reports complete when identity + ad server + media kit configured
- health_check returns healthy status
- get_config returns non-secret config values
"""

import json
import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_settings(**overrides):
    """Create a plain namespace settings object (avoids MagicMock serialization issues)."""
    defaults = {
        "seller_organization_name": "Default Publisher",
        "seller_organization_id": "org-001",
        "gam_network_code": None,
        "freewheel_sh_mcp_url": None,
        "ssp_connectors": "",
        "ssp_routing_rules": "",
        "ad_server_type": "google_ad_manager",
        "gam_enabled": False,
        "freewheel_enabled": False,
        "freewheel_inventory_mode": "deals_only",
        "default_currency": "USD",
        "default_price_floor_cpm": 1.0,
        "approval_gate_enabled": False,
        "approval_timeout_hours": 24,
        "approval_required_flows": "",
        "yield_optimization_enabled": False,
        "programmatic_floor_multiplier": 1.0,
        "preferred_deal_discount_max": 0.15,
        "agent_registry_enabled": False,
        "agent_registry_url": "",
        "pubmatic_mcp_url": "",
        "index_exchange_api_url": "",
        "magnite_api_url": "",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestGetSetupStatus:
    """get_setup_status tool tests."""

    @pytest.mark.asyncio
    async def test_incomplete_with_default_publisher(self):
        from ad_seller.interfaces.mcp_server import get_setup_status

        settings = _make_settings(seller_organization_name="Default Publisher")
        storage = AsyncMock()

        with patch(
            "ad_seller.interfaces.mcp_server._get_settings", return_value=settings
        ), patch(
            "ad_seller.interfaces.mcp_server._get_storage", new_callable=AsyncMock, return_value=storage
        ):
            result = json.loads(await get_setup_status())

        assert result["publisher_identity"]["configured"] is False
        assert result["setup_complete"] is False
        assert "incomplete" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_complete_when_fully_configured(self):
        from ad_seller.interfaces.mcp_server import get_setup_status

        settings = _make_settings(
            seller_organization_name="My Publisher",
            gam_network_code="12345",
        )
        storage = AsyncMock()

        # Mock MediaKitService to return some packages
        mock_pkg = MagicMock()
        mock_service = AsyncMock()
        mock_service.list_packages_public.return_value = [mock_pkg]

        # The function does: from ..engines.media_kit_service import MediaKitService
        # We patch the module in sys.modules so the local import picks up our mock.
        fake_module = MagicMock()
        fake_module.MediaKitService.return_value = mock_service

        with patch(
            "ad_seller.interfaces.mcp_server._get_settings", return_value=settings
        ), patch(
            "ad_seller.interfaces.mcp_server._get_storage", new_callable=AsyncMock, return_value=storage
        ), patch.dict(
            "sys.modules",
            {"ad_seller.engines.media_kit_service": fake_module},
        ):
            result = json.loads(await get_setup_status())

        assert result["publisher_identity"]["configured"] is True
        assert result["ad_server"]["configured"] is True
        assert result["media_kit"]["configured"] is True
        assert result["setup_complete"] is True
        assert "fully configured" in result["message"].lower()


class TestHealthCheck:
    """health_check tool tests."""

    @pytest.mark.asyncio
    async def test_healthy_status(self):
        from ad_seller.interfaces.mcp_server import health_check

        settings = _make_settings()
        storage = AsyncMock()

        with patch(
            "ad_seller.interfaces.mcp_server._get_settings", return_value=settings
        ), patch(
            "ad_seller.interfaces.mcp_server._get_storage", new_callable=AsyncMock, return_value=storage
        ):
            result = json.loads(await health_check())

        assert result["status"] == "healthy"
        assert result["checks"]["storage"] == "ok"

    @pytest.mark.asyncio
    async def test_degraded_when_storage_fails(self):
        from ad_seller.interfaces.mcp_server import health_check

        settings = _make_settings()

        with patch(
            "ad_seller.interfaces.mcp_server._get_settings", return_value=settings
        ), patch(
            "ad_seller.interfaces.mcp_server._get_storage",
            new_callable=AsyncMock,
            side_effect=Exception("Storage unavailable"),
        ):
            result = json.loads(await health_check())

        assert result["status"] == "degraded"
        assert "error" in result["checks"]["storage"]


class TestGetConfig:
    """get_config tool tests."""

    @pytest.mark.asyncio
    async def test_returns_non_secret_values(self):
        from ad_seller.interfaces.mcp_server import get_config

        settings = _make_settings(
            seller_organization_name="My Publisher",
            seller_organization_id="org-001",
            default_currency="USD",
            default_price_floor_cpm=2.0,
        )

        with patch(
            "ad_seller.interfaces.mcp_server._get_settings", return_value=settings
        ):
            result = json.loads(await get_config())

        assert result["publisher"]["name"] == "My Publisher"
        assert result["publisher"]["org_id"] == "org-001"
        assert result["pricing"]["currency"] == "USD"
        assert result["pricing"]["floor_cpm"] == 2.0
        # Ensure no secret fields like API keys
        config_str = json.dumps(result)
        assert "api_key" not in config_str.lower()
        assert "anthropic" not in config_str.lower()
