"""File storage abstraction for local disks and WeChat Cloud Run COS.

`storage_path` values stored in the database are opaque references:
- local development: absolute filesystem paths
- WeChat Cloud Run: ``cos://<bucket>/<key>``

Business code must use this module instead of opening storage paths directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import settings


class StorageError(RuntimeError):
    pass


_cos_lock = asyncio.Lock()
_cos_credentials: dict[str, str | int] = {}


def _clean_key(key: str) -> str:
    cleaned = key.replace("\\", "/").lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        raise StorageError("非法存储对象路径")
    return cleaned


def _full_cos_key(key: str) -> str:
    prefix = settings.cos_prefix.strip().strip("/")
    return f"{prefix}/{_clean_key(key)}" if prefix else _clean_key(key)


def _cos_ref(key: str) -> str:
    return f"cos://{settings.cos_bucket_name}/{_full_cos_key(key)}"


def reference(key: str) -> str:
    """Return the storage reference for a key without writing the object."""
    if settings.storage_backend == "local":
        return str(_local_destination(key))
    return _cos_ref(key)


def is_cos_ref(value: str) -> bool:
    return str(value or "").startswith("cos://")


def _parse_cos_ref(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    if parsed.scheme != "cos" or not parsed.netloc or not parsed.path:
        raise StorageError("COS 存储引用格式错误")
    return parsed.netloc, parsed.path.lstrip("/")


def _local_path(ref: str) -> Path:
    path = Path(str(ref or "")).expanduser()
    if not path.is_absolute():
        path = settings.resolve_path(str(path))
    return path


def _local_destination(key: str) -> Path:
    root = settings.upload_path.resolve()
    destination = (root / _clean_key(key)).resolve()
    if root != destination and root not in destination.parents:
        raise StorageError("目标文件超出上传目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


async def _get_cos_credentials() -> tuple[str, str, str]:
    """Return COS credentials, preferring Cloud Run's token-free open API."""
    static_id = settings.cos_secret_id.strip()
    static_key = settings.cos_secret_key.strip()
    if static_id and static_key:
        return static_id, static_key, ""
    now = int(time.time())
    async with _cos_lock:
        if _cos_credentials and int(_cos_credentials.get("expires_at", 0)) > now + 60:
            return (
                str(_cos_credentials["secret_id"]),
                str(_cos_credentials["secret_key"]),
                str(_cos_credentials.get("token", "")),
            )
        url = f"{settings.wechat_openapi_base.rstrip('/')}/_/cos/getauth"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StorageError(f"获取微信云托管 COS 临时密钥失败：{exc}") from exc
        secret_id = str(payload.get("TmpSecretId") or "")
        secret_key = str(payload.get("TmpSecretKey") or "")
        token = str(payload.get("Token") or "")
        expires_at = int(payload.get("ExpiredTime") or 0)
        if not secret_id or not secret_key or not token:
            raise StorageError("微信云托管 COS 临时密钥响应不完整")
        _cos_credentials.update({
            "secret_id": secret_id,
            "secret_key": secret_key,
            "token": token,
            "expires_at": expires_at,
        })
        return secret_id, secret_key, token


async def _cos_client():
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise StorageError("未安装 cos-python-sdk-v5") from exc
    secret_id, secret_key, token = await _get_cos_credentials()
    config = CosConfig(
        Region=settings.cos_region_name,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token or None,
        Scheme="https",
    )
    return CosS3Client(config)


def _content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


async def save_file(source: Path, key: str, *, content_type: str = "") -> str:
    """Persist a local file and return its database storage reference."""
    if settings.storage_backend == "local":
        destination = _local_destination(key)
        if source.resolve() != destination:
            try:
                await asyncio.to_thread(shutil.copyfile, source, destination)
            except OSError as exc:
                raise StorageError(f"保存本地文件失败：{exc}") from exc
        return str(destination)

    client = await _cos_client()
    cloud_key = _full_cos_key(key)
    try:
        await asyncio.to_thread(
            client.upload_file,
            Bucket=settings.cos_bucket_name,
            Key=cloud_key,
            LocalFilePath=str(source),
            EnableMD5=False,
            ContentType=content_type or _content_type(key),
        )
    except Exception as exc:
        raise StorageError(f"上传 COS 失败：{exc}") from exc
    return _cos_ref(key)


async def save_bytes(data: bytes, key: str, *, content_type: str = "") -> str:
    suffix = Path(key).suffix
    fd, raw_path = tempfile.mkstemp(prefix="zhi-storage-", suffix=suffix)
    path = Path(raw_path)
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
        return await save_file(path, key, content_type=content_type)
    finally:
        path.unlink(missing_ok=True)


async def read_bytes(ref: str) -> bytes:
    if not ref:
        raise StorageError("存储引用为空")
    if not is_cos_ref(ref):
        path = _local_path(ref)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise StorageError(f"读取本地文件失败：{exc}") from exc

    bucket, key = _parse_cos_ref(ref)
    client = await _cos_client()
    try:
        response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
        body = response.get("Body")
        if body is None:
            raise StorageError("COS 返回内容为空")
        return await asyncio.to_thread(body.get_raw_stream().read)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(f"读取 COS 文件失败：{exc}") from exc


