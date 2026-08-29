from __future__ import annotations

from datetime import datetime, timezone


def _conditional_etag(value):
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


class FakeClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeBody:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.presigned = []

    def put_object(
        self,
        Bucket,
        Key,
        Body,
        ContentType="application/octet-stream",
        Metadata=None,
        IfNoneMatch=None,
        IfMatch=None,
        **_kwargs,
    ):
        if IfNoneMatch == "*" and Key in self.objects:
            raise FakeClientError("PreconditionFailed")
        if IfMatch is not None and (
            Key not in self.objects
            or _conditional_etag(self.objects[Key]["ETag"]) != _conditional_etag(IfMatch)
        ):
            raise FakeClientError("PreconditionFailed")
        self.objects[Key] = {
            "Body": Body,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ETag": '"deadbeef"',
        }
        return {"ETag": '"deadbeef"'}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("404")
        obj = self.objects[Key]
        return {
            "ContentLength": len(obj["Body"]),
            "ContentType": obj["ContentType"],
            "ETag": obj["ETag"],
            "LastModified": obj["LastModified"],
            "Metadata": obj["Metadata"],
        }

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        obj = self.objects[Key]
        return {
            "Body": FakeBody(obj["Body"]),
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
        }

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        page = keys[:MaxKeys]
        contents = [
            {
                "Key": key,
                "Size": len(self.objects[key]["Body"]),
                "LastModified": self.objects[key]["LastModified"],
                "ETag": self.objects[key]["ETag"],
            }
            for key in page
        ]
        return {
            "Contents": contents,
            "IsTruncated": len(keys) > MaxKeys,
            "NextContinuationToken": "tok" if len(keys) > MaxKeys else None,
        }

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}

    def copy_object(self, Bucket, Key, CopySource, Metadata=None, MetadataDirective=None, **_kwargs):
        source_key = CopySource["Key"] if isinstance(CopySource, dict) else CopySource.split("/", 1)[1]
        if source_key not in self.objects:
            raise FakeClientError("NoSuchKey")
        obj = dict(self.objects[source_key])
        if MetadataDirective == "REPLACE" and Metadata is not None:
            obj["Metadata"] = Metadata
        self.objects[Key] = obj
        return {"ETag": obj["ETag"]}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=900, HttpMethod=None):
        self.presigned.append(
            {"op": op, "Params": Params or {}, "ExpiresIn": ExpiresIn, "HttpMethod": HttpMethod}
        )
        return f"https://signed.example/{Params['Key']}?op={op}&exp={ExpiresIn}"
