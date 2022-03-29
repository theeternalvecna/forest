# Copyright (c) 2022 MobileCoin Inc.
# Copyright (c) 2022 Ilia Daniher <i@mobilecoin.com>
# MIT LICENSE
import asyncio
import json
import os
import time
from typing import Any, Generic, List, Optional, TypeVar, overload
import aiohttp
from forest.cryptography import hash_salt, get_ciphertext_value, get_cleartext_value

NAMESPACE = os.getenv("FLY_APP_NAME") or open("/etc/hostname").read().strip()
pAUTH = os.getenv("PAUTH", "")
pURL = os.getenv("PURL", "https://gusc1-charming-parrot-31440.upstash.io")

if not pAUTH:
    raise ValueError("Need to set PAUTH envvar for persistence")


class persistentKVStoreClient:
    async def post(self, key: str, data: str) -> str:
        raise NotImplementedError

    async def get(self, key: str) -> str:
        raise NotImplementedError


class fasterpKVStoreClient(persistentKVStoreClient):
    """Strongly consistent, persistent storage.
    Redis with [strong consistency via Upstash](https://docs.upstash.com/redis/features/consistency)
    On top of Redis and Webdis.
    Check out <https://github.com/mobilecoinofficial/forest/blob/main/pdictng_docs/upstash_pauth.png> for setup / pAUTH
    """

    def __init__(
        self,
        base_url: str = pURL,
        auth_str: str = pAUTH,
        namespace: str = NAMESPACE,
    ):
        self.url = base_url
        self.conn = aiohttp.ClientSession()
        self.auth = auth_str
        self.namespace = hash_salt(namespace)
        self.exists: dict[str, bool] = {}
        self.headers = {
            "Authorization": f"Bearer {self.auth}",
        }

    async def post(self, key: str, data: str) -> str:
        key = hash_salt(f"{self.namespace}_{key}")
        data = get_ciphertext_value(data)
        # try to set
        async with self.conn.post(
            f"{self.url}/SET/{key}", headers=self.headers, data=data
        ) as resp:
            return await resp.json()

    async def get(self, key: str) -> str:
        """Get and return value of an object with the specified key and namespace"""
        key = hash_salt(f"{self.namespace}_{key}")
        async with self.conn.get(f"{self.url}/GET/{key}", headers=self.headers) as resp:
            res = await resp.json()
            if "result" in res:
                return get_cleartext_value(res["result"])

        return ""


