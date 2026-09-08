"""Bounded attachment decoding and Perplexity subscription uploads."""
import asyncio
import base64
import binascii
import inspect
import ipaddress
import json
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from curl_cffi import CurlMime, CurlOpt
from curl_cffi import requests

MAX_ATTACHMENT_BYTES=20 * 1024 * 1024
UPLOAD_ENDPOINT="https://www.perplexity.ai/rest/uploads/create_upload_url"
IMAGE_TYPES={"image/png", "image/jpeg", "image/gif", "image/webp"}
FILE_TYPES=IMAGE_TYPES | {
    "application/pdf", "text/plain", "text/markdown", "text/csv", "application/json",
    "application/rtf", "text/rtf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class AttachmentError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code=status_code


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


def _filename(value, content_type):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise AttachmentError("Attachment filename must be a non-empty string")
    name=PurePosixPath((value or "").replace("\\", "/")).name
    if not name:
        extension={"image/jpeg": ".jpg", "text/plain": ".txt", "text/markdown": ".md"}.get(content_type)
        name="attachment"+(extension or mimetypes.guess_extension(content_type) or ".bin")
    if len(name) > 255 or any(ord(char) < 32 for char in name):
        raise AttachmentError("Invalid attachment filename")
    return name


def _validate(attachment, image=False):
    if not isinstance(attachment, Attachment):
        raise AttachmentError("Stored file loader returned an invalid attachment", 500)
    if not isinstance(attachment.data, bytes) or not attachment.data:
        raise AttachmentError("Attachment is empty")
    if len(attachment.data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("Attachment exceeds the 20 MiB limit", 413)
    allowed=IMAGE_TYPES if image else FILE_TYPES
    if attachment.content_type not in allowed:
        raise AttachmentError("Unsupported attachment media type")
    if attachment.content_type in IMAGE_TYPES:
        signatures={
            "image/png": attachment.data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": attachment.data.startswith(b"\xff\xd8\xff"),
            "image/gif": attachment.data.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": attachment.data.startswith(b"RIFF") and attachment.data[8:12] == b"WEBP",
        }
        if not signatures[attachment.content_type]:
            raise AttachmentError("Image content does not match its declared media type")
    return Attachment(_filename(attachment.filename, attachment.content_type), attachment.content_type, attachment.data)


def decode_attachment(value, filename=None, image=False):
    if not isinstance(value, str) or not value:
        raise AttachmentError("Attachment data must be a non-empty base64 string")
    media_type=None
    encoded=value
    if value.startswith("data:"):
        header, separator, encoded=value.partition(",")
        if not separator or not header.endswith(";base64"):
            raise AttachmentError("Attachment data URLs must use base64 encoding")
        media_type=header[5:-7].lower()
    elif image:
        raise AttachmentError("Inline images must use a base64 data URL")
    if len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES+2)//3):
        raise AttachmentError("Attachment exceeds the 20 MiB limit", 413)
    try:
        data=base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("Attachment contains invalid base64 data") from exc
    media_type=media_type or mimetypes.guess_type(filename or "")[0]
    if not media_type:
        raise AttachmentError("Provide a filename with an extension or a data URL media type")
    return _validate(Attachment(_filename(filename, media_type), media_type, data), image)


async def _public_target(url):
    try:
        parsed=urlsplit(url)
        port=parsed.port or 443
        hostname=parsed.hostname
    except ValueError as exc:
        raise AttachmentError("Invalid attachment URL") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password or port != 443:
        raise AttachmentError("Attachment URLs must use public HTTPS on port 443 without credentials")
    try:
        addresses=await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AttachmentError("Attachment URL hostname could not be resolved") from exc
    ips={item[4][0] for item in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AttachmentError("Attachment URLs cannot access private or reserved networks")
    address=sorted(ips, key=lambda ip: ":" in ip)[0]
    return parsed, address


async def download_attachment(url, filename=None, image=False):
    """Pin checked DNS results and revalidate each redirect without forwarding cookies."""
    if not isinstance(url, str):
        raise AttachmentError("Attachment URL must be a string")
    for _ in range(4):
        parsed, address=await _public_target(url)
        pin=f"{parsed.hostname}:443:{'['+address+']' if ':' in address else address}".encode()
        body=bytearray()
        oversized=False

        def receive(data):
            nonlocal oversized
            if len(body)+len(data) > MAX_ATTACHMENT_BYTES:
                oversized=True
                return 0
            body.extend(data)
            return len(data)

        try:
            async with requests.AsyncSession(curl_options={CurlOpt.RESOLVE: [pin]}, trust_env=False) as remote:
                response=await remote.get(url, allow_redirects=False, timeout=30,
                    content_callback=receive, proxy="")
        except Exception as exc:
            if oversized:
                raise AttachmentError("Attachment exceeds the 20 MiB limit", 413) from exc
            raise AttachmentError("Attachment download failed", 502) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location=response.headers.get("location")
            if not location:
                raise AttachmentError("Attachment redirect has no destination", 502)
            url=urljoin(url, location)
            continue
        if response.status_code != 200:
            raise AttachmentError(f"Attachment download returned HTTP {response.status_code}", 502)
        media_type=response.headers.get("content-type", "").split(";", 1)[0].lower()
        name=filename or unquote(PurePosixPath(parsed.path).name) or None
        if media_type in {"", "application/octet-stream"}:
            media_type=mimetypes.guess_type(name or "")[0]
        return _validate(Attachment(_filename(name, media_type), media_type, bytes(body)), image)
    raise AttachmentError("Attachment URL exceeded the redirect limit")


async def resolve_attachment(part, file_loader=None):
    """Read Chat image_url/file or Responses input_image/input_file content parts."""
    if not isinstance(part, dict):
        raise AttachmentError("Attachment content parts must be objects")
    kind=part.get("type")
    image=kind in {"image_url", "input_image"}
    if kind not in {"image_url", "input_image", "file", "input_file"}:
        raise AttachmentError("Unsupported attachment content type")
    data=part.get("file") if kind == "file" else part
    if not isinstance(data, dict):
        raise AttachmentError("file must be an object")
    filename=data.get("filename")
    file_id=data.get("file_id")
    if file_id:
        if not isinstance(file_id, str) or file_loader is None:
            raise AttachmentError("Unknown or unsupported file_id")
        attachment=file_loader(file_id)
        if inspect.isawaitable(attachment):
            attachment=await attachment
        return _validate(attachment, image)
    value=data.get("image_url") if image else data.get("file_data")
    if isinstance(value, dict) and image:
        value=value.get("url")
    if isinstance(value, str) and value.startswith("data:"):
        return decode_attachment(value, filename, image)
    if not image and value is not None:
        return decode_attachment(value, filename)
    url=value if image else data.get("file_url")
    if url is not None:
        return await download_attachment(url, filename, image)
    raise AttachmentError("Attachment requires file data, a public HTTPS URL, or file_id")


async def _wait_for_processing(session, file_uuid, object_url):
    if not isinstance(file_uuid, str) or not file_uuid:
        raise AttachmentError("Document upload metadata omitted file_uuid", 502)
    response=None
    try:
        async with asyncio.timeout(60):
            response=await session.post(
                "https://www.perplexity.ai/rest/sse/attachment_processing/subscribe",
                json={"file_uuids": [file_uuid]}, headers={"accept": "text/event-stream",
                    "origin": "https://www.perplexity.ai", "referer": "https://www.perplexity.ai/",
                    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}, stream=True, timeout=60)
            if response.status_code != 200:
                raise AttachmentError(f"Attachment processing returned HTTP {response.status_code}", 502)
            data=[]
            size=0
            async for line in response.aiter_lines():
                if isinstance(line, bytes):
                    line=line.decode("utf-8")
                line=line.rstrip("\r\n")
                size+=len(line)
                if size > 1024 * 1024:
                    raise AttachmentError("Attachment processing response is too large", 502)
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not line and data:
                    result=json.loads("\n".join(data))
                    data=[]
                    if not isinstance(result, dict):
                        raise AttachmentError("Invalid attachment processing result", 502)
                    if result.get("file_uuid") != file_uuid:
                        continue
                    if result.get("success") is not True or result.get("token_limit_exceeded"):
                        raise AttachmentError("Perplexity could not fully parse the attachment", 502)
                    url=result.get("s3_url") or object_url
                    parsed=urlsplit(url)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                        raise AttachmentError("Invalid processed attachment URL", 502)
                    return url
            raise AttachmentError("Attachment processing ended without a result", 502)
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError("Attachment processing failed", 502) from exc
    finally:
        if response is not None:
            await response.aclose()


async def upload_attachment(session, attachment, version="2.18"):
    """Use the authenticated Perplexity session to obtain storage upload credentials."""
    attachment=_validate(attachment)
    try:
        response=await session.post(UPLOAD_ENDPOINT, params={"version": version, "source": "default"},
            json={"content_type": attachment.content_type, "file_size": len(attachment.data),
                  "filename": attachment.filename, "force_image": False, "source": "default"}, timeout=30)
    except Exception as exc:
        raise AttachmentError("Perplexity attachment authorization failed", 502) from exc
    if response.status_code != 200:
        raise AttachmentError(f"Perplexity attachment authorization returned HTTP {response.status_code}", 502)
    try:
        info=response.json()
    except ValueError as exc:
        raise AttachmentError("Perplexity returned invalid upload metadata", 502) from exc
    if not isinstance(info, dict) or info.get("error") or info.get("rate_limited"):
        raise AttachmentError("Perplexity rejected the upload or its upload quota is exhausted", 502)
    bucket=info.get("s3_bucket_url")
    object_url=info.get("s3_object_url")
    fields=info.get("fields")
    if not isinstance(bucket, str) or not isinstance(object_url, str) or not isinstance(fields, dict):
        raise AttachmentError("Perplexity upload metadata is incomplete", 502)
    parsed=urlsplit(bucket)
    host=parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or not (
        host.endswith(".amazonaws.com") or host == "api.cloudinary.com"
    ):
        raise AttachmentError("Perplexity returned an unrecognized upload destination", 502)
    mime=CurlMime()
    try:
        for name, value in fields.items():
            if not isinstance(name, str) or not isinstance(value, (str, int)):
                raise AttachmentError("Perplexity upload form is invalid", 502)
            mime.addpart(name=name, data=str(value))
        mime.addpart(name="file", filename=attachment.filename, content_type=attachment.content_type, data=attachment.data)
        # A fresh client prevents subscription cookies from being sent to third-party storage.
        async with requests.AsyncSession() as storage:
            uploaded=await storage.post(bucket, multipart=mime, timeout=60, allow_redirects=False)
        if uploaded.status_code not in {200, 201, 204}:
            raise AttachmentError(f"Attachment storage returned HTTP {uploaded.status_code}", 502)
        if "image/upload" in object_url:
            result=uploaded.json()
            if not isinstance(result, dict) or any(
                isinstance(entry, dict) and entry.get("status") == "rejected"
                for entry in result.get("moderation", [])
            ):
                raise AttachmentError("Image storage rejected the attachment", 502)
            secure_url=result.get("secure_url")
            if not isinstance(secure_url, str) or not secure_url.startswith("https://"):
                raise AttachmentError("Image storage response omitted the uploaded image URL", 502)
            object_url=re.sub(r"/private/s--.*?--/v\d+/user_uploads/", "/private/user_uploads/", secure_url)
        if attachment.content_type not in IMAGE_TYPES:
            object_url=await _wait_for_processing(session, info.get("file_uuid"), object_url)
        return object_url
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError("Attachment storage upload failed", 502) from exc
    finally:
        mime.close()
