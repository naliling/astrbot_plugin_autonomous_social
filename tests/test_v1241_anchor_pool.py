"""由头池是**一次性队列**，不是待办清单。

我一度以为「未用的由头被 prune 掉了」是 bug——`prune_anchors` 对 `used_at=0` 的
判定 `0 + 45天 > now` 恒为假，所以每加一条新由头，上一条就被丢掉，池子上限实际是 1，
而不是 `add_anchor` 注释里写的 5、更不是 v1.23 声明的「高档 12」。

改完（未用的按寿命留着、上限放到 12）之后，7 天回归仿真里「正常」场景的送达
**从 127 条掉到 19 条**。原因和想的不一样：

    上限 1  → 每条用完就让位，下一轮从当下状态重新派生，6 轮拿到 18 条候选
    上限 12 → 池子里**用过的**还在，`add_anchor` 又不会重加同样的事，
               候选逐轮变少（3→2→1），派生被饿死

**所以上限 1 才是对的机制**，`prune_anchors` 没写错，是注释在误导人。
这一版把注释改对，把「由头池 12/8/4」的说法从 CHANGELOG / README 里去掉。
"""
import logging
import os
import sys
import time
import types
import unittest

for _n, _a in (("astrbot", {}), ("astrbot.api", {"logger": logging.getLogger("x")}),
               ("astrbot.api.event", {"MessageChain": object})):
    _m = types.ModuleType(_n)
    for _k, _v in _a.items():
        setattr(_m, _k, _v)
    sys.modules[_n] = _m

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social.anchors import (  # noqa: E402
    ANCHOR_MAX, KIND_CUE, KIND_DERIVED, add_anchor, load_anchors,
    prepare_round_anchors,
)

BODY = {
    "day": {"doing": "整理个案笔记", "done": ["门诊接待"], "next": ["要睡了"]},
    "weather": "外面在下雨",
    "asleep": False,
}


class PoolIsOneShotQueue(unittest.TestCase):
    def test_consumed_anchor_makes_way_for_fresh_ones(self):
        """一轮派生 → 消费一条 → 下一轮**必须还能派生出新的**。

        这是整个机制的关键：留着用过的不放，派生就被饿死（实测 127 条 → 19 条）。
        """
        u, now = {}, time.time()
        total = 0
        for _ in range(6):
            picks = prepare_round_anchors(u, dict(BODY), now, limit=12, tier="high")
            total += len(picks)
            for p in picks[:1]:
                p["used_at"] = now
            now += 600
        self.assertGreaterEqual(
            total, 12,
            f"6 轮只派出 {total} 条候选 —— 池子在堵死派生（上限太大时就是这个现象）",
        )

    def test_pool_never_exceeds_the_storage_cap(self):
        now = time.time()
        u = {}
        for i in range(20):
            add_anchor(u, KIND_DERIVED, f"由头{i}", now=now)
        self.assertLessEqual(len(load_anchors(u)), ANCHOR_MAX)

    def test_ttl_still_applies_per_kind(self):
        """对方那边的事留得久，她这边刚发生的留得短。"""
        from social.anchors import _KIND_TTL
        self.assertGreater(_KIND_TTL[KIND_CUE], _KIND_TTL[KIND_DERIVED])


class NoOverclaimedPoolSize(unittest.TestCase):
    """文档不许再把由头池说成 12——那是不成立的。

    只查**表格里的**声明，不查 CHANGELOG 里那句「曾经写着 12」的更正说明——
    那是故意留下的反例，不该被当成残留。
    """

    _CLAIMS = ("由头池 12", "由头池 12/8/4", "| 12 |", "高档 12")

    # 这些词出现在「已经改掉了」的说明里是正常的，只在**当它是真的规格**时才算残留。
    _EXCUSES = ("曾经", "不成立", "改掉", "夸大", "回退", "做不到", "说成", "实际")

    def test_docs_do_not_claim_a_twelve_slot_pool(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("README.md", "CHANGELOG.md"):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines, 1):
                if any(w in line for w in self._EXCUSES):
                    continue          # 更正/说明性的话，跳过
                for claim in self._CLAIMS:
                    self.assertNotIn(claim, line, f"{name}:{i} 还在声明「{claim}」")

    def test_no_table_claims_twelve(self):
        """配置表里更不该出现：表格里的数字会被当成规格照着理解。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("README.md", "CHANGELOG.md"):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines, 1):
                if not line.strip().startswith("|"):
                    continue          # 只看表格行
                for claim in self._CLAIMS:
                    self.assertNotIn(claim, line, f"{name}:{i} 表格里还写着「{claim}」")


if __name__ == "__main__":
    unittest.main()
