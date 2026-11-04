import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import summit_trainnex_drill as drills


class DrillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        drills.DRILLS.clear()
        self.actor = SimpleNamespace(id=1, mention='<@1>', send=AsyncMock())
        self.first = SimpleNamespace(id=2, bot=False, display_name='First', mention='<@2>', send=AsyncMock())
        self.second = SimpleNamespace(id=3, bot=False, display_name='Second', mention='<@3>', send=AsyncMock())
        self.channel = SimpleNamespace(send=AsyncMock())
        self.runtime = SimpleNamespace(TRAINING_SERVER_ID=4, ANNOUNCEMENT_CHANNEL_ID=5,
            CLAIM_NOTIFICATION_CHANNEL_ID=6, has_fto_role=lambda member:True,
            bot=SimpleNamespace(get_guild=lambda guild:SimpleNamespace(get_member=lambda uid:{2:self.first,3:self.second}.get(uid))),
            get_training_channel=AsyncMock(return_value=self.channel))

    def interaction(self, member):
        return SimpleNamespace(user=member, response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_claim_is_restricted_and_duplicate_claim_does_not_notify_twice(self):
        await drills.start(self.runtime,self.actor,[2])
        drill=drills.DRILLS[1];view=drill.view
        wrong=self.interaction(self.second)
        await view.respond(wrong,'claim')
        wrong.response.send_message.assert_awaited_once()
        self.assertTrue(drill.active)
        await asyncio.gather(view.respond(self.interaction(self.first),'claim'),view.respond(self.interaction(self.first),'claim'))
        self.assertFalse(drill.active)
        self.assertEqual(self.channel.send.await_count,1)
        self.actor.send.assert_awaited_once()
        self.assertIn('Claim completed',drills.status(1)['status'])

    async def test_pass_timeout_and_fallback_only_selected_can_claim(self):
        await drills.start(self.runtime,self.actor,[2,3])
        drill=drills.DRILLS[1]
        await drill.view.respond(self.interaction(self.first),'pass')
        self.second.send.assert_awaited_once()
        await drill.view.on_timeout()
        self.assertIsNone(drill.view.assigned)
        stranger=SimpleNamespace(id=9)
        await drill.view.respond(self.interaction(stranger),'claim')
        self.assertTrue(drill.active)
        await drill.view.respond(self.interaction(self.second),'claim')
        self.assertEqual(self.channel.send.await_count,2)
        self.assertFalse(drill.active)

    async def test_cancel_and_duplicate_participant_protection(self):
        await drills.start(self.runtime,self.actor,[2])
        with self.assertRaises(ValueError):
            await drills.start(self.runtime,self.actor,[3])
        actor2=SimpleNamespace(id=10)
        with self.assertRaises(ValueError):
            await drills.start(self.runtime,actor2,[2])
        await drills.cancel(1)
        self.assertFalse(drills.status(1)['active'])
        self.assertTrue(drills.DRILLS[1].view.is_finished())

    async def test_non_fto_cannot_receive_test_offer(self):
        self.runtime.has_fto_role=lambda member:False
        with self.assertRaises(ValueError):
            await drills.start(self.runtime,self.actor,[2])
        self.first.send.assert_not_awaited()
