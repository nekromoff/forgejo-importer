"""JSON file cache for data read from the source system, one file per entry"""
import dataclasses
import datetime
import json
import os
import time
from enum import Enum
from typing import Any

from fg_migration.core import canonical_types
from fg_migration.core.canonical_types import CanonicalRepo
from fg_migration.utils import fg_print

CACHE_FORMAT_VERSION = 1

# never written to the cache files, credentials are restored from the config on load
SECRET_FIELDS = frozenset({"auth_password", "auth_username", "auth_token", "password"})


class JsonCache:
    """Stores canonical objects (and plain JSON data) as one JSON file per key.
       Each entry is written immediately, so an interrupted run keeps what it fetched.
       An entry is only used if written for the same source (url and user), it is
       younger than max_age_hours, and refresh is not requested"""

    def __init__(self, cache_dir: str, max_age_hours: float, identity: str,
                 secrets: dict[str, Any], refresh: bool = False):
        self.cache_dir = cache_dir
        self.max_age_seconds = max_age_hours * 3600
        self.identity = identity
        # values for the SECRET_FIELDS of CanonicalRepo, as they are not cached
        self.secrets = secrets
        self.refresh = refresh

    def path(self, key: str) -> str:
        """file path of a cache entry"""
        return os.path.join(self.cache_dir, f"{key}.json")

    def get(self, key: str) -> tuple[Any, float] | None:
        """(data, age in seconds) of a valid entry, else None"""
        if self.refresh:
            return None
        path = self.path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except (OSError, ValueError) as e:
            fg_print.warning(f"Ignoring unreadable cache file {path}: {e}")
            return None

        if (content.get("version") != CACHE_FORMAT_VERSION
                or content.get("identity") != self.identity):
            fg_print.debug(f"Ignoring cache file {path}, written for another source")
            return None
        age = time.time() - content.get("created", 0)
        if age > self.max_age_seconds:
            fg_print.debug(f"Ignoring cache file {path}, it is too old")
            return None

        try:
            return self._decode(content["data"]), age
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            fg_print.warning(f"Ignoring cache file {path} that cannot be decoded: {e}")
            return None

    def value(self, key: str) -> Any:
        """data of a valid entry, else None"""
        hit = self.get(key)
        return None if hit is None else hit[0]

    def put(self, key: str, data: Any) -> None:
        """write an entry immediately"""
        path = self.path(key)
        content = {
            "version": CACHE_FORMAT_VERSION,
            "identity": self.identity,
            "created": time.time(),
            "created_readable": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            "data": self._encode(data),
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # write to a temporary file first, an interrupted run must not leave a broken cache
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(content, f, indent=1)
            os.replace(tmp_path, path)
        except OSError as e:
            fg_print.warning(f"Unable to write cache file {path}: {e}")

    def delete(self, key: str) -> None:
        """remove an entry, if present"""
        try:
            os.remove(self.path(key))
        except FileNotFoundError:
            pass
        except OSError as e:
            fg_print.warning(f"Unable to remove cache file {self.path(key)}: {e}")

    def _encode(self, value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            encoded = {"__type__": type(value).__qualname__}
            for field in dataclasses.fields(value):
                if field.name in SECRET_FIELDS:
                    encoded[field.name] = None
                else:
                    encoded[field.name] = self._encode(getattr(value, field.name))
            return encoded
        if isinstance(value, Enum):
            return {"__enum__": type(value).__qualname__, "value": value.value}
        if isinstance(value, (set, frozenset)):
            return {"__set__": [self._encode(item) for item in value]}
        if isinstance(value, (list, tuple)):
            return [self._encode(item) for item in value]
        if isinstance(value, dict):
            return {key: self._encode(item) for key, item in value.items()}
        return value

    def _decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "__enum__" in value:
            return self._resolve_type(value["__enum__"])(value["value"])
        if "__set__" in value:
            return {self._decode(item) for item in value["__set__"]}
        if "__type__" in value:
            cls = self._resolve_type(value["__type__"])
            fields = {key: self._decode(item) for key, item in value.items()
                      if key != "__type__"}
            if cls is CanonicalRepo:
                fields.update(self.secrets)
            return cls(**fields)
        return {key: self._decode(item) for key, item in value.items()}

    @staticmethod
    def _resolve_type(qualname: str) -> type:
        """resolve e.g. CanonicalRepo or HierarchyNode.RelationEnum"""
        resolved: Any = canonical_types
        for part in qualname.split("."):
            resolved = getattr(resolved, part)
        return resolved
