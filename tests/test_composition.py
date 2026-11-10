"""Regression tests for dependency wiring introduced by service extraction."""

from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from gift_offer.config import Config
from gift_offer.service import Service
from gift_offer.store import Store


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = AsyncMock()
        self.config = Config(token="test-token")
        self.service = Service(self.config, self.store, self.api, 999, clock=lambda: 100)

    async def test_replacing_runtime_dependencies_reaches_components(self):
        api = AsyncMock()
        self.service.api = api
        self.service.config = replace(self.config, event_chat_id=-123)
        await self.service.notifications_service.notify_topic(7, "event")
        api.call.assert_awaited_once_with(
            "sendMessage",
            chat_id=-123,
            message_thread_id=7,
            text="event",
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        self.api.call.assert_not_awaited()

    async def test_connection_update_and_lookup_share_payload_and_cache(self):
        connection = {
            "id": "connection",
            "user": {"id": 111, "first_name": "Buyer"},
            "user_chat_id": 111,
            "is_enabled": True,
            "rights": {"can_reply": True},
        }
        await self.service.handle({"business_connection": connection})
        result = await self.service.connections_service.connection("connection")
        self.assertIs(result, connection)
        self.assertIs(self.service.connections["connection"], connection)
        self.api.call.assert_not_awaited()
        # The same 60-second boundary still expires the shared cache.
        self.service.clock = lambda: 160
        refreshed = copy.deepcopy(connection)
        self.api.call.return_value = refreshed
        self.assertIs(await self.service.connection("connection"), refreshed)
        self.api.call.assert_awaited_once_with(
            "getBusinessConnection", business_connection_id="connection"
        )

    async def test_mutable_component_state_is_not_shared_between_instances(self):
        other = Service(self.config, self.store, self.api, 999)
        for attribute, key, value in (
            ("connections", "connection", {"id": "connection"}),
            ("connection_checked_at", "connection", 100),
            ("retry_at", "offer", 120),
            ("transfer_confirmation_waiting", ("offer", 111), 100),
        ):
            getattr(self.service, attribute)[key] = value
            self.assertEqual(getattr(other, attribute), {})
        self.service.delete_notices.add("connection")
        self.service.admin_recipient_waiting.add(111)
        self.assertEqual(other.delete_notices, set())
        self.assertEqual(other.admin_recipient_waiting, set())

    async def test_legacy_state_replacement_reaches_owning_component(self):
        retry_at = {"offer": 200}
        waiting = {111}
        self.service.retry_at = retry_at
        self.service.admin_recipient_waiting = waiting
        self.assertIs(self.service.cards_service.state.retry_at, retry_at)
        self.assertIs(self.service.administration_service.state.recipient_waiting, waiting)

    async def test_updates_and_maintenance_still_share_one_lock(self):
        trace = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def private(message, now):
            trace.append("private")
            entered.set()
            await release.wait()

        async def flush(now):
            trace.append("flush")

        async def cleanup(now):
            trace.append("cleanup")

        self.service.administration_service.private_message = private
        self.service.cards_service._flush = flush
        self.service.cards_service._cleanup_commands = cleanup
        update = asyncio.create_task(self.service.handle({"message": {}}))
        await entered.wait()
        maintenance = asyncio.create_task(self.service.tick())
        try:
            await asyncio.sleep(0)
            self.assertEqual(trace, ["private"])
            self.assertFalse(maintenance.done())
        finally:
            release.set()
            await asyncio.gather(update, maintenance)
        self.assertEqual(trace, ["private", "flush", "cleanup", "flush", "cleanup"])
