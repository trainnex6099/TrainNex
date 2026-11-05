"""TrainNex readiness checks and an isolated exercise of its production workflow."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

TEST_LOCK = asyncio.Lock()


async def run_system_test(runtime):
    if TEST_LOCK.locked():
        raise ValueError("A TrainNex system test is already running. Wait for its result.")
    async with TEST_LOCK:
        checks = []

        def record(label, passed, detail):
            checks.append({"label": label, "passed": bool(passed), "detail": detail[:100]})

        application = runtime.bot.get_guild(runtime.APPLICATION_SERVER_ID)
        training = runtime.bot.get_guild(runtime.TRAINING_SERVER_ID)
        record("Discord connections", application is not None and training is not None,
               "Application and training servers connected." if application and training else "Check configured server IDs and bot membership.")
        record("FTO role", training is not None and training.get_role(runtime.FTO_ROLE_ID) is not None,
               "Configured FTO role exists." if training and training.get_role(runtime.FTO_ROLE_ID) else "Configure an FTO role in the training server.")
        channel_checks = [
            ("Approval channel", application, runtime.APPROVAL_CHANNEL_ID, False),
            ("Fallback channel", training, runtime.ANNOUNCEMENT_CHANNEL_ID, True),
            ("Claim notifications", training, runtime.CLAIM_NOTIFICATION_CHANNEL_ID, True),
        ]
        for label, guild, channel_id, writes in channel_checks:
            channel = guild.get_channel(channel_id) if guild else None
            if channel is None or guild.me is None:
                record(label, False, "Configured channel or bot membership is unavailable.")
                continue
            permissions = channel.permissions_for(guild.me)
            required = ["view_channel", "read_message_history"]
            if writes:
                required += ["send_messages", "embed_links"]
            missing = [name.replace("_", " ") for name in required if not getattr(permissions, name, False)]
            record(label, not missing, "Missing: " + ", ".join(missing) if missing else "Channel access and required permissions are available.")
        if application and runtime.MELONY_BOT_ID:
            try:
                member = application.get_member(runtime.MELONY_BOT_ID) or await application.fetch_member(runtime.MELONY_BOT_ID)
                record("Approval bot", member.bot, "Configured approval bot is a server member.")
            except Exception:
                record("Approval bot", False, "The configured approval bot could not be found.")
        else:
            record("Approval bot", False, "Configure the Melony approval bot ID.")
        try:
            backup = await runtime.SUMMIT_API.get("/api/summit/operations", runtime.TRAINING_SERVER_ID)
            record("Portal connection", runtime.SUMMIT_API.configured and "state" in backup,
                   "Authenticated portal connection responded.")
        except Exception:
            record("Portal connection", False, "Check Summit’s portal URL, credentials, and connectivity.")
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="summit-trainnex-test-") as folder:
            environment = os.environ.copy()
            environment.update(SUMMIT_DATA_DIR=folder, DISCORD_TOKEN="isolated-system-test",
                               SUMMIT_API_BASE_URL="", SUMMIT_API_SECRET="", TRAINNEX_TEST_ROOT=str(root))
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--worker",
                cwd=folder, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
                line = next((line for line in stdout.decode("utf-8", "replace").splitlines()
                             if line.startswith("TRAINNEX_SYSTEM_TEST=")), None)
                if not line:
                    raise ValueError("Missing test report")
                checks.extend(json.loads(line.split("=", 1)[1])["checks"])
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                record("Workflow simulation", False, "The isolated workflow exceeded its 60-second timeout.")
            except (ValueError, KeyError, json.JSONDecodeError):
                record("Workflow simulation", False, "The isolated workflow did not return a valid report.")
        passed = sum(check["passed"] for check in checks)
        return {"test": "trainnex-system", "status": f"{passed}/{len(checks)} checks passed",
                "passed": passed == len(checks), "checks": checks,
                "mode": "Readiness checks plus isolated production-workflow simulation"}


def run_worker():
    sys.path.insert(0, os.environ["TRAINNEX_TEST_ROOT"])
    import discord
    import main
    from summit_bridge import training_wire

    first, second, trainee = 111111111111111111, 222222222222222222, 333333333333333333
    labels = {
        "test_approval_and_claim": "Approval → queue → offer → claim notification",
        "test_pass_and_queue_rotation": "Pass offer and rotate queue",
        "test_timeout_and_fallback": "Expire offers and open fallback",
        "test_fallback_claim": "Claim fallback and prevent a second claim",
        "test_blocked_dm": "Blocked DM routes to the next FTO",
        "test_offer_ownership": "Reject another member’s offer claim",
        "test_restart_and_portal_state": "Save, reload, and serialize portal state",
        "test_queue_permissions": "Queue manager permissions",
    }

    class WorkflowTests(unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            self.message_number = 0
            self.messages = {}
            self.patches = []
            self.sent_channels = []

            def new_message():
                self.message_number += 1
                message = SimpleNamespace(id=900000000000000000 + self.message_number, edit=AsyncMock())
                self.messages[message.id] = message
                return message

            async def send(**kwargs):
                for file in kwargs.get("files", []):
                    file.close()
                return new_message()

            async def fetch_message(message_id):
                return self.messages[message_id]

            self.trainers = {}
            for uid in (first, second):
                member = MagicMock(spec=discord.Member)
                member.id = uid
                member.bot = False
                member.roles = [SimpleNamespace(id=55)]
                member.dm_channel = SimpleNamespace(id=uid, send=AsyncMock(side_effect=send),
                                                    fetch_message=AsyncMock(side_effect=fetch_message))
                self.trainers[uid] = member
            self.guild = SimpleNamespace(id=44, get_member=lambda uid:self.trainers.get(uid),
                                         fetch_member=AsyncMock(side_effect=lambda uid:self.trainers[uid]))
            for member in self.trainers.values():
                member.guild = self.guild
            self.channel = SimpleNamespace(id=66, send=AsyncMock(side_effect=send),
                                           fetch_message=AsyncMock(side_effect=fetch_message))
            self.data = {"queue":[first, second], "offers":{}, "pending":{}, "processed_approval_messages":[]}
            replacements = {
                "DATA": self.data, "TRAINING_SERVER_ID":44, "APPLICATION_SERVER_ID":43,
                "APPROVAL_CHANNEL_ID":77, "MELONY_BOT_ID":88, "FTO_ROLE_ID":55,
                "FTO_COMMANDER_ROLE_ID":56, "FTO_OVERSEER_ROLE_ID":57,
                "ANNOUNCEMENT_CHANNEL_ID":66, "CLAIM_NOTIFICATION_CHANNEL_ID":67,
                "resolve_user":AsyncMock(side_effect=lambda uid:self.trainers.get(uid)),
                "get_training_channel":AsyncMock(return_value=self.channel),
            }
            for name, value in replacements.items():
                self.patches.append(patch.object(main, name, value))
            self.patches += [
                patch.object(main.bot, "get_guild", return_value=self.guild),
                patch.object(main.bot, "schedule_offer_expiration"),
                patch.object(main.bot, "expiration_tasks", {}),
                patch.object(main, "publish_portal_state", AsyncMock()),
            ]
            for item in self.patches:
                item.start()
            main.save_data()

        def tearDown(self):
            for item in reversed(self.patches):
                item.stop()

        def interaction(self, uid, guild=True):
            return SimpleNamespace(user=self.trainers[uid], guild=self.guild if guild else None,
                                   response=SimpleNamespace(is_done=lambda:False, send_message=AsyncMock()))

        async def offer(self):
            await main.route_trainee(trainee, "https://example.invalid/test-approval")
            return next(key for key,value in self.data["offers"].items() if value["state"]=="pending")

        async def test_approval_and_claim(self):
            message = SimpleNamespace(
                id=123, guild=SimpleNamespace(id=43), channel=SimpleNamespace(id=77),
                author=SimpleNamespace(id=88), mentions=[], raw_mentions=[],
                content=f"<@{trainee}>", jump_url="https://example.invalid/test-approval",
                embeds=[discord.Embed(title="WCSO Department Application | Response Approved")],
            )
            self.assertTrue(main.is_melony_approval(message))
            await main.process_approval(message)
            self.assertEqual(len(self.data["offers"]),1)
            await main.process_approval(message)
            self.assertEqual(len(self.data["offers"]),1)
            offer_id = next(iter(self.data["offers"]))
            await main.handle_offer_claim(self.interaction(first), offer_id)
            self.assertEqual(self.data["pending"][str(trainee)]["claimed_by"],first)
            self.channel.send.assert_awaited_once()
            self.assertEqual(self.data["queue"],[second,first])

        async def test_pass_and_queue_rotation(self):
            offer_id = await self.offer()
            await main.handle_offer_pass(self.interaction(first),offer_id)
            next_offer = next(value for value in self.data["offers"].values() if value["state"]=="pending")
            self.assertEqual(next_offer["trainer_id"],second)
            self.assertEqual(self.data["queue"],[second,first])
            await main.handle_offer_claim(self.interaction(second),next_offer["offer_id"])
            self.assertEqual(self.data["pending"][str(trainee)]["claimed_by"],second)

        async def test_timeout_and_fallback(self):
            offer_id = await self.offer()
            await main.expire_offer(offer_id)
            next_id = next(key for key,value in self.data["offers"].items() if value["state"]=="pending")
            await main.expire_offer(next_id)
            self.assertEqual(self.data["pending"][str(trainee)]["state"],"open")
            self.assertEqual(self.channel.send.await_count,1)

        async def test_fallback_claim(self):
            self.data["queue"]=[]
            await main.route_trainee(trainee, "https://example.invalid/test-approval")
            await main.handle_fallback_claim(self.interaction(first),trainee)
            self.assertEqual(self.data["pending"][str(trainee)]["claimed_by"],first)
            count = self.channel.send.await_count
            await main.handle_fallback_claim(self.interaction(second),trainee)
            self.assertEqual(self.channel.send.await_count,count)
            self.assertEqual(self.data["pending"][str(trainee)]["claimed_by"],first)

        async def test_blocked_dm(self):
            self.trainers[first].dm_channel.send.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason="Forbidden"),"Test blocked DM")
            offer_id=await self.offer()
            self.assertEqual(self.data["offers"][offer_id]["trainer_id"],second)

        async def test_offer_ownership(self):
            offer_id=await self.offer()
            await main.handle_offer_claim(self.interaction(second),offer_id)
            self.assertEqual(self.data["offers"][offer_id]["state"],"pending")
            await main.handle_offer_claim(self.interaction(first),offer_id)
            count=self.channel.send.await_count
            await main.handle_offer_claim(self.interaction(first),offer_id)
            self.assertEqual(self.channel.send.await_count,count)

        async def test_restart_and_portal_state(self):
            offer_id=await self.offer()
            main.save_data()
            restored=main.load_data()
            self.assertEqual(restored,self.data)
            wire=training_wire(restored)
            self.assertEqual(wire["offers"][offer_id]["trainer_id"],str(first))
            view=main.OfferView(offer_id)
            self.assertTrue(any(button.custom_id==f"trainnex:claim:{offer_id}" for button in view.children))

        async def test_queue_permissions(self):
            member=self.trainers[first]
            member.guild_permissions.administrator=False
            self.assertFalse(main.is_queue_manager(member))
            member.roles.append(SimpleNamespace(id=56))
            self.assertTrue(main.is_queue_manager(member))

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.checks=[]

        def addSuccess(self,test):
            super().addSuccess(test)
            self.checks.append({"label":labels[test._testMethodName],"passed":True,"detail":"Production workflow completed in isolated test storage."})

        def failure(self,test,error):
            detail = type(error[1]).__name__ + ": " + str(error[1])
            self.checks.append({"label":labels.get(test._testMethodName,"Workflow test"),"passed":False,"detail":detail[:100]})

        def addFailure(self,test,error):
            super().addFailure(test,error)
            self.failure(test,error)

        def addError(self,test,error):
            super().addError(test,error)
            self.failure(test,error)

    result=Result()
    # These checks exercise production callbacks with test Discord I/O.
    # Block creation of an HTTP session so an accidental live request fails.
    with patch("aiohttp.ClientSession", side_effect=RuntimeError("Live HTTP is disabled in the isolated test")):
        unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests).run(result)
    print("TRAINNEX_SYSTEM_TEST="+json.dumps({"checks":result.checks}))


if __name__=="__main__" and "--worker" in sys.argv:
    run_worker()
