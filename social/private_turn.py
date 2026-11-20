"""Context and delivery checks for a private proactive message, not a normal reply."""
from __future__ import annotations

import re
from typing import Any

from .reasoning import TOPIC_CONTINUE_THRESHOLD, _num, live_cue
from .verify import _bigrams, strip_actions

THREADS = frozenset({'probe', 'presence', 'continue', 'continuity'})


def peer_revision(user: dict) -> tuple:
    # Count catches several events with the same clock tick. Neither outgoing
    # text nor the separate reply-rate bookkeeping is a new peer turn.
    return (str(user.get('umo') or ''), _num(user.get('last_seen')),
            _num(user.get('message_count')))


def last_proactive(user: dict) -> float:
    return max(_num(user.get('last_sent')), _num(user.get('last_proactive_sent')))


def unanswered(user: dict) -> bool:
    sent = last_proactive(user)
    return sent > 0 and _num(user.get('last_seen')) <= sent


def selected_due_cue(user: dict, meta: dict, now: float) -> str:
    cue = live_cue(user, now)
    if not cue:
        return ''
    anchor = meta.get('anchor')
    kind = anchor.get('kind') if isinstance(anchor, dict) else ''
    category = meta.get('category')
    if not (category == 'cue' or (category == 'anchor' and kind == 'cue')):
        return ''
    selected = str(meta.get('cue') or meta.get('about') or '').split()
    return cue if selected == cue.split() else ''


def dialogue(user: dict, meta: dict, now: float, window: float) -> list[dict]:
    """Only time-stamped dialogue can be a continuation; untimed history isn't a new turn."""
    category = meta.get('category')
    if category not in THREADS and category != 'closer':
        return []
    if unanswered(user):
        return []
    age_limit = max(TOPIC_CONTINUE_THRESHOLD, window) if category in {'continue', 'closer'} else TOPIC_CONTINUE_THRESHOLD
    seen = _num(user.get('last_seen'))
    if seen <= 0 or not 0 <= now - seen <= age_limit:
        return []
    source = user.get('conversation')
    result = []
    for row in source if isinstance(source, list) else ():
        if not isinstance(row, dict):
            continue
        ts = _num(row.get('ts'))
        text = str(row.get('text') or '').strip()
        direction = row.get('dir', 'in')
        if ts > 0 and max(now - age_limit, last_proactive(user)) < ts <= now and text and direction in ('in', 'out'):
            result.append({'dir': direction, 'text': text, 'ts': ts})
    return result if any(row['dir'] == 'in' for row in result) else []


# These clauses presuppose a question/statement by the peer. General feelings
# (including being cold), greetings, invitations and RP gestures are not a reply.
_ANSWER_FRAGMENT = re.compile(
    r'^(?:(?:不过|可是|其实|真的?|当然|一点也|一点都|也|还)\s*){0,3}'
    r'(?:(?:我)?(?:不|没)(?:太|那么|有点)?(?:冷|热|饿|困|忙|累|疼|怕|难过|生气|介意|着急|担心|在生气)'
    r'|(?:是|不是|对|不对|有|没有|好|不好|行|不行|可以|不可以|知道了|知道啦|我知道)' 
    r'|(?:在|不在|在呢|没事|没问题|是的|对的|不是的|没有的))'
    r'(?:的|呀|啊|啦|呢|嘛|哦|噢|喔|咯|哟|呐|唷|哈|诶|欸|喵|汪)*$'
)
_ASSUMED_QUESTION = re.compile(r'(?:你|您)(?:刚(?:才|刚)?|方才)?(?:这么|这样|一)?(?:一问|问我|问起|问[，。])')
_CLAUSE = re.compile(r'[，,。！？!?\n；;：:、…]+')
_NON_WORD = re.compile(r'[^\w\s]', re.UNICODE)


def reply_without_turn(text: str) -> bool:
    spoken = strip_actions(text)
    if _ASSUMED_QUESTION.search(spoken):
        # An invitation to ask something new is not claiming that they asked.
        if re.search(r'(?:你|您)(?:刚才|刚刚|方才|这么一|这样一|一)问', spoken):
            return True
    clauses = [_NON_WORD.sub('', c).strip() for c in _CLAUSE.split(spoken) if _NON_WORD.sub('', c).strip()]
    return bool(clauses and _ANSWER_FRAGMENT.fullmatch(clauses[0]))