class fastpKVStoreClient(persistentKVStoreClient):
    """Strongly consistent, persistent storage.
    Stores a sneak table mapping keys to their existence to update faster.
    On top of Postgresql and Postgrest.
    Schema:
                                         Table "public.keyvalue"
       Column   |       Type       | Collation | Nullable |             Default
    ------------+------------------+-----------+----------+----------------------------------
     key        | bigint           |           | not null | generated by default as identity
     value      | text             |           |          | 'EMPTY'::text
     update     | text             |           |          |
     namespace  | text             |           |          |
     key_       | text             |           | not null |
     created_at | double precision |           |          |
     ttl        | bigint           |           |          |
     updated_at | double precision |           |          |
    Indexes:
        "keyvalue_pkey" PRIMARY KEY, btree (key)
        "keyvalue_key__key" UNIQUE CONSTRAINT, btree (key_)

    """

    def __init__(
        self,
        base_url: str = pURL,
        auth_str: str = pAUTH,
        namespace: str = NAMESPACE,
    ):
        self.url = base_url
        self.conn = aiohttp.ClientSession()
        self.auth = auth_str
        self.namespace = hash_salt(namespace)
        self.exists: dict[str, bool] = {}
        self.headers = {
            "Content-Type": "application/json",
            "apikey": f"{self.auth}",
            "Authorization": f"Bearer {self.auth}",
            "Prefer": "return=representation",
        }

    async def post(self, key: str, data: str) -> str:
        key = hash_salt(key)
        data = get_ciphertext_value(data)
        # try to set
        if self.exists.get(key):
            async with self.conn.patch(
                f"{self.url}?key_=eq.{key}&namespace=eq.{self.namespace}",
                headers=self.headers,
                data=json.dumps(
                    dict(
                        value=data,
                        updated_at=time.time(),
                        namespace=self.namespace,
                    )
                ),
            ) as resp:
                return await resp.json()
        async with self.conn.post(
            f"{self.url}",
            headers=self.headers,
            data=json.dumps(
                dict(
                    key_=key,
                    value=data,
                    created_at=time.time(),
                    namespace=self.namespace,
                )
            ),
        ) as resp:
            resp_text = await resp.text()
            # if set fails
            if "duplicate key value violates unique constraint" in resp_text:
                self.exists[key] = True
                # do update (patch not post)
                async with self.conn.patch(
                    f"{self.url}?key_=eq.{key}&namespace=eq.{self.namespace}",
                    headers=self.headers,
                    data=json.dumps(
                        dict(
                            value=data,
                            updated_at=time.time(),
                            namespace=self.namespace,
                        )
                    ),
                ) as resp:
                    return await resp.json()
            return json.loads(resp_text)

    async def get(self, key: str) -> str:
        """Get and return value of an object with the specified key and namespace"""
        key = hash_salt(key)
        async with self.conn.get(
            f"{self.url}?select=value&key_=eq.{key}&namespace=eq.{self.namespace}",
            headers={
                "Accept": "application/octet-stream",
                "apikey": f"{self.auth}",
                "Authorization": f"Bearer {self.auth}",
            },
        ) as resp:
            maybe_res = await resp.text()
            if maybe_res:
                self.exists[key] = True
                return get_cleartext_value(maybe_res)
            return ""


V = TypeVar("V")
# V = TypeVar("V", str, int, list, dict[str, str])
# that would be nice but causes an error with aPersistDictOfLists
# Value of type variable "V" of "aPersistDict" cannot be "list"
# possibly related: https://stackoverflow.com/questions/59933946/difference-between-typevart-a-b-and-typevart-bound-uniona-b
# https://stackoverflow.com/questions/55375362/why-does-mypy-ignore-a-generic-typed-variable-that-contains-a-type-incompatible


