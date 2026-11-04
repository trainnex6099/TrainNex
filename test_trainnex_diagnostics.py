"""Validate the production self-test runs in a separate process and storage."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import summit_trainnex_diagnostics as diagnostics


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_run_checks_readiness_and_all_workflow_paths(self):
        permissions = SimpleNamespace(view_channel=True, read_message_history=True,
                                      send_messages=True, embed_links=True, attach_files=True)
        channel = SimpleNamespace(permissions_for=lambda member: permissions)
        guild = SimpleNamespace(me=object(), get_channel=lambda channel_id: channel,
                                get_role=lambda role_id: object(),
                                get_member=lambda member_id: SimpleNamespace(bot=True))
        api = SimpleNamespace(configured=True, get=AsyncMock(return_value={"state": {}}))
        runtime = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda guild_id:guild),
            APPLICATION_SERVER_ID=43, TRAINING_SERVER_ID=44, APPROVAL_CHANNEL_ID=77,
            MELONY_BOT_ID=88, FTO_ROLE_ID=55, ANNOUNCEMENT_CHANNEL_ID=66,
            CLAIM_NOTIFICATION_CHANNEL_ID=67, SUMMIT_API=api,
        )
        result = await diagnostics.run_system_test(runtime)
        failed = [check for check in result["checks"] if not check["passed"]]
        self.assertEqual(failed, [])
        self.assertEqual(len(result["checks"]), 16)
        self.assertEqual(result["test"], "trainnex-system")
        api.get.assert_awaited_once_with("/api/summit/operations", 44)

    async def test_missing_servers_and_portal_are_reported_as_failures(self):
        runtime = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda guild_id:None),
            APPLICATION_SERVER_ID=0, TRAINING_SERVER_ID=0, APPROVAL_CHANNEL_ID=0,
            MELONY_BOT_ID=0, FTO_ROLE_ID=0, ANNOUNCEMENT_CHANNEL_ID=0,
            CLAIM_NOTIFICATION_CHANNEL_ID=0,
            SUMMIT_API=SimpleNamespace(configured=False, get=AsyncMock(side_effect=RuntimeError("offline"))),
        )
        result=await diagnostics.run_system_test(runtime)
        self.assertFalse(result["passed"])
        failures={check["label"] for check in result["checks"] if not check["passed"]}
        self.assertIn("Discord connections",failures)
        self.assertIn("Portal connection",failures)


if __name__=="__main__":
    unittest.main()
