# Author: Green Mountain Systems AI Inc.
# Donated to IAB Tech Lab

"""FreeWheel ad server adapter implementing the AdServerClient interface.

Routes operations to FreeWheel's Streaming Hub (publisher-side) and
Buyer Cloud (demand-side) MCP servers.

Key architectural note from FreeWheel team (FreeWheel):
  Streaming Hub programmatic deals do NOT require IO, Campaign, or Placement.
  Those are direct-sold concepts only. The SH side is simpler: deals are
  created directly via book_deal() which handles both SH and BC.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from .ad_server_base import (
    AdServerAudienceSegment,
    AdServerClient,
    AdServerDeal,
    AdServerInventoryItem,
    AdServerLineItem,
    AdServerOrder,
    AdServerType,
    BookingResult,
)
from .freewheel_mcp_client import FreeWheelMCPClient
from .freewheel_normalizer import (
    micros_to_dollars,
    normalize_audience_segments,
    normalize_booking_result,
    normalize_deal,
    normalize_inventory,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Auth helpers
# =============================================================================


def _build_sh_auth_params(settings: Any) -> dict[str, str]:
    """Build Streaming Hub MCP login params from settings.

    Auth mechanism: OAuth 2.0 Resource Owner Password Credentials Grant
    (RFC 6749). The MCP ``streaming_hub_login`` tool wraps the token
    exchange at ``https://api.freewheel.tv/auth/token``.  Tokens are
    valid for 7 days; rate limit on the token endpoint is 3 req/s.

    Required settings:
        FREEWHEEL_SH_USERNAME — publisher account username
        FREEWHEEL_SH_PASSWORD — publisher account password
        FREEWHEEL_NETWORK_ID  — publisher network/account identifier
    """
    params: dict[str, str] = {}
    if settings.freewheel_sh_username:
        params["username"] = settings.freewheel_sh_username
    if settings.freewheel_sh_password:
        params["password"] = settings.freewheel_sh_password
    if getattr(settings, "freewheel_network_id", None):
        params["network_id"] = settings.freewheel_network_id
    return params


def _build_bc_auth_params(settings: Any) -> dict[str, str]:
    """Build Buyer Cloud (Beeswax) MCP login params from settings.

    Auth mechanism: Session cookie via the Buzz REST API authenticate
    endpoint at ``https://<buzz_key>.api.beeswax.com/rest/authenticate``.
    The MCP ``buyer_cloud_login`` tool wraps this exchange.  Sessions
    last 100 hours by default (30 days with ``keep_logged_in``).

    Required settings:
        FREEWHEEL_BC_EMAIL    — Beeswax account email
        FREEWHEEL_BC_PASSWORD — Beeswax account password
        FREEWHEEL_BC_BUZZ_KEY — Buzz API key (determines API hostname)
    """
    params: dict[str, str] = {}
    if settings.freewheel_bc_email:
        params["email"] = settings.freewheel_bc_email
    if settings.freewheel_bc_password:
        params["password"] = settings.freewheel_bc_password
    if settings.freewheel_bc_buzz_key:
        params["buzz_key"] = settings.freewheel_bc_buzz_key
    # Request extended session (30-day cookie) to reduce re-auth frequency
    params["keep_logged_in"] = "true"
    return params


class FreeWheelAdServerClient(AdServerClient):
    """AdServerClient implementation for FreeWheel (Streaming Hub + Buyer Cloud).

    Phase 1: Read-only (inventory + audiences from SH)
    Phase 2: PD/PA deal booking via SH book_deal
    Phase 3: PG booking + BC campaign management
    """

    ad_server_type = AdServerType.FREEWHEEL

    def __init__(self) -> None:
        self._sh_client = FreeWheelMCPClient()
        self._bc_client: Optional[FreeWheelMCPClient] = None
        self._settings: Any = None

    def _get_settings(self) -> Any:
        if self._settings is None:
            from ..config import get_settings

            self._settings = get_settings()
        return self._settings

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(self) -> None:
        """Connect to FreeWheel Streaming Hub MCP (and BC if configured).

        Authentication uses two separate credential sets:

        - **Streaming Hub (SH):** OAuth 2.0 ROPCG via ``streaming_hub_login``
          MCP tool.  Requires ``FREEWHEEL_SH_USERNAME``, ``FREEWHEEL_SH_PASSWORD``,
          and ``FREEWHEEL_NETWORK_ID``.  Tokens are valid for 7 days.

        - **Buyer Cloud (BC):** Session cookie via ``buyer_cloud_login`` MCP
          tool (wraps Beeswax ``/rest/authenticate``).  Requires
          ``FREEWHEEL_BC_EMAIL``, ``FREEWHEEL_BC_PASSWORD``, and
          ``FREEWHEEL_BC_BUZZ_KEY``.  Sessions last 100 h (30 days with
          ``keep_logged_in``).

        A publisher configures both for PG booking (SH + BC) or just SH for
        PD/PA deals.
        """
        settings = self._get_settings()

        sh_url = settings.freewheel_sh_mcp_url
        if not sh_url:
            raise ConnectionError(
                "FREEWHEEL_SH_MCP_URL not configured. Set it to the Streaming Hub MCP endpoint."
            )

        # --- Streaming Hub auth (OAuth 2.0 ROPCG) ---
        sh_auth = _build_sh_auth_params(settings)

        await self._sh_client.connect(
            url=sh_url,
            auth_params=sh_auth if sh_auth else None,
            login_tool="streaming_hub_login" if sh_auth else None,
        )

        logger.info(
            "Connected to Streaming Hub (network=%s)",
            settings.freewheel_network_id or "default",
        )

        # --- Buyer Cloud auth (Beeswax session cookie) ---
        bc_url = settings.freewheel_bc_mcp_url
        if bc_url:
            self._bc_client = FreeWheelMCPClient()
            bc_auth = _build_bc_auth_params(settings)

            await self._bc_client.connect(
                url=bc_url,
                auth_params=bc_auth if bc_auth else None,
                login_tool="buyer_cloud_login" if bc_auth else None,
            )
            logger.info("Connected to Buyer Cloud (buzz_key=%s)", settings.freewheel_bc_buzz_key or "default")

    async def disconnect(self) -> None:
        """Disconnect from FreeWheel MCP servers."""
        await self._sh_client.disconnect(logout_tool="streaming_hub_logout")
        if self._bc_client:
            await self._bc_client.disconnect(logout_tool="buyer_cloud_logout")

    async def _reconnect_sh(self) -> None:
        """Re-authenticate with Streaming Hub on session expiry."""
        settings = self._get_settings()
        sh_auth = _build_sh_auth_params(settings)
        logger.info("Re-authenticating with Streaming Hub")
        await self._sh_client.reconnect(
            auth_params=sh_auth if sh_auth else None,
            login_tool="streaming_hub_login" if sh_auth else None,
        )

    async def _reconnect_bc(self) -> None:
        """Re-authenticate with Buyer Cloud on session expiry."""
        if not self._bc_client:
            return
        settings = self._get_settings()
        bc_auth = _build_bc_auth_params(settings)
        logger.info("Re-authenticating with Buyer Cloud")
        await self._bc_client.reconnect(
            auth_params=bc_auth if bc_auth else None,
            login_tool="buyer_cloud_login" if bc_auth else None,
        )

    # =========================================================================
    # Inventory & Audiences (Streaming Hub)
    # =========================================================================

    async def list_inventory(
        self,
        *,
        limit: int = 100,
        filter_str: Optional[str] = None,
    ) -> list[AdServerInventoryItem]:
        """List inventory from FreeWheel Streaming Hub.

        Behavior depends on FREEWHEEL_INVENTORY_MODE:
        - "full": Calls list_inventory() to get all available inventory.
        - "deals_only": Calls list_inventory() with a filter to return
          only pre-configured deals/packages the publisher set up for
          agentic selling. This is the default — publishers must opt-in
          to expose their full inventory to the agent.
        """
        settings = self._get_settings()
        inventory_mode = settings.freewheel_inventory_mode

        args: dict[str, Any] = {}
        if limit:
            args["limit"] = limit
        if filter_str:
            args["filter"] = filter_str

        if inventory_mode == "deals_only":
            # Only return pre-configured template deals / packages
            args["type"] = "template_deals"
            logger.info("Inventory mode: deals_only — returning pre-configured deals only")
        else:
            logger.info("Inventory mode: full — returning all available inventory")

        raw = await self._sh_client.call_tool("list_inventory", args)

        # Handle both list and dict-with-items responses
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("items", raw.get("inventory", raw.get("data", [])))
        else:
            items = []

        return normalize_inventory(items)

    async def list_audience_segments(
        self,
        *,
        limit: int = 500,
        filter_str: Optional[str] = None,
    ) -> list[AdServerAudienceSegment]:
        """List audience segments from FreeWheel Streaming Hub."""
        args: dict[str, Any] = {}
        if limit:
            args["limit"] = limit
        if filter_str:
            args["filter"] = filter_str

        raw = await self._sh_client.call_tool("list_audience_segments", args)

        if isinstance(raw, list):
            segments = raw
        elif isinstance(raw, dict):
            segments = raw.get("items", raw.get("segments", raw.get("data", [])))
        else:
            segments = []

        return normalize_audience_segments(segments)

    # =========================================================================
    # Order/IO Operations — NOT SUPPORTED for SH Programmatic
    # =========================================================================

    async def create_order(
        self,
        name: str,
        advertiser_id: str,
        *,
        advertiser_name: Optional[str] = None,
        agency_id: Optional[str] = None,
        notes: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> AdServerOrder:
        """Not supported — FreeWheel SH programmatic deals don't use orders/IOs.

        Use book_deal() instead for programmatic deal creation.
        """
        raise NotImplementedError(
            "FreeWheel Streaming Hub does not use orders/IOs for programmatic deals. "
            "Use book_deal() for deal creation. Orders are a direct-sold concept only."
        )

    async def get_order(self, order_id: str) -> AdServerOrder:
        """Not supported — see create_order() docstring."""
        raise NotImplementedError(
            "FreeWheel Streaming Hub does not use orders/IOs for programmatic deals."
        )

    async def approve_order(self, order_id: str) -> AdServerOrder:
        """Not supported — see create_order() docstring."""
        raise NotImplementedError(
            "FreeWheel Streaming Hub does not use orders/IOs for programmatic deals."
        )

    # =========================================================================
    # Line Item Operations — NOT SUPPORTED on SH (BC support in Phase 3)
    # =========================================================================

    async def create_line_item(
        self,
        order_id: str,
        name: str,
        *,
        cost_micros: int,
        currency: str = "USD",
        cost_type: str = "CPM",
        impressions_goal: int = -1,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        targeting: Optional[dict[str, Any]] = None,
        creative_sizes: Optional[list[tuple[int, int]]] = None,
        external_id: Optional[str] = None,
    ) -> AdServerLineItem:
        """Not supported on SH — campaigns/placements are direct-sold only.

        Phase 3 will implement BC line item creation if FreeWheel team adds
        BC campaign/line item management to the MCP.
        """
        raise NotImplementedError(
            "FreeWheel Streaming Hub does not use line items for programmatic deals. "
            "Use book_deal() instead. BC line item support coming in Phase 3."
        )

    async def update_line_item(
        self,
        line_item_id: str,
        updates: dict[str, Any],
    ) -> AdServerLineItem:
        """Not supported on SH — see create_line_item() docstring."""
        raise NotImplementedError(
            "FreeWheel Streaming Hub does not use line items for programmatic deals."
        )

    # =========================================================================
    # Deal Operations (Streaming Hub + Buyer Cloud)
    # =========================================================================

    async def create_deal(
        self,
        deal_id: str,
        *,
        name: Optional[str] = None,
        deal_type: str = "private_auction",
        floor_price_micros: int = 0,
        fixed_price_micros: int = 0,
        currency: str = "USD",
        buyer_seat_ids: Optional[list[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        targeting: Optional[dict[str, Any]] = None,
    ) -> AdServerDeal:
        """Create a deal via FreeWheel's book_deal MCP tool.

        FreeWheel team's book_deal() creates on both SH and BC.
        """
        args: dict[str, Any] = {
            "deal_id": deal_id,
            "deal_type": deal_type,
        }
        if name:
            args["name"] = name
        if floor_price_micros:
            args["floor_price"] = micros_to_dollars(floor_price_micros)
        if fixed_price_micros:
            args["fixed_price"] = micros_to_dollars(fixed_price_micros)
        if currency != "USD":
            args["currency"] = currency
        if buyer_seat_ids:
            args["buyer_seat_ids"] = buyer_seat_ids
        if start_time:
            args["start_time"] = start_time.isoformat()
        if end_time:
            args["end_time"] = end_time.isoformat()
        if targeting:
            args["targeting"] = targeting

        raw = await self._sh_client.call_tool("book_deal", args)
        return normalize_deal(raw if isinstance(raw, dict) else {"deal_id": deal_id})

    async def update_deal(
        self,
        deal_id: str,
        updates: dict[str, Any],
    ) -> AdServerDeal:
        """Update a deal via FreeWheel's update_deal MCP tool."""
        args = {"deal_id": deal_id, **updates}

        # Convert microcurrency to dollars if present
        if "floor_price_micros" in args:
            args["floor_price"] = micros_to_dollars(args.pop("floor_price_micros"))
        if "fixed_price_micros" in args:
            args["fixed_price"] = micros_to_dollars(args.pop("fixed_price_micros"))

        raw = await self._sh_client.call_tool("update_deal", args)
        return normalize_deal(raw if isinstance(raw, dict) else {"deal_id": deal_id})

    async def book_deal(
        self,
        deal_id: str,
        advertiser_name: str,
        *,
        deal_type: str = "private_auction",
        floor_price_micros: int = 0,
        fixed_price_micros: int = 0,
        currency: str = "USD",
        impressions_goal: int = -1,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        targeting: Optional[dict[str, Any]] = None,
        creative_sizes: Optional[list[tuple[int, int]]] = None,
    ) -> BookingResult:
        """Book a deal via FreeWheel's book_deal MCP tool.

        This is the primary deal creation path for FreeWheel.
        FreeWheel team's book_deal() handles both SH and BC in one call.
        Returns BookingResult with deal but no order/line items
        (SH programmatic doesn't use those).
        """
        args: dict[str, Any] = {
            "deal_id": deal_id,
            "advertiser_name": advertiser_name,
            "deal_type": deal_type,
        }
        if floor_price_micros:
            args["floor_price"] = micros_to_dollars(floor_price_micros)
        if fixed_price_micros:
            args["fixed_price"] = micros_to_dollars(fixed_price_micros)
        if currency != "USD":
            args["currency"] = currency
        if impressions_goal > 0:
            args["impressions_goal"] = impressions_goal
        if start_time:
            args["start_time"] = start_time.isoformat()
        if end_time:
            args["end_time"] = end_time.isoformat()
        if targeting:
            args["targeting"] = targeting

        raw = await self._sh_client.call_tool("book_deal", args)
        return normalize_booking_result(raw if isinstance(raw, dict) else {})
