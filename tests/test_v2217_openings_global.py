"""v2.2.17：开头样本必须跨群取（她自己的习惯不分群）。

## 为什么

「刚…」句式是用户报的第一条症状：v2.2.6 那 27 条破冰里 15 条以「刚」开头。
v2.2.7 加了开头轮换闸（最近 6 条里同一个开头字 ≥3 次就禁这个字），v2.2.17 之前
它的样本只取**当前这个群**的历史。

线上 2026-11-10 的真实数据戳穿了这个口径：那天她发了 52 条群消息、散在 25 个群，
其中 13 条以「刚」开头——但按群分开看，每个群只见到 1~5 条，
「最近 6 条里同一开头 ≥3 次」在任何一个群里都凑不满。

实测（`scripts/replay_group_openings.py`）：按群回放 52 条 → 开头轮换闸**拦 0 条**；
改成跨群窗口回放（`scripts/replay_openings_cross.py`）→ **拦 9 条（17.3%）**，
而且拦的正是真问题簇：08:04/08:27「本宫方才把茶壶茶盏一并洗了」、14:32~15:45
那一片「刚用蹄子夹…」「刚趴窗边看外头」。

## 这一版

* `social.state.SocialState.own_group_openings(bid, now, hours=24.0, limit=6)`：
  跨所有群汇总她自己最近开口的正文（旧→新）。
* `social.engine._build_group_ctx` 的 `openings` 改成用它，取不到才回落本群历史。
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE


def _install_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("autonomous_social_v2217_test")
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = object
    api.event = event
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event})


_install_stub()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from social.state import SocialState  # noqa: E402
from social.verify import _opening_repeat_issue, opening_head  # noqa: E402

NOW = 1794384000.0
GANG_A = "刚用蹄子夹着杯子泡茶，洒了半桌"
GANG_B = "刚醒，睡0小时，趴床上赖着"
GANG_C = "刚把窗户推开"
BEN_A = "本宫把茶壶茶盏都洗了"
OTHER = "外头阴得发灰，蹄子踩地都凉"


class OwnGroupOpeningsTest(unittest.TestCase):
    """state.own_group_openings：跨群、按时间、限量。"""

    def _state(self) -> SocialState:
        tmp = tempfile.mkdtemp(prefix="aso_v2217_")
        return SocialState(str(Path(tmp) / "state.json"))

    def test_collects_across_groups(self):
        st = self._state()
        st.record_group_self_text("b1", "g1", GANG_A, NOW - 900.0)
        st.record_group_self_text("b1", "g2", GANG_B, NOW - 600.0)
        st.record_group_self_text("b1", "g3", GANG_C, NOW - 300.0)
        got = st.own_group_openings("b1", NOW, hours=24.0, limit=6)
        self.assertEqual(got, [GANG_A, GANG_B, GANG_C], f"没跨群汇总：{got!r}")

    def test_ignores_other_people(self):
        st = self._state()
        st.record_group_message(
            "b1", "g1", "umo://g1", "g1", "群友", "别人说的话", False, NOW - 600.0
        )
        self.assertEqual(st.own_group_openings("b1", NOW), [])

    def test_window_and_limit(self):
        st = self._state()
        st.record_group_self_text("b1", "g1", "太早的一条", NOW - 40 * 3600.0)
        for i in range(4):
            st.record_group_self_text("b1", f"g{i}", f"第{i}条{i}", NOW - (4 - i) * 60.0)
        got = st.own_group_openings("b1", NOW, hours=24.0, limit=3)
        self.assertEqual(len(got), 3, f"limit 没生效：{got!r}")
        self.assertNotIn("太早的一条", got, f"窗口外的被取进来了：{got!r}")

    def test_empty_state(self):
        st = self._state()
        self.assertEqual(st.own_group_openings("b404", NOW), [])


class CrossGroupGateTest(unittest.TestCase):
    """跨群样本喂给开头轮换闸，才拦得住「刚」。"""

    def test_third_same_head_is_blocked(self):
        openings = [GANG_A, OTHER, GANG_B, "嗯，在呢", "今天有点冷", GANG_C]
        why = _opening_repeat_issue(GANG_C, openings)
        self.assertTrue(why, "同一开头出现 3 次仍放行")
        self.assertIn("刚", str(why))

    def test_sparse_same_head_is_allowed(self):
        """一个群里只见一次「刚」时不该拦——否则误伤正常说话。"""
        openings = [GANG_A, OTHER, "本宫方才把双翼展开", "嗯，在呢", "今天有点冷"]
        self.assertIsNone(_opening_repeat_issue(GANG_C, openings))

    def test_ben_head_also_caught(self):
        openings = [BEN_A, OTHER, "本宫才把茶壶茶盏洗了", "嗯", "好", "本宫方在阳台展开双翼"]
        why = _opening_repeat_issue("本宫刚掩上阳台门", openings)
        self.assertTrue(why, "「本」开头同样成灾，闸没拦住")
        self.assertEqual(opening_head("本宫刚掩上阳台门"), "本")


class EngineUsesCrossGroupTest(unittest.TestCase):
    """生成侧接线：`_build_group_ctx` 必须用跨群样本。"""

    def test_ctx_uses_own_group_openings(self):
        src = io.open(
            ROOT / "social" / "engine.py", encoding="utf-8", errors="replace"
        ).read()
        self.assertIn("own_group_openings", src, "engine 没接跨群开头样本")
        self.assertIn("_x_openings", src, "engine 的 openings 没走跨群变量")
        i = src.index("def _build_group_ctx")
        j = src.index("def _species_of", i)
        seg = src[i:j]
        self.assertIn("own_group_openings", seg, "_build_group_ctx 里没调跨群取数")

    def test_state_method_exists(self):
        self.assertTrue(callable(getattr(SocialState, "own_group_openings", None)))


if __name__ == "__main__":
    unittest.main()
