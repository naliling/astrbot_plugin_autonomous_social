"""Offline private-send regression: real engine, clock, prompts and guards; no AstrBot/provider calls."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_v1289_continue import NOW, _engine, _user
from social import anchors, threads
from social.generator import MessageGenerator

OLD_QUESTION = '外面降温了，你冷不冷？'
OLD_ANSWER = '（拢了拢翅膀）不冷啦，刚才我咬着扫帚把屋里打扫干净了。'
CHORE = '今天我用嘴叼着扫帚把地扫完了。'


class PrivateTurnTest(unittest.IsolatedAsyncioTestCase):
    def make_case(self, **config):
        eng = _engine(**config)
        user = _user(eng, last_seen=NOW - 500 * 3600, last_message=OLD_QUESTION,
                     last_spoken=NOW - 4 * 3600, last_spoken_text=OLD_ANSWER,
                     topics=[OLD_QUESTION],
                     conversation=[
                         {'dir': 'in', 'text': OLD_QUESTION, 'ts': NOW - 500 * 3600},
                         {'dir': 'out', 'text': OLD_ANSWER, 'ts': NOW - 4 * 3600},
                     ])
        eng._load_session_history = AsyncMock(return_value=[{'dir': 'in', 'text': OLD_QUESTION}])
        eng._fetch_memory_note = AsyncMock(return_value='')
        eng._persona = AsyncMock(return_value=('娜莉灵', '你说话亲切，可以自然地描写翅膀。'))
        eng.input_state = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        eng._send_with_retry = AsyncMock(return_value=(True, '', False))
        return eng, user

    async def send(self, eng, user, body, meta=None, during=None):
        async def decide(*args, **kwargs):
            if during:
                during()
            return SimpleNamespace(raw_text=body, send=True, parts=[body], promise='')
        eng.generator = SimpleNamespace(decide=decide)
        meta = meta if meta is not None else {'category': 'greet', 'mode': 'greet_morning', 'msg_type': 'greeting'}
        return await eng._speak_inner('b1', 'u1', user, 0.0, NOW, preset=('', meta))

    async def test_reply_without_open_human_turn_does_not_reach_adapter(self):
        for candidate in ('不冷呀🐾', '真的不冷啦。', '（拢拢翅膀）不过你一问就不冷啦。'):
            with self.subTest(candidate=candidate):
                eng, user = self.make_case()
                sent, _ = await self.send(eng, user, candidate)
                self.assertFalse(sent)
                eng._send_with_retry.assert_not_awaited()

    async def test_invented_chore_is_blocked_even_without_a_plan(self):
        eng, user = self.make_case()
        sent, _ = await self.send(eng, user, CHORE)
        self.assertFalse(sent)
        eng._send_with_retry.assert_not_awaited()

    async def test_valid_starter_with_character_gesture_still_delivers(self):
        eng, user = self.make_case()
        body = '（轻轻歪头）有点想你了，过来陪我坐会儿嘛。'
        sent, reason = await self.send(eng, user, body)
        self.assertTrue(sent, reason)
        eng._send_with_retry.assert_awaited_once()

    def test_reply_without_turn_needs_a_live_question(self):
        from social import private_turn
        self.assertTrue(private_turn.reply_without_turn('不冷呀🐾'))
        self.assertTrue(private_turn.reply_without_turn('（拢翅膀）不过你一问就不冷啦。'))
        self.assertFalse(private_turn.reply_without_turn('（伸懒腰）有点想你了。'))

    async def test_waiting_special_care_does_not_carry_on_the_same_thread(self):
        eng, user = self.make_case(special_care_enabled=True, special_care_list=['u1'])
        user.update(last_seen=NOW - 5400, last_message='今天路上好堵',
                    last_sent=NOW - 3600, last_proactive_sent=NOW - 3600,
                    last_proactive_replied=False, pending_result='ignored', silent_sent=1)
        meta = threads.thread_meta('continue', about='今天路上好堵', silence_minutes=60)
        sent, _ = await self.send(eng, user, '路上慢点，我不急着等你。', meta)
        self.assertFalse(sent)
        eng._send_with_retry.assert_not_awaited()

    async def test_unrelated_live_cue_does_not_bypass_the_pending_gate(self):
        eng, user = self.make_case(cue_followup=True)
        user.update(last_sent=NOW - 4 * 3600, last_proactive_sent=NOW - 4 * 3600,
                    last_proactive_replied=False, pending_result='waiting', silent_sent=1,
                    cue='今天下午面试', cue_due=NOW - 30)
        unrelated = anchors.anchor_meta({'kind': anchors.KIND_MISS, 'about': '有点想你了'})
        sent, _ = await self.send(eng, user, '闲下来就有点想你了。', unrelated)
        self.assertFalse(sent)
        eng._send_with_retry.assert_not_awaited()

    async def test_new_human_turn_invalidates_draft_even_after_two_minutes(self):
        eng, user = self.make_case()
        def during():
            user['last_seen'] = NOW + 1
            eng._time = lambda: NOW + 181
        sent, _ = await self.send(eng, user, '早呀，想找你坐一会儿。', during=during)
        self.assertFalse(sent)
        eng._send_with_retry.assert_not_awaited()

    async def test_old_lines_do_not_enter_a_new_starter_prompt(self):
        eng, user = self.make_case()
        gen = MessageGenerator(AsyncMock(return_value=object()), eng.cfg)
        prompt_seen = []
        async def decide(umo, target, core_context, reason, meta, persona, mind, **kwargs):
            _, prompt, _, _ = await gen._compose(umo, target, core_context, reason, meta,
                                                persona, mind, decide=True, **kwargs)
            prompt_seen.append(prompt)
            return SimpleNamespace(raw_text='', send=False, why_not='离线探针')
        eng.generator = SimpleNamespace(decide=decide)
        meta = {'category': 'greet', 'mode': 'greet_morning', 'msg_type': 'greeting'}
        await eng._speak_inner('b1', 'u1', user, 0.0, NOW, preset=('', meta))
        self.assertEqual(len(prompt_seen), 1)
        self.assertNotIn(OLD_QUESTION, prompt_seen[0])
        self.assertNotIn('你自己上一句对TA说的是', prompt_seen[0])
        self.assertIn('你说话亲切', prompt_seen[0])

    async def test_live_cue_can_follow_up_a_specific_real_event(self):
        eng, user = self.make_case(cue_followup=True)
        user.update(last_sent=NOW - 4 * 3600, last_proactive_sent=NOW - 4 * 3600,
                    last_proactive_replied=False, pending_result='waiting', silent_sent=1,
                    cue='今天下午面试', cue_due=NOW - 30)
        selected = anchors.anchor_meta({'kind': anchors.KIND_CUE, 'about': '今天下午面试'})
        sent, reason = await self.send(eng, user, '下午的面试还顺利吗？', selected)
        self.assertTrue(sent, reason)
        self.assertFalse(user['cue'])