class aPersistDict(Generic[V]):
    """Async, consistent, persistent storage.
    Does not inherit from dict, but behaves mostly in the same way.
    Care is taken to offer asynchronous methods and strong consistency.
    This can be used for
        - inventory
        - subscribers
        - config info
    in a way that are persisted across reboots.
    No schemas and privacy preserving, but could be faster.
    Each write takes about 70 ms.

    This takes a type parameter for the value
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """If an argument is provided or a 'tag' keyword argument is passed...
        this will be used as a tag for backup / restore.
        """
        self.tag = ""
        if args:
            self.tag = args[0]
        if "tag" in kwargs:
            self.tag = kwargs.pop("tag")
        self.dict_: dict[str, Any] = {}
        self.client: persistentKVStoreClient = (
            fastpKVStoreClient() if "supabase" in pURL else fasterpKVStoreClient()
        )
        self.rwlock = asyncio.Lock()
        self.loop = asyncio.get_event_loop()
        self.init_task = asyncio.create_task(self.finish_init(**kwargs))
        self.write_task: Optional[asyncio.Task] = None

    def __repr__(self) -> str:
        return f"a{self.dict_}"

    def __str__(self) -> str:
        return f"a{self.dict_}"

    async def __getitem__(self, key: str) -> V:
        if value := await self.get(key):
            return value
        raise KeyError(key)

    def __setitem__(self, key: str, value: V) -> None:
        if self.write_task and not self.write_task.done():
            raise ValueError("Can't set value. write_task incomplete.")
        self.write_task = asyncio.create_task(self.set(key, value))

    async def finish_init(self, **kwargs: Any) -> None:
        """Does the asynchrnous part of the initialisation process."""
        async with self.rwlock:
            key = f"Persist_{self.tag}_{NAMESPACE}"
            result = await self.client.get(key)
            if result:
                self.dict_ = json.loads(result)
            self.dict_.update(**kwargs)

    @overload
    async def get(self, key: str, default: V) -> V:
        ...

    @overload
    async def get(self, key: str, default: None = None) -> Optional[V]:
        ...

    async def get(self, key: str, default: Optional[V] = None) -> Optional[V]:
        """Analogous to dict().get() - but async. Waits until writes have completed on the backend before returning results."""
        # always wait for pending writes - where a task has been created but lock not held
        if self.write_task:
            await self.write_task
            self.write_task = None
        # then grab the lock
        async with self.rwlock:
            return self.dict_.get(key) or default

    async def keys(self) -> List[str]:
        async with self.rwlock:
            return list(self.dict_.keys())

    async def remove(self, key: str) -> None:
        """Removes a value from the map, if it exists."""
        await self.set(key, None)
        return None

    @overload
    async def pop(self, key: str, default: V) -> V:
        ...

    @overload
    async def pop(self, key: str, default: None = None) -> Optional[V]:
        ...

    async def pop(self, key: str, default: Optional[V] = None) -> Optional[V]:
        """Returns and removes a value if it exists"""
        res = await self.get(key, default)
        await self.set(key, None)
        return res

    async def _set(self, key: str, value: Optional[V]) -> str:
        """Sets a value at a given key, returns metadata.
        This function exists so *OTHER FUNCTIONS* holding the lock can set values."""
        if key is not None and value is not None:
            self.dict_.update({key: value})
        elif key and value is None and key in self.dict_:
            self.dict_.pop(key)
        client_key = f"Persist_{self.tag}_{NAMESPACE}"
        client_value = json.dumps(self.dict_)
        return await self.client.post(client_key, client_value)

    async def set(self, key: str, value: Optional[V]) -> str:
        """Sets a value at a given key, returns metadata."""
        async with self.rwlock:
            return await self._set(key, value)


class aPersistDictOfInts(aPersistDict[int]):
    async def increment(self, key: str, value: int) -> str:
        """Since one cannot simply add to a coroutine, this function exists.
        If the key exists and the value is None, or an empty array, the provided value is added to a(the) list at that value."""
        value_to_extend: Any = 0
        async with self.rwlock:
            value_to_extend = self.dict_.get(key, 0)
            if isinstance(value_to_extend, int):
                return await self._set(key, value_to_extend + value)
            raise TypeError(f"key {key} is not an int")

    async def decrement(self, key: str, value: int) -> str:
        """Since one cannot simply add to a coroutine, this function exists.
        If the key exists and the value is None, or an empty array, the provided value is added to a(the) list at that value."""
        value_to_extend: Any = 0
        async with self.rwlock:
            value_to_extend = self.dict_.get(key, 0)
            if isinstance(value_to_extend, int):
                return await self._set(key, value_to_extend - value)
            raise TypeError(f"key {key} is not an int")


I = TypeVar("I")  # inner value


class aPersistDictOfLists(aPersistDict[list[I]]):
    "This takes a type parameter for the values in the *inner* list"

    async def extend(self, key: str, value: I) -> str:
        """Since one cannot simply add to a coroutine, this function exists.
        If the key exists and the value is None, or an empty array, the provided value is added to a(the) list at that value."""
        value_to_extend: Optional[list[I]] = []
        async with self.rwlock:
            value_to_extend = self.dict_.get(key, [])
            if isinstance(value_to_extend, list):
                value_to_extend.append(value)
                return await self._set(key, value_to_extend)
            raise TypeError(f"value {value_to_extend} for key {key} is not a list")

    async def remove_from(self, key: str, not_value: I) -> str:
        """Removes a value specified from the list, if present.
        Returns metadata"""
        async with self.rwlock:
            values_to_filter = self.dict_.get(key, [])
            if isinstance(values_to_filter, list):
                values_without_specified = [
                    el for el in values_to_filter if not_value != el
                ]
                return await self._set(key, values_without_specified)
            raise TypeError(f"key {key} is not a list")