async def materialize(ref: str, *, suffix: str = "") -> Path:
    """Copy an object to a temporary local file for parsers and FileResponse."""
    fd, raw_path = tempfile.mkstemp(prefix="zhi-object-", suffix=suffix)
    path = Path(raw_path)
    try:
        if not is_cos_ref(ref):
            source = _local_path(ref)
            await asyncio.to_thread(shutil.copyfile, source, path)
            return path

        bucket, key = _parse_cos_ref(ref)
        client = await _cos_client()
        await asyncio.to_thread(client.download_file, Bucket=bucket, Key=key, DestFilePath=str(path))
        return path
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, StorageError):
            raise
        raise StorageError(f"读取存储对象失败：{exc}") from exc


async def copy_ref(source_ref: str, key: str) -> str:
    temporary = await materialize(source_ref, suffix=Path(key).suffix)
    try:
        return await save_file(temporary, key)
    finally:
        temporary.unlink(missing_ok=True)


async def exists(ref: str) -> bool:
    if not ref:
        return False
    if not is_cos_ref(ref):
        return _local_path(ref).is_file()
    bucket, key = _parse_cos_ref(ref)
    client = await _cos_client()
    try:
        await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


async def delete(ref: str) -> None:
    if not ref:
        return
    if not is_cos_ref(ref):
        try:
            await asyncio.to_thread(_local_path(ref).unlink, missing_ok=True)
        except OSError:
            pass
        return
    bucket, key = _parse_cos_ref(ref)
    client = await _cos_client()
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except Exception:
        pass


# ---- 客户端直传 / 直下 -------------------------------------------------------
# 小程序走微信云托管私有协议（callContainer）时，请求体上限 100KiB、单次调用上限
# 15s，文件字节不可能经容器转发。所以文件走对象存储：
#   · 上传：服务端按 COS「POST Object」规范签发一次性表单凭证，小程序用
#     wx.uploadFile 直接传到桶里（该接口不使用 COS 统一签名，签名规则见官方文档 436/14690）；
#   · 下载：服务端签发短时效预签名 URL，小程序 wx.downloadFile 直接取。
# 两条路都只经过腾讯云的域名，不需要自建域名与备案。


def bucket_url() -> str:
    """直传表单的提交地址：POST Object 打桶根，对象路径由表单 key 决定。"""
    return f"https://{settings.cos_bucket_name}.cos.{settings.cos_region_name}.myqcloud.com/"


def full_key(key: str) -> str:
    """业务 key → 桶内完整 key（含 COS_PREFIX）。直传表单与上传回执核对都用它。"""
    return _full_cos_key(key)


def ref_from_full_key(key: str) -> str:
    """把客户端回执的完整 key 还原成存储引用（客户端不接触 cos:// 形式）。"""
    return f"cos://{settings.cos_bucket_name}/{_clean_key(key)}"


async def object_size(ref: str) -> int:
    """对象真实体积：上传回执只认服务端读到的值，不信客户端上报。"""
    if not is_cos_ref(ref):
        path = _local_path(ref)
        return path.stat().st_size if path.is_file() else 0
    bucket, key = _parse_cos_ref(ref)
    client = await _cos_client()
    try:
        head = await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
    except Exception as exc:
        raise StorageError(f"读取对象信息失败：{exc}") from exc
    return int(head.get("Content-Length") or 0)


async def presigned_get(ref: str, *, expires: int = 900) -> str:
    """短时效下载地址；本地存储没有这个概念，返回空串由调用方回退到服务端转发。"""
    if not is_cos_ref(ref):
        return ""
    bucket, key = _parse_cos_ref(ref)
    client = await _cos_client()
    try:
        return await asyncio.to_thread(
            client.get_presigned_url,
            Method="GET",
            Bucket=bucket,
            Key=key,
            Expired=max(60, int(expires)),
        )
    except Exception as exc:
        raise StorageError(f"生成下载地址失败：{exc}") from exc


async def post_form(key: str, *, max_bytes: int, expires: int = 900) -> dict:
    """签发直传表单凭证（COS POST Object）。

    policy 里锁死对象 key 与体积上限，客户端只能往这一个位置写、
    且写不了超过配额的文件；凭证本身带失效时间，泄露也无法长期复用。
    """
    secret_id, secret_key, token = await _get_cos_credentials()
    key_time = f"{int(time.time())};{int(time.time()) + max(60, int(expires))}"
    object_key = _full_cos_key(key)
    policy = {
        "expiration": datetime.fromtimestamp(int(key_time.split(";")[1]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": [
            {"q-sign-algorithm": "sha1"},
            {"q-ak": secret_id},
            {"q-sign-time": key_time},
            {"key": object_key},
            ["content-length-range", 1, max(1, int(max_bytes))],
        ],
    }
    policy_text = json.dumps(policy, separators=(",", ":"), ensure_ascii=False)
    sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()
    string_to_sign = hashlib.sha1(policy_text.encode("utf-8")).hexdigest()
    fields = {
        "key": object_key,
        "policy": base64.b64encode(policy_text.encode("utf-8")).decode("ascii"),
        "q-sign-algorithm": "sha1",
        "q-ak": secret_id,
        "q-key-time": key_time,
        "q-signature": hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest(),
    }
    if token:
        fields["x-cos-security-token"] = token
    return {"url": bucket_url(), "fields": fields, "key": object_key, "expires_at": int(key_time.split(";")[1])}