# Narrow, high-signal claims about concrete activities. We do not try to prove
# every line of dialogue or police a character's feelings/actions as fiction.
_ACTIVITY = re.compile(
    r'扫地|扫帚|打扫|拖地|拖把|收衣服|晾衣服|洗衣服|叠衣服|擦(?:窗|玻璃|桌|地)|抹布|'
    r'洗碗|做饭|煮饭|炒菜|烤面包|取快递|寄快递|开会|看诊|写完(?:稿|报告)|'
    r'把[^，。！？；]{1,14}(?:收拾|修|擦|扫|洗|整理|写)(?:完|好了|干净了|掉了)'
)
_ASSERTED = re.compile(r'刚才|刚刚|刚|正在|在.{0,12}(?:擦|扫|叼|洗|煮|烤)|一直在|试着|已经|完了|做好了|过了|干净了|着|今天|昨[天晚]|今[早晚]|下午|上午|了$')
_NON_ASSERTED = re.compile(r'想(?:去|要|着|试试|先|和你|跟你|给你)|想[^我你，。]{0,5}(?:打扫|收拾|整理|擦|扫|洗|做饭)|打算|准备|计划|待会|等(?:下|会|你)|回头|明[天早晚]|以后|要不要|能不能|假如|如果|梦(?:见|到|里)|好像|也许|可能|不妨|记得|别忘|你(?:要|可以|刚|在|说|已经)|一起|算不上')


def _activity_claims(text: str):
    # Keep text inside actions too: a gesture is fine, a factual chore tucked
    # inside parentheses isn't exempt. Question punctuation is significant.
    for sentence in re.split(r'(?<=[。！？!?；;\n])', text):
        if sentence.rstrip().endswith(('？', '?')):
            continue
        for clause in _CLAUSE.split(sentence):
            clean = _NON_WORD.sub('', clause).strip()
            if (_ACTIVITY.search(clean) and _ASSERTED.search(clean)
                    and not _NON_ASSERTED.search(clean)):
                yield clean


def unsupported_activity(text: str, facts: list[str]) -> str:
    trusted = [_NON_WORD.sub('', s) for s in facts if s]
    for claim in _activity_claims(text):
        gram = _bigrams(claim)
        if not any(claim in fact or (
            len(gram) >= 4 and len(gram & _bigrams(fact)) / len(gram) >= 0.7
        ) for fact in trusted):
            return claim
    return ''


_TIME_WORDS = ('今天', '明天', '昨天', '后天', '上午', '下午', '中午', '早上',
               '晚上', '待会', '等会', '回头', '晚点', '现在', '之前', '刚才')


def on_due_topic(text: str, cue: str) -> bool:
    """A due-cue send must actually mention the thing it was selected for."""
    cue = str(cue or '').strip()
    if not cue:
        return True
    norm = lambda s: _NON_WORD.sub('', str(s or '')).casefold()
    body, target = norm(text), norm(cue)
    if not body or not target:
        return False
    if target in body or body in target:
        return True
    # The cue often carries a time word ("今天下午面试") that the body naturally
    # drops or rewrites ("下午的面试还顺利吗"). Match on substantive 2-char
    # slices after removing those time words, so "面试" still counts.
    stripped = target
    for word in _TIME_WORDS:
        stripped = stripped.replace(norm(word), '')
    grams = {g for g in _bigrams(stripped) if g.strip()} if stripped else set()
    if not grams:
        grams = {g for g in _bigrams(target) if g.strip()}
    body_grams = _bigrams(body)
    if not grams:
        return False
    return len(grams & body_grams) / len(grams) >= 0.5


def event_facts(cards, reason_meta=None) -> list[str]:
    """Only non-generated, concrete cards count as evidence of a completed deed."""
    out: list[str] = []
    for card in cards or ():
        if not isinstance(card, dict):
            continue
        if card.get('generated') is True:
            continue
        source = str(card.get('source') or '')
        if source in ('日程', '想做的事', '未完事项', '梦境', '生活素材', '兴趣', '责任', '社交', '歇着', '偶发'):
            continue
        bits = [str(card.get(key) or '') for key in ('title', 'detail')]
        merged = ' '.join(b for b in bits if b).strip()
        if merged:
            out.append(merged)
    return out


def reply_targets_turn(text: str, peer_line: str) -> bool:
    """Whether a reply-shaped line can be justified by the visible peer turn."""
    peer = str(peer_line or '').strip()
    if not peer:
        return False
    return bool(reply_without_turn(text)) and ('冷' in peer or '?'.casefold() in peer.casefold()
                                              or '？' in peer or '吗' in peer)
