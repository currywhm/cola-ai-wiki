import base64
import json
import secrets
import time
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography import x509
import httpx
from ..config import settings


def required() -> None:
    missing = [name for name, value in {
        "WECHAT_APPID": settings.wechat_appid,
        "WECHAT_MCHID": settings.wechat_mchid,
        "WECHAT_MCH_SERIAL_NO": settings.wechat_mch_serial_no,
        "WECHAT_PAY_API_V3_KEY": settings.wechat_pay_api_v3_key,
        "WECHAT_PAY_NOTIFY_URL": settings.wechat_pay_notify_url,
    }.items() if not value]
    if missing or not settings.resolve_path(settings.wechat_pay_private_key_path).exists():
        raise RuntimeError(f"微信支付配置不完整: {', '.join(missing or ['WECHAT_PAY_PRIVATE_KEY_PATH'])}")


def private_key():
    return serialization.load_pem_private_key(settings.resolve_path(settings.wechat_pay_private_key_path).read_bytes(), password=None)


def sign(message: str) -> str:
    signature = private_key().sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def authorization(method: str, path: str, body: str) -> str:
    nonce = secrets.token_urlsafe(16)
    timestamp = str(int(time.time()))
    signature = sign(f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n")
    return f'WECHATPAY2-SHA256-RSA2048 mchid="{settings.wechat_mchid}",nonce_str="{nonce}",signature="{signature}",timestamp="{timestamp}",serial_no="{settings.wechat_mch_serial_no}"'


async def create_jsapi_order(openid: str, out_trade_no: str, description: str, amount: int) -> str:
    required()
    body = json.dumps({"appid": settings.wechat_appid, "mchid": settings.wechat_mchid, "description": description, "out_trade_no": out_trade_no, "notify_url": settings.wechat_pay_notify_url, "amount": {"total": amount, "currency": "CNY"}, "payer": {"openid": openid}}, ensure_ascii=False, separators=(",", ":"))
    path = "/v3/pay/transactions/jsapi"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.wechat_pay_base_url.rstrip('/')}{path}", content=body.encode(), headers={"Authorization": authorization("POST", path, body), "Accept": "application/json", "Content-Type": "application/json"})
    if response.status_code >= 300:
        raise RuntimeError(f"微信支付统一下单失败: {response.text[:500]}")
    return response.json()["prepay_id"]


def payment_params(prepay_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    package = f"prepay_id={prepay_id}"
    pay_sign = sign(f"{settings.wechat_appid}\n{timestamp}\n{nonce}\n{package}\n")
    return {"timeStamp": timestamp, "nonceStr": nonce, "package": package, "signType": "RSA", "paySign": pay_sign}


def verify_notify(headers: dict[str, str], body: bytes) -> bool:
    cert_path = settings.resolve_path(settings.wechat_pay_platform_cert_path)
    if not cert_path.exists():
        return False
    def header(name: str) -> str:
        return headers.get(name) or headers.get(name.lower()) or ""
    timestamp = header("Wechatpay-Timestamp")
    nonce = header("Wechatpay-Nonce")
    signature = header("Wechatpay-Signature")
    if not timestamp or not nonce or not signature:
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        cert.public_key().verify(base64.b64decode(signature), f"{timestamp}\n{nonce}\n{body.decode()}\n".encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def decrypt_notify(resource: dict) -> dict:
    key = settings.wechat_pay_api_v3_key.encode()
    plaintext = AESGCM(key).decrypt(resource["nonce"].encode(), base64.b64decode(resource["ciphertext"]), resource.get("associated_data", "").encode())
    return json.loads(plaintext.decode())
