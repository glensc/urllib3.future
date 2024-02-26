from __future__ import annotations

from test import notMacOS
from time import time

import pytest

from urllib3 import AsyncHTTPSConnectionPool, ResponsePromise

from .. import TraefikTestCase


@pytest.mark.asyncio
class TestConnectionPoolMultiplexed(TraefikTestCase):
    async def test_multiplexing_fastest_to_slowest(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host, self.https_port, ca_certs=self.ca_authority
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen("GET", "/delay/3", multiplexed=True)
                promise_fast = await pool.urlopen("GET", "/delay/1", multiplexed=True)

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            before = time()

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert 1.5 >= round(time() - before, 2)

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in (await response.json())["url"]

            assert 3.5 >= round(time() - before, 2)
            assert await pool.get_response() is None

    async def test_multiplexing_without_preload(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host, self.https_port, ca_certs=self.ca_authority
        ) as pool:
            promises = []

            for i in range(5):
                promise_slow = await pool.urlopen(
                    "GET", "/delay/3", multiplexed=True, preload_content=False
                )
                promise_fast = await pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
                )

                assert isinstance(promise_fast, ResponsePromise)
                assert isinstance(promise_slow, ResponsePromise)
                promises.append(promise_slow)
                promises.append(promise_fast)

            assert len(promises) == 10

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            for i in range(5):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/3" in (await response.json())["url"]

            assert await pool.get_response() is None

    @notMacOS()
    async def test_multiplexing_stream_saturation(self) -> None:
        async with AsyncHTTPSConnectionPool(
            self.host, self.https_port, ca_certs=self.ca_authority, maxsize=2
        ) as pool:
            promises = []

            for i in range(300):
                promise = await pool.urlopen(
                    "GET", "/delay/1", multiplexed=True, preload_content=False
                )
                assert isinstance(promise, ResponsePromise)
                promises.append(promise)

            assert len(promises) == 300
            assert pool.num_connections == 2

            for i in range(300):
                response = await pool.get_response()
                assert response is not None
                assert response.status == 200
                assert "/delay/1" in (await response.json())["url"]

            assert await pool.get_response() is None
            assert pool.pool is not None and pool.num_connections == 2
