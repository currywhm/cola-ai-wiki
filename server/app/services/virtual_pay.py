import hashlib
import hmac
import json
from typing import Any

import httpx

from ..config import settings


def _hmac_sha256(key: str, message: str) -> str:
    return hmac.new(key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()


def calc_pay_sig(uri: str, post_body: str, app_key: str | None = None) -> str:
    """Virtual payment signature. post_body must be the exact wire JSON string."""
    key = app_key or settings.wechat_virtual_app_key
    return _hmac_sha256(key, f'{uri}&{post_body}')


def calc_user_signature(post_body: str, session_key: str) -> str:
    return _hmac_sha256(session_key, post_body)


def virtual_product(plan: str) -> tuple[str, int]:
    plan_values = {
        'plus_monthly': (settings.wechat_virtual_plus_monthly_product_id, settings.wechat_virtual_plus_monthly_goods_price),
        'plus_quarterly': (settings.wechat_virtual_plus_quarterly_product_id, settings.wechat_virtual_plus_quarterly_goods_price),
        'plus_yearly': (settings.wechat_virtual_plus_yearly_product_id, settings.wechat_virtual_plus_yearly_goods_price),
        'pro_monthly': (settings.wechat_virtual_monthly_product_id, settings.wechat_virtual_monthly_goods_price),
        'pro_quarterly': (settings.wechat_virtual_quarterly_product_id, settings.wechat_virtual_quarterly_goods_price),
        'pro_yearly': (settings.wechat_virtual_yearly_product_id, settings.wechat_virtual_yearly_goods_price),
    }
    product_id, goods_price = plan_values.get(plan, ('', 0))
    if product_id and goods_price > 0:
        return product_id, goods_price
    if plan == 'pro_monthly' and settings.wechat_virtual_product_id and settings.wechat_virtual_goods_price > 0:
        return settings.wechat_virtual_product_id, settings.wechat_virtual_goods_price
    return '', 0


def virtual_configured(plan: str | None = None) -> bool:
    product_id, goods_price = virtual_product(plan or 'pro_monthly')
    return bool(settings.wechat_virtual_offer_id and settings.wechat_virtual_app_key and product_id and goods_price > 0)


def sign_data(*, offer_id: str, quantity: int, env: int, product_id: str, goods_price: int, out_trade_no: str, attach: str) -> str:
    # Keep insertion order and compact separators: this exact string is signed and sent to WeChat.
    return json.dumps({
        'offerId': offer_id, 'buyQuantity': quantity, 'env': env, 'currencyType': 'CNY',
        'productId': product_id, 'goodsPrice': goods_price, 'outTradeNo': out_trade_no, 'attach': attach,
    }, ensure_ascii=False, separators=(',', ':'))


async def query_order(openid: str, out_trade_no: str) -> dict[str, Any]:
    body = json.dumps({'openid': openid, 'env': settings.wechat_virtual_env, 'order_id': out_trade_no}, ensure_ascii=False, separators=(',', ':'))
    uri = '/xpay/query_order'
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f'{settings.wechat_virtual_base_url.rstrip("/")}{uri}', content=body.encode('utf-8'), headers={'Content-Type': 'application/json', 'pay_sig': calc_pay_sig(uri, body)})
    if response.status_code >= 400:
        raise RuntimeError(f'微信虚拟支付查单失败（{response.status_code}）')
    return response.json()
