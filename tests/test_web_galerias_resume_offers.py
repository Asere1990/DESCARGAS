"""Offers redirect is HMAC-signed to CUSTOMER-SERVICE; no payment or media writes."""
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias.app import Gallery
from web_galerias.customer_bridge import post_signed

class CustomerOffersLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_offers_link_uses_existing_gallery_entitlement_and_signed_bridge(self):
        req=N(content_length=0,json=AsyncMock(return_value={"op":"offers-link"}))
        g=N(auth=AsyncMock(return_value=456),db=lambda:object(),
            entitled=AsyncMock(return_value=True))
        with patch("web_galerias.customer_bridge.ready",return_value=True),patch(
             "web_galerias.customer_bridge.post_signed",
             new=AsyncMock(return_value={"url":"https://t.me/CustomerRobot/miniapp?startapp=offers"})
        ) as signed:
            result=await Gallery.api(g,req)
        self.assertEqual(result,{"url":"https://t.me/CustomerRobot/miniapp?startapp=offers"})
        signed.assert_awaited_once_with("offers-link",{"telegram_id":456},timeout=5)
        g.entitled.assert_awaited_once_with(456)

    async def test_unentitled_user_never_requests_customer_link(self):
        req=N(content_length=0,json=AsyncMock(return_value={"op":"offers-link"}))
        g=N(auth=AsyncMock(return_value=456),db=lambda:object(),
            entitled=AsyncMock(return_value=False))
        with patch("web_galerias.customer_bridge.post_signed",
                   new_callable=AsyncMock) as signed:
            with self.assertRaises(web.HTTPForbidden):
                await Gallery.api(g,req)
            signed.assert_not_awaited()

    async def test_invalid_bridge_link_fail_closed(self):
        req=N(content_length=0,json=AsyncMock(return_value={"op":"offers-link"}))
        g=N(auth=AsyncMock(return_value=456),db=lambda:object(),
            entitled=AsyncMock(return_value=True))
        with patch("web_galerias.customer_bridge.ready",return_value=True),patch(
             "web_galerias.customer_bridge.post_signed",
             new=AsyncMock(return_value={"url":"https://evil.example/?startapp=offers"})
        ):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await Gallery.api(g,req)

if __name__=="__main__":
    unittest.main()
