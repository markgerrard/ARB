from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from arb_files.config import Settings
from arb_files.names import to_key


def _err_code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _iso(value):
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _put_if_match_etag(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("files backend returned invalid ETag")
    serialized = value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else value
    if not serialized:
        raise RuntimeError("files backend returned invalid ETag")
    return serialized


class FilesStore:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable | None = None,
        audit_sink=None,
        now: Callable[[], datetime] | None = None,
    ):
        self.s = settings
        self._client_factory = client_factory or self._default_client_factory
        self._client = None
        self.audit_sink = audit_sink
        self._now = now or _default_now

    def _default_client_factory(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.s.endpoint,
            region_name=self.s.region,
            aws_access_key_id=self.s.access_key,
            aws_secret_access_key=self.s.secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    @property
    def client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _name_from_key(self, key: str) -> str:
        return key[len(self.s.prefix) :] if key.startswith(self.s.prefix) else key

    def _emit_audit(self, event: dict) -> None:
        if self.audit_sink is not None:
            self.audit_sink(event)

    def _validate_list_prefix(self, prefix: str) -> None:
        if prefix.startswith("/"):
            raise ValueError("prefix must not start with '/'")
        for segment in prefix.split("/"):
            if segment in {".", ".."}:
                raise ValueError("invalid prefix segment")

    def list(self, prefix: str = "") -> dict:
        self._validate_list_prefix(prefix)
        full_prefix = f"{self.s.prefix}{prefix}"
        response = self.client.list_objects_v2(
            Bucket=self.s.bucket,
            Prefix=full_prefix,
            MaxKeys=self.s.list_max,
        )
        trash_prefix = f"{self.s.prefix}.trash/"
        items = [
            {
                "name": self._name_from_key(item["Key"]),
                "size": item.get("Size"),
                "modified": _iso(item.get("LastModified")),
                "etag": item.get("ETag"),
            }
            for item in response.get("Contents", [])
            if not item["Key"].startswith(trash_prefix)
        ]
        return {
            "items": items,
            "is_truncated": bool(response.get("IsTruncated")),
            "next_token": response.get("NextContinuationToken"),
        }

    def head(self, name: str) -> dict:
        key = to_key(self.s.prefix, name)
        try:
            response = self.client.head_object(Bucket=self.s.bucket, Key=key)
        except Exception as exc:
            if _err_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return {"exists": False, "name": name}
            raise RuntimeError("files backend unavailable; retry") from exc
        meta = response.get("Metadata", {})
        return {
            "exists": True,
            "name": name,
            "size": response.get("ContentLength"),
            "content_type": response.get("ContentType"),
            "etag": response.get("ETag"),
            "modified": _iso(response.get("LastModified")),
            "uploaded_by": meta.get("uploaded-by"),
            "uploaded_at": meta.get("uploaded-at"),
        }

    def get_bytes(self, name: str) -> tuple[bytes, str]:
        key = to_key(self.s.prefix, name)
        try:
            response = self.client.get_object(Bucket=self.s.bucket, Key=key)
        except Exception as exc:
            if _err_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                raise KeyError(f"not found: {name}") from exc
            raise RuntimeError("files backend unavailable; retry") from exc
        return response["Body"].read(), response.get("ContentType", "application/octet-stream")

    def _trash_key(self, name: str, etag: str | None) -> str:
        date = self._now().date().isoformat()
        version = (etag or "unknown").strip('"') or "unknown"
        return f"{self.s.prefix}.trash/{date}/{version}/{name}"

    def put_bytes(
        self,
        name: str,
        data: bytes,
        content_type: str,
        *,
        uploaded_by: str,
        force: bool = False,
    ) -> dict:
        key = to_key(self.s.prefix, name)
        if_match = None
        if force:
            prior = self.head(name)
            if prior["exists"]:
                if_match = _put_if_match_etag(prior.get("etag"))
                trash_key = self._trash_key(name, prior.get("etag"))
                self.client.copy_object(
                    Bucket=self.s.bucket,
                    Key=trash_key,
                    CopySource={"Bucket": self.s.bucket, "Key": key},
                )
                self._emit_audit(
                    {
                        "op": "overwrite",
                        "name": name,
                        "actor": uploaded_by,
                        "etag": prior.get("etag"),
                        "ts": self._now().isoformat(),
                        "recovery_key": trash_key,
                    }
                )
                # Atomic race guard: only overwrite if the object is still the version we trashed.
                # If a concurrent writer changed it after our head(), the conditional PUT 412s and we
                # surface a retryable error instead of silently clobbering an unseen, un-trashed version.
        kwargs = {
            "Bucket": self.s.bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "Metadata": {"uploaded-by": uploaded_by, "uploaded-at": self._now().isoformat()},
        }
        if not force:
            kwargs["IfNoneMatch"] = "*"
        elif if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            response = self.client.put_object(**kwargs)
        except Exception as exc:
            if _err_code(exc) in {"PreconditionFailed", "412"}:
                if force:
                    raise RuntimeError(
                        "stale write; another writer changed the object after read — retry"
                    ) from exc
                raise ValueError("exists; pass force=true to overwrite") from exc
            raise RuntimeError("files backend unavailable; retry") from exc
        return {"name": name, "size": len(data), "etag": response.get("ETag")}

    def presign_get(self, name: str) -> dict:
        head = self.head(name)
        if not head["exists"]:
            raise KeyError(f"not found: {name}")
        key = to_key(self.s.prefix, name)
        url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.s.bucket, "Key": key},
            ExpiresIn=self.s.presign_ttl,
        )
        return {
            "url": url,
            "method": "GET",
            "expires_in": self.s.presign_ttl,
            "size": head.get("size"),
            "content_type": head.get("content_type"),
        }

    def presign_put(
        self,
        name: str,
        content_type: str | None = None,
        *,
        uploaded_by: str,
        force: bool = False,
    ) -> dict:
        key = to_key(self.s.prefix, name)
        params = {"Bucket": self.s.bucket, "Key": key}
        headers = {}
        metadata = {"uploaded-by": uploaded_by, "uploaded-at": self._now().isoformat()}
        params["Metadata"] = metadata
        for key_name, value in metadata.items():
            headers[f"x-amz-meta-{key_name}"] = value
        if content_type:
            params["ContentType"] = content_type
            headers["Content-Type"] = content_type
        if not force:
            params["IfNoneMatch"] = "*"
            headers["If-None-Match"] = "*"
        else:
            # The PUT itself happens out-of-band at S3, so the store never observes it. The
            # recovery copy and the audit record must therefore be taken HERE, at issue time —
            # otherwise a presigned force PUT bypasses the delete-safety plane completely.
            prior = self.head(name)
            if prior["exists"]:
                if_match = _put_if_match_etag(prior.get("etag"))
                trash_key = self._trash_key(name, prior.get("etag"))
                self.client.copy_object(
                    Bucket=self.s.bucket,
                    Key=trash_key,
                    CopySource={"Bucket": self.s.bucket, "Key": key},
                )
                self._emit_audit(
                    {
                        "op": "overwrite",
                        "name": name,
                        "actor": uploaded_by,
                        "etag": prior.get("etag"),
                        "ts": self._now().isoformat(),
                        "recovery_key": trash_key,
                        # This records an AUTHORISED overwrite, not an observed one: the client
                        # may never PUT. Marked so an audit reader can tell the two apart.
                        "via": "presign",
                    }
                )
                # Same race guard as put_bytes: the out-of-band PUT only lands if the object
                # is still the version we trashed, so a concurrent writer's version can never
                # be clobbered without its own recovery copy.
                params["IfMatch"] = if_match
                headers["If-Match"] = if_match
        url = self.client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=self.s.presign_ttl,
            HttpMethod="PUT",
        )
        return {
            "url": url,
            "method": "PUT",
            "expires_in": self.s.presign_ttl,
            "headers": headers,
        }

    def delete(self, name: str, *, actor: str) -> dict:
        key = to_key(self.s.prefix, name)
        head = self.head(name)
        if not head["exists"]:
            raise KeyError(f"not found: {name}")
        trash_key = self._trash_key(name, head.get("etag"))
        self.client.copy_object(
            Bucket=self.s.bucket,
            Key=trash_key,
            CopySource={"Bucket": self.s.bucket, "Key": key},
        )
        self._emit_audit(
            {
                "op": "delete",
                "name": name,
                "actor": actor,
                "etag": head.get("etag"),
                "ts": self._now().isoformat(),
                "recovery_key": trash_key,
            }
        )
        self.client.delete_object(Bucket=self.s.bucket, Key=key)
        return {"deleted": True, "name": name, "recovery": {"trash_key": trash_key}}
