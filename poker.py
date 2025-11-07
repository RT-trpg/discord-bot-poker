import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from PIL import Image
import io, os, random, asyncio
from itertools import combinations
import logging
import math
from datetime import datetime, timedelta


# ====== 로깅 ======
logging.basicConfig(level=logging.INFO)

# ====== 인텐트 최소 권한 권장 ======
# 슬래시 커맨드만 사용하므로 기본 인텐트로 충분합니다.
intents = discord.Intents.default()
# intents.message_content = True  # <- 슬래시 커맨드만 사용하면 필요 X
# intents.members = True          # <- 현재 코드에서 멤버 목록 조회가 필요 X

bot = commands.Bot(command_prefix="!", intents=intents)

# ====== 봇 준비 이벤트 ======
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")

# 슬래시 명령(app_commands) 사용 시, 시작할 때 동기화해 두면 편해요
@bot.event
async def setup_hook():
    try:
        # DB 스키마 준비
        await init_db()

        # 슬래시 명령 동기화 (이 파일에 정의된 명령들이 등록됨)
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %s", [c.name for c in synced])

    except Exception as e:
        logging.exception("setup_hook failed: %s", e)

# ====== 카드 이미지 경로/크기 ======
CARDS_DIR = os.getenv("CARDS_DIR", "./cards")  # 레포에 cards 폴더를 넣어 배포
 # 예: As.png, 10h.png ...
CARD_W, CARD_H = 67, 92        # 원본
SCALE = 0.9                    # 1/4로 축소 전송
GAP = 6

# ====== 게임 캐시 ======
# players: {uid: {name, coins, bet, contrib, cards, folded, all_in}}
players = {}
game = {
    "deck": [],
    "community": [],
    "pot": 0,
    "round": None,       # "preflop"|"flop"|"turn"|"river"
    "turn_order": [],
    "idx": 0,            # 현재 턴 인덱스
    "current_bet": 0,    # 이번 스트리트 기준 베팅
    "acted": set(),      # 이번 스트리트에서 최소 1회 행동한 uid
    "game_started": False,
    "last_prompt_msg_id": None,
    "channel_id": None,

    # 블라인드/딜러
    "dealer_pos": -1,    # 딜러 버튼(턴오더 인덱스). 매 게임마다 회전
    "sb": 10,            # 스몰블라인드
    "bb": 20,            # 빅블라인드

    # 타이머
    "timer_task": None,    # 카운트다운 업데이트 태스크
    "deadline_ts": None,   # 이 턴 마감(UTC) 유닉스초
}

# ====== DB 초기화 ======
async def init_db():
    async with aiosqlite.connect("test.db") as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS character (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                coin INTEGER DEFAULT 1000,
                in_game INTEGER DEFAULT 0,
                bet INTEGER DEFAULT 0,
                all_in INTEGER DEFAULT 0
            )
        ''')
        
        # [수정] 봇 재시작/크래시 시, DB에 갇힌 유저 상태 초기화
        await db.execute('''
            UPDATE character SET in_game = 0, bet = 0, all_in = 0 WHERE in_game = 1
        ''')
        
        await db.commit()

# ====== 카드 유틸 ======
def create_deck():
    suits = ['s','h','d','c']
    ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    return [f"{r}{s}" for s in suits for r in ranks]

def deal_hole():
    deck = create_deck()
    random.shuffle(deck)
    game["deck"] = deck
    for uid in players:
        players[uid]["cards"] = [deck.pop(), deck.pop()]
        players[uid]["bet"] = 0
        players[uid]["contrib"] = 0
        players[uid]["folded"] = False
        players[uid]["all_in"] = False

def compose(card_codes):
    """['As','Kd',...] → 가로 합성 PNG (1/4 크기) BytesIO 반환"""
    if not card_codes:
        return None
    try:
        w_scaled = max(1, int(CARD_W * SCALE))
        h_scaled = max(1, int(CARD_H * SCALE))
        imgs = []
        for code in card_codes:
            path = os.path.join(CARDS_DIR, f"{code}.png")
            if not os.path.exists(path):
                logging.warning(f"카드 이미지 없음: {path}")
                img = Image.new("RGBA", (w_scaled, h_scaled), (200, 200, 200, 255))  # 임시 회색
            else:
                img = Image.open(path).convert("RGBA").resize((w_scaled, h_scaled), Image.LANCZOS)
            imgs.append(img)
        total_w = w_scaled * len(imgs) + GAP * (len(imgs) - 1)
        if total_w <= 0: total_w = 1 # 0 너비 방지
        canvas = Image.new("RGBA", (total_w, h_scaled), (0,0,0,0))
        x = 0
        for im in imgs:
            canvas.paste(im, (x, 0), im)
            x += w_scaled + GAP
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logging.error(f"이미지 합성 오류: {e}")
        return None

def active_players():
    return [uid for uid, p in players.items() if not p["folded"] and (p["coins"] > 0 or p["all_in"])]

def can_act(uid):
    p = players.get(uid)
    return bool(p) and (not p["folded"]) and (not p["all_in"]) and p["coins"] > 0

def ready_to_advance():
    """모든 유효 플레이어가 이번 스트리트에서 최소 1회 행동했고, bet == current_bet"""
    for uid, p in players.items():
        if p["folded"] or p["all_in"]:
            continue
        if uid not in game["acted"]:
            return False
        if p["bet"] != game["current_bet"]:
            return False
    return True

def next_actor_index(start_from=None):
    i = game["idx"] if start_from is None else start_from
    n = len(game["turn_order"])
    if n == 0: return None
    for k in range(n):
        j = (i + k) % n
        uid = game["turn_order"][j]
        if can_act(uid):
            return j
    return None

# ====== 핸드 평가 ======
RANK_ORDER = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':11,'Q':12,'K':13,'A':14}

def parse_card(code):
    if code.startswith('10'):
        return '10', code[2]
    return code[0], code[1]

def hand_strength(cards7):
    if len(cards7) < 5:
        return (0,)
    best = None
    for combo in combinations(cards7, 5):
        score = score_5cards(combo)
        if (best is None) or (score > best):
            best = score
    return best

def score_5cards(cards5):
    ranks, suits = [], []
    for c in cards5:
        r, s = parse_card(c); ranks.append(r); suits.append(s)
    vals = sorted([RANK_ORDER[r] for r in ranks], reverse=True)
    counts = {v: vals.count(v) for v in set(vals)}
    is_flush = len(set(suits)) == 1

    uniq = sorted(set(vals), reverse=True)
    def straight_high(vs):
        if len(vs) < 5: return None
        # A-5 (마운틴) 엣지 케이스를 먼저 확인
        if {14, 2, 3, 4, 5}.issubset(set(vs)):
             return 5
        # 일반 스트레이트 확인
        for i in range(len(vs)-4):
            window = vs[i:i+5]
            if window == list(range(window[0], window[0]-5, -1)):
                return window[0]
        return None
    
    sh = straight_high(uniq)

    if is_flush and sh:             return (8, sh)       # 스트레이트 플러시
    if 4 in counts.values():       # 포카드
        four = max([v for v,c in counts.items() if c==4])
        kicker = max([v for v in vals if v != four])
        return (7, four, kicker)
    trips = sorted([v for v,c in counts.items() if c==3], reverse=True)
    pairs = sorted([v for v,c in counts.items() if c==2], reverse=True)
    if trips and (pairs or len(trips) >= 2): # 풀하우스
        t = trips[0]; p = pairs[0] if pairs else trips[1]
        return (6, t, p)
    if is_flush:                            return (5, *vals)    # 플러시
    if sh:                                  return (4, sh)       # 스트레이트
    if trips:                               # 트립스
        t = trips[0]; kick = sorted([v for v in vals if v!=t], reverse=True)[:2]
        return (3, t, *kick)
    if len(pairs) >= 2:                     # 투페어
        p1,p2 = pairs[:2]; kicker = max([v for v in vals if v!=p1 and v!=p2])
        return (2, p1, p2, kicker)
    if len(pairs) == 1:                     # 원페어
        p1 = pairs[0]; kick = sorted([v for v in vals if v!=p1], reverse=True)[:3]
        return (1, p1, *kick)
    return (0, *vals)                       # 하이카드

def hand_name(tup):
    names = {8:"스트레이트 플러시",7:"포카드",6:"풀하우스",5:"플러시",4:"스트레이트",3:"트리플",2:"투페어",1:"원페어",0:"하이카드"}
    return names.get(tup[0], "알 수 없음") if tup else "알 수 없음"

# ====== 사이드팟 ======
def build_side_pots(contrib_map):
    levels = sorted(set([v for v in contrib_map.values() if v > 0]))
    if not levels: return []
    pots, prev = [], 0
    for cap in levels:
        members_all = [uid for uid,v in contrib_map.items() if v >= cap]
        amount = (cap - prev) * len(members_all)
        eligible = [uid for uid in members_all if not players[uid]["folded"]]
        pots.append({"cap":cap, "members_all":members_all, "amount":amount, "eligible":eligible})
        prev = cap
    return pots

def split_amount(amount, winners):
    if not winners: return {}
    base = amount // len(winners)
    rem = amount % len(winners)
    dist = {w: base for w in winners}
    order = sorted(winners)  # uid 오름차순으로 자투리 분배
    for i in range(rem):
        dist[order[i]] += 1
    return dist

# ====== 라운드/턴 진행 ======
async def disable_prev_prompt(channel: discord.abc.Messageable):
    """
    이전 턴 프롬프트와 카운트다운을 정리한다.
    """
    # 1) 카운트다운 태스크 정리
    task = game.get("timer_task")
    if task and not task.done():
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.debug(f"timer_task await error: {e}")
    game["timer_task"] = None
    game["deadline_ts"] = None

    # 2) 이전 프롬프트 버튼 비활성화
    msg_id = game.get("last_prompt_msg_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            try:
                await msg.edit(view=None)
            except Exception as e:
                logging.debug(f"msg.edit(view=None) failed: {e}")
        except Exception as e:
            logging.debug(f"fetch_message({msg_id}) failed: {e}")

    # 3) 상태 초기화
    game["last_prompt_msg_id"] = None


async def prompt_action(channel):
    if not game["turn_order"] or game["idx"] >= len(game["turn_order"]):
        logging.error("잘못된 턴 상태"); return
    
    # 다음 행동할 사람 찾기 (현재 idx 포함)
    next_idx = next_actor_index(game["idx"])
    if next_idx is None:
        # 행동할 사람이 아무도 없음 (모두 폴드/올인)
        logging.info("행동할 플레이어 없음, 다음 스트리트로 강제 진행")
        await go_next_street(channel)
        return

    game["idx"] = next_idx # 턴 인덱스 업데이트
    uid = game["turn_order"][game["idx"]]
    
    alive = [u for u in active_players() if not players[u]["folded"]]
    if len(alive) <= 1:
        await handle_single_winner(channel, alive); return

    p = players[uid]; cur_bet = game["current_bet"]
    need_to_call = max(0, cur_bet - p["bet"])

    # 이전 프롬프트/타이머 정리
    await disable_prev_prompt(channel)

    # 120초 마감 타임스탬프 저장
    deadline = datetime.utcnow() + timedelta(seconds=120)
    game["deadline_ts"] = int(deadline.timestamp())

    # 기본 안내문 + 마감 표시
    base_text = (
        f"🎯 **{p['name']}**의 차례!\n"
        f"라운드: **{game['round'] or 'preflop'}** / 팟: **{game['pot']}** / "
        f"콜 필요: **{need_to_call}** / 보유: **{p['coins']}**"
    )

    view = ActionPromptView(actor_id=uid)
    msg = await channel.send(
        base_text + f"\n⏳ 마감: <t:{game['deadline_ts']}:R> (<t:{game['deadline_ts']}:T>)",
        view=view
    )
    game["last_prompt_msg_id"] = msg.id

    # 카운트다운 시작 (진행바/남은 초 갱신)
    game["timer_task"] = asyncio.create_task(_run_countdown(msg, base_text, game["deadline_ts"]))

# ← 여긴 들여쓰기 없이 모듈 전역 (O)
async def advance_or_next_round(channel):
    """
    모든 유효 플레이어가 이번 스트리트에서 행동을 1번 이상 했고,
    모두의 bet가 current_bet에 맞춰졌다면 다음 스트리트로,
    아니면 다음 액터에게 턴을 넘깁니다.
    """
    if ready_to_advance() or next_actor_index() is None:
        await go_next_street(channel)
    else:
        # 다음 턴은 현재 턴 다음 사람부터 찾아야 함
        next_idx = next_actor_index(game["idx"] + 1)
        if next_idx is not None:
            game["idx"] = next_idx
            await prompt_action(channel)
        else:
            # 다음 사람이 없으면 (예: 현재 턴이 마지막이었음)
            # ready_to_advance() 조건이 false인 경우 (예: A가 100벳, B가 200벳)
            # 다시 처음(SB)부터 돌아서 행동해야 함
            first_actor_i = (game["dealer_pos"] + 1) % len(game["turn_order"])
            next_idx_from_start = next_actor_index(first_actor_i)
            if next_idx_from_start is not None and next_idx_from_start != game["idx"]:
                 game["idx"] = next_idx_from_start
                 await prompt_action(channel)
            else:
                 # 그래도 없거나, 현재 턴으로 다시 돌아왔다면 스트리트 종료
                 await go_next_street(channel)


async def end_game():
    # 남아있는 카운트다운 태스크 정리
    task = game.get("timer_task")
    if task and not task.done():
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
    game["timer_task"] = None
    game["deadline_ts"] = None
    
    # 게임 상태 초기화
    global game, players
    players = {}
    game = {
        "deck": [], "community": [], "pot": 0, "round": None,
        "turn_order": [], "idx": 0, "current_bet": 0, "acted": set(),
        "game_started": False, "last_prompt_msg_id": None, "channel_id": None,
        "dealer_pos": game.get("dealer_pos", -1), # 딜러 위치는 유지
        "sb": 10, "bb": 20,
        "timer_task": None, "deadline_ts": None,
    }

async def go_next_street(channel):
    """
    스트리트 종료 → 팟/기여 정산 → 다음 스트리트 공개(플랍/턴/리버) 후 다음 액터에게 턴,
    또는 쇼다운 처리
    """
    # 1) 이번 스트리트 베팅을 팟으로 이동
    for uid, p in players.items():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0
    game["current_bet"] = 0
    game["acted"].clear()

    # 2) 다음 공개/라운드 전개
    current_round = game.get("round", "preflop") # None일 경우 preflop으로 간주
    
    if current_round == "preflop":
        game["round"] = "flop"
        if len(game["deck"]) >= 3:
            game["community"] = [game["deck"].pop(), game["deck"].pop(), game["deck"].pop()]
            await channel.send("🔥 **플랍 공개!**")
        else:
            logging.error("덱 카드 부족"); await end_game(); return
        # 포스트플랍 선행: 딜러 다음
        n = len(game["turn_order"])
        if n > 0:
            first_postflop_i = (game["dealer_pos"] + 1) % n
            maybe = next_actor_index(first_postflop_i)
            if maybe is not None:
                game["idx"] = maybe

    elif current_round == "flop":
        game["round"] = "turn"
        if len(game["deck"]) >= 1:
            game["community"].append(game["deck"].pop())
            await channel.send("🌪️ **턴 공개!**")
        else:
            logging.error("덱 카드 부족"); await end_game(); return
        n = len(game["turn_order"])
        if n > 0:
            first_postflop_i = (game["dealer_pos"] + 1) % n
            maybe = next_actor_index(first_postflop_i)
            if maybe is not None:
                game["idx"] = maybe

    elif current_round == "turn":
        game["round"] = "river"
        if len(game["deck"]) >= 1:
            game["community"].append(game["deck"].pop())
            await channel.send("🌊 **리버 공개!**")
        else:
            logging.error("덱 카드 부족"); await end_game(); return
        n = len(game["turn_order"])
        if n > 0:
            first_postflop_i = (game["dealer_pos"] + 1) % n
            maybe = next_actor_index(first_postflop_i)
            if maybe is not None:
                game["idx"] = maybe

    else: # river
        # 쇼다운
        await resolve_showdown(channel)
        return

    # 3) 보드 이미지 표시
    buf = compose(game["community"])
    if buf:
        await channel.send(file=discord.File(buf, filename=f"board_{game['round']}.png"))

    # 4) 다음 액터 프롬프트 (행동 가능한 사람이 2명 이상인지 확인)
    remaining_to_act = [uid for uid in game["turn_order"] if can_act(uid)]
    if len(remaining_to_act) < 2 and game["round"] != "river":
         # 행동할 사람이 1명 이하거나, 모두 올인 상태면
         # 다음 스트리트로 바로 진행 (베팅 라운드 스킵)
         await channel.send("남은 플레이어가 1명 이하이거나 모두 올인 상태입니다. 다음 카드를 즉시 공개합니다.")
         await asyncio.sleep(1) # 잠시 대기
         await go_next_street(channel)
    else:
        # 정상적으로 다음 턴 진행
        await prompt_action(channel)


async def handle_single_winner(channel, alive):
    for p in players.values():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0
    if alive:
        winner = alive[0]
        players[winner]["coins"] += game["pot"]
        async with aiosqlite.connect("test.db") as db:
            await db.execute("UPDATE character SET coin=?, in_game=0 WHERE user_id=?", (players[winner]["coins"], winner))
            await db.commit()
        await channel.send(f"🏆 **{players[winner]['name']}** 단독 승리! 팟 {game['pot']} 코인 획득")
    else:
        await channel.send("모두 폴드하여 팟이 증발했습니다...") # 이 경우는 거의 없어야 함
        
    # DB에 다른 플레이어들 in_game=0 처리
    async with aiosqlite.connect("test.db") as db:
        for uid in players:
             if not alive or uid != alive[0]:
                 await db.execute("UPDATE character SET in_game=0 WHERE user_id=?", (uid,))
        await db.commit()

    await end_game()

# ====== 쇼다운/정산 ======
async def resolve_showdown(channel):
    # 마지막 베팅 이동
    for uid, p in players.items():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0

    remaining = [uid for uid, p in players.items() if not p["folded"]]
    if len(remaining) <= 1:
        await handle_single_winner(channel, remaining)
        return

    contrib = {uid: players[uid].get("contrib", 0) for uid in players}
    pots = build_side_pots(contrib)

    board = game["community"]
    winnings = {uid: 0 for uid in players}
    strength_cache = {}
    for uid, p in players.items():
        if p["folded"]: continue
        strength_cache[uid] = hand_strength(p["cards"] + board)

    if board:
        buf = compose(board)
        if buf: await channel.send("🃏 **최종 보드:**", file=discord.File(buf, filename="final_board.png"))

    desc_lines = []
    # 핸드 공개 순서 정렬 (나중에 베팅한 사람부터, 또는 딜러 왼쪽부터)
    # 여기서는 간단히 uid 순서로...
    sorted_showdown = sorted(strength_cache.keys(), key=lambda u: players[u]["name"])

    for uid in sorted_showdown:
        st = strength_cache[uid]
        desc_lines.append(f"**{players[uid]['name']}**: {hand_name(st)}")
        buf = compose(players[uid]["cards"])
        if buf:
            await channel.send(f"{players[uid]['name']}의 핸드: `{players[uid]['cards'][0]}`, `{players[uid]['cards'][1]}`", file=discord.File(buf, filename=f"hand_{players[uid]['name']}.png"))
    
    if desc_lines:
        await channel.send("🎯 **쇼다운 요약:**\n" + "\n".join(desc_lines))

    for i, pot in enumerate(pots, 1):
        amount = pot["amount"]; eligible = pot["eligible"]
        if not eligible or amount <= 0: continue
        best, winners = None, []
        for uid in eligible:
            st = strength_cache.get(uid)
            if st is None: continue
            if (best is None) or (st > best):
                best = st; winners = [uid]
            elif st == best:
                winners.append(uid)
        dist = split_amount(amount, winners)
        for uid, val in dist.items():
            winnings[uid] += val
        
        winner_names = [players[u]['name'] for u in winners]
        if winners:
            await channel.send(f"🫙 **{'메인팟' if i == 1 else f'사이드팟 #{i}'}** (총 {amount}) → 승자: {', '.join(winner_names)} ({hand_name(best)})")
        else:
            await channel.send(f"🫙 **{'메인팟' if i == 1 else f'사이드팟 #{i}'}** (총 {amount}) → 승자 없음 (해당 팟에 폴드하지 않은 유저가 없음)")


    total_distributed = 0
    result_lines = []
    for uid, p in players.items():
        won = winnings.get(uid, 0)
        p["coins"] += won
        total_distributed += won
        if won > 0:
            result_lines.append(f"**{p['name']}**: +{won} 코인 (현재: {p['coins']})")
        
    await channel.send(f"💰 **총 {total_distributed} 코인 분배 완료!**\n" + "\n".join(result_lines))

    async with aiosqlite.connect("test.db") as db:
        for uid, p in players.items():
            await db.execute("UPDATE character SET coin=?, in_game=0 WHERE user_id=?", (p["coins"], uid))
        await db.commit()

    await end_game()

# ====== UI ======
class RaiseModal(discord.ui.Modal, title="레이즈 금액 입력"):
    def __init__(self, actor_id: int):
        super().__init__()
        self.actor_id = actor_id
        p = players.get(actor_id)
        cur_bet = game.get("current_bet", 0)
        min_raise = game.get("bb", 20) # 최소 레이즈는 BB
        call_need = max(0, cur_bet - p.get("bet", 0))
        
        placeholder = f"최소 {min_raise} 이상 입력 (콜 {call_need} + {min_raise})"
        if p and p["coins"] <= call_need + min_raise:
             placeholder = f"올인만 가능 (최대 {p['coins']})"

        self.amount = discord.ui.TextInput(label=f"레이즈 금액 (현재 베팅: {cur_bet})", placeholder=placeholder, required=True, max_length=10)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # [수정] self.amount -> self.amount.value 로 변경
            val_str = str(self.amount.value).strip()
            if not val_str:
                 raise ValueError("입력값이 없습니다.")
            val = int(val_str)
            if val <= 0: raise ValueError("0보다 커야 합니다.")
        except Exception as e:
            logging.debug(f"레이즈 금액 오류: {e}")
            await interaction.response.send_message("1 이상의 정수를 입력해 주세요!", ephemeral=True); return
        await handle_raise(interaction, self.actor_id, val)

class ActionPromptView(discord.ui.View):
    """공개 '행동하기' 버튼 → 현재 차례인 유저만 누를 수 있음(검증 후 에페메럴 버튼 제공)"""
    def __init__(self, actor_id: int, timeout=120): # 120초 타임아웃
        super().__init__(timeout=timeout); self.actor_id = actor_id
    
    async def on_timeout(self):
        """
        뷰 자체가 타임아웃 (플레이어가 '행동하기' 버튼조차 누르지 않음)
        """
        logging.info(f"ActionPromptView timed out for {self.actor_id}")
        # 타임아웃 시 자동으로 폴드 처리
        await handle_afk_fold(self.actor_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("아직 네 차례가 아니야!", ephemeral=True); return False
        if not game["game_started"]:
            await interaction.response.send_message("게임이 시작되지 않았어요!", ephemeral=True); return False
        if game["idx"] >= len(game["turn_order"]) or game["turn_order"][game["idx"]] != self.actor_id:
            await interaction.response.send_message("이미 턴이 지나갔어요!", ephemeral=True); return False
        return True
    
    @discord.ui.button(label="🎰 행동하기", style=discord.ButtonStyle.primary)
    async def _open_actions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("액션을 선택하세요:", view=ActionView(self.actor_id), ephemeral=True)

class ActionView(discord.ui.View):
    """에페메럴: 체크/콜/레이즈/폴드"""
    def __init__(self, actor_id: int, timeout=120): # 120초 타임아웃
        super().__init__(timeout=timeout); self.actor_id = actor_id
        
        # 버튼 활성화/비활성화 로직
        p = players.get(actor_id)
        can_check = False
        if p:
            need = game["current_bet"] - p["bet"]
            if need == 0:
                can_check = True

        # 체크가 불가능하면(콜해야 하면) 체크 버튼 비활성화
        self._check.disabled = not can_check
        # 콜이 0이면 (체크 상황) 콜 버튼 비활성화
        self._call.disabled = can_check

    async def on_timeout(self):
        """
        에페메럴 뷰 타임아웃 ('행동하기'는 눌렀으나 최종 선택을 안 함)
        """
        logging.info(f"ActionView timed out for {self.actor_id}")
        # 타임아웃 시 자동으로 폴드 처리
        await handle_afk_fold(self.actor_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return game["game_started"] and game["idx"] < len(game["turn_order"]) and \
            interaction.user.id == self.actor_id and game["turn_order"][game["idx"]] == self.actor_id
    
    @discord.ui.button(label="체크", style=discord.ButtonStyle.secondary)
    async def _check(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_check(interaction, self.actor_id)
    
    @discord.ui.button(label="콜", style=discord.ButtonStyle.primary)
    async def _call(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_call(interaction, self.actor_id)
    
    @discord.ui.button(label="레이즈", style=discord.ButtonStyle.success)
    async def _raise(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaiseModal(self.actor_id))
    
    @discord.ui.button(label="폴드", style=discord.ButtonStyle.danger)
    async def _fold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_fold(interaction, self.actor_id)

class MultiPeekCardsView(discord.ui.View):
    """참가자 전원의 '내 카드 보기' 버튼을 한 메시지에 가로로 배치 (본인만 클릭 가능)"""
    def __init__(self, uid_name_pairs, timeout=300):
        super().__init__(timeout=timeout)
        for i, (uid, name) in enumerate(uid_name_pairs):
            row_index = i // 5  # 한 줄 최대 5개 버튼
            btn = discord.ui.Button(
                label=name, style=discord.ButtonStyle.secondary,
                row=row_index, custom_id=f"peek_{uid}"
            )
            
            async def callback(interaction: discord.Interaction, target_uid=uid):
                if interaction.user.id != target_uid:
                    await interaction.response.send_message("이 버튼은 해당 플레이어만 사용할 수 있어요!", ephemeral=True); return
                
                # players 딕셔너리가 비어있지 않고, 해당 유저가 존재하는지 확인
                p = players.get(target_uid)
                if not p:
                    await interaction.response.send_message("게임이 시작되지 않았거나 참가자가 아닙니다!", ephemeral=True); return

                cards = p.get("cards")
                if not cards:
                    await interaction.response.send_message("아직 카드가 배분되지 않았어요!", ephemeral=True); return
                
                buf = compose(cards)
                if buf:
                    await interaction.response.send_message(
                        "🎴 당신의 핸드:", file=discord.File(buf, filename="my_cards.png"), ephemeral=True
                    )
                else:
                    await interaction.response.send_message("카드 이미지를 생성할 수 없습니다.", ephemeral=True)
            
            btn.callback = callback
            self.add_item(btn)

# ====== 액션 처리 ======
async def handle_check(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    need = game["current_bet"] - p["bet"]
    if need > 0:
        await inter.response.send_message(f"체크 불가! {need} 코인 콜 필요", ephemeral=True); return
    await inter.response.edit_message(content="✅ 체크!", view=None) # Ephemeral 응답 수정
    game["acted"].add(uid)
    await advance_or_next_round(inter.channel)

async def handle_call(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    need = max(0, game["current_bet"] - p["bet"])
    if need == 0:
        await inter.response.edit_message(content="✅ 체크! (콜 필요 없음)", view=None); return # 콜 버튼 눌렀지만 체크인 상황
    pay = min(need, p["coins"])
    p["coins"] -= pay; p["bet"] += pay
    if p["coins"] == 0:
        p["all_in"] = True; await inter.response.edit_message(content=f"🔥 올인! {pay} 코인", view=None)
    else:
        await inter.response.edit_message(content=f"📞 콜 {pay} 코인", view=None)
    game["acted"].add(uid)
    await advance_or_next_round(inter.channel)

async def handle_raise(inter: discord.Interaction, uid: int, raise_amt: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    
    need_to_call = max(0, game["current_bet"] - p["bet"])
    min_raise = game.get("bb", 20) # 최소 레이즈폭은 BB
    
    # 1. 입력한 raise_amt가 최소 레이즈폭보다 작은지?
    if raise_amt < min_raise:
        # 단, 올인인 경우는 예외
        if p["coins"] == need_to_call + raise_amt:
             pass # 올인 레이즈는 금액 미달이어도 허용
        else:
             await inter.response.send_message(f"최소 레이즈 금액은 {min_raise} (BB) 입니다!", ephemeral=True); return

    # 2. 총 내야 할 돈 (콜 + 레이즈)
    total_need = need_to_call + raise_amt

    # 3. 가진 돈보다 많이 낼 순 없음 (올인 처리)
    if total_need > p["coins"]:
        total_need = p["coins"]
        raise_amt = total_need - need_to_call # 실제 레이즈 금액 조정

    if total_need <= need_to_call: # 올인했는데 콜 금액보다 적거나 같은 경우
        # 이것은 사실상 콜임
        await handle_call(inter, uid)
        return

    # 4. 베팅 처리
    p["coins"] -= total_need; p["bet"] += total_need
    game["current_bet"] = max(game["current_bet"], p["bet"]) # 현재 베팅 갱신
    
    if p["coins"] == 0:
        p["all_in"] = True; await inter.response.edit_message(content=f"🔥 올인 레이즈! {total_need} 코인 (총 베팅: {game['current_bet']})", view=None)
    else:
        await inter.response.edit_message(content=f"📈 레이즈 {raise_amt} 코인 (총 베팅: {game['current_bet']})", view=None)
    
    game["acted"] = {uid}  # 레이즈했으므로, 이 사람 빼고 모두 다시 행동해야 함
    
    # 다음 턴으로
    await advance_or_next_round(inter.channel)


async def handle_fold(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    p["folded"] = True
    await inter.response.edit_message(content="🚫 폴드!", view=None)
    game["acted"].add(uid)
    await advance_or_next_round(inter.channel)

async def handle_afk_fold(uid: int):
    """
    턴 타임아웃으로 인한 자동 폴드 처리
    뷰의 on_timeout에서 호출됨 (interaction 객체가 없음)
    """
    # 1. 게임/채널 상태 확인
    if not game["game_started"] or not game["channel_id"]:
        return # 게임이 이미 끝났거나 채널 정보가 없음
    channel = bot.get_channel(game["channel_id"])
    if not channel:
        logging.error(f"AFK: 채널 ID {game['channel_id']}를 찾을 수 없음")
        return

    # 2. 현재 턴이 타임아웃된 유저가 맞는지 확인 (중요: 레이스 컨디션 방지)
    if (not game["turn_order"] or 
        game["idx"] >= len(game["turn_order"]) or 
        game["turn_order"][game["idx"]] != uid):
        # 타임아웃이 발생했지만, 그 직전에 유저가 행동했거나 턴이 이미 넘어간 경우
        logging.info(f"AFK: 턴이 이미 {uid}가 아님, 무시")
        return

    # 3. 플레이어 정보 확인
    p = players.get(uid)
    if not p or p["folded"] or p["all_in"]:
        # 플레이어가 없거나, 이미 폴드/올인 상태면 처리할 필요 없음
        return

    # 4. 강제 폴드 처리
    logging.info(f"AFK: {p['name']} ({uid}) 자동 폴드 처리")
    p["folded"] = True
    game["acted"].add(uid) 
    await channel.send(f"⏰ **{p['name']}**님의 턴 시간이 초과되어 자동으로 **폴드**합니다.")
    
    # 5. 이전 프롬프트 정리 (중요)
    await disable_prev_prompt(channel)

    # 6. 다음 턴으로 진행
    await advance_or_next_round(channel)


# ====== 슬래시 커맨드 ======
@bot.tree.command(name="등록", description="캐릭터 등록 (1000 코인 시작)")
@app_commands.describe(이름="사용할 캐릭터 이름")
async def 등록(inter: discord.Interaction, 이름: str):
    if len(이름) > 20:
        await inter.response.send_message("이름은 20자 이하로 입력해 주세요!", ephemeral=True); return
    uid = inter.user.id
    async with aiosqlite.connect("test.db") as db:
        cur = await db.execute("SELECT name FROM character WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row:
            await inter.response.send_message(f"이미 '{row[0]}'로 등록되어 있어요!", ephemeral=True); return
        await db.execute("INSERT INTO character (user_id,name,coin,in_game,bet,all_in) VALUES (?,?,?,?,?,?)",
                         (uid, 이름, 1000, 0, 0, 0))
        await db.commit()
    await inter.response.send_message(f"🎉 '{이름}' 등록 완료! 시작 코인 1000", ephemeral=True)

@bot.tree.command(name="조회", description="내 캐릭터 정보 조회")
async def 조회(inter: discord.Interaction):
    uid = inter.user.id
    async with aiosqlite.connect("test.db") as db:
        cur = await db.execute("SELECT name, coin FROM character WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        await inter.response.send_message("먼저 `/등록`으로 캐릭터를 만들어줘!", ephemeral=True); return
    name, coin = row
    status = "게임 참가 중" if uid in players else "대기 중"
    embed = discord.Embed(title="🎮 캐릭터 정보", color=0x00ff00)
    embed.add_field(name="이름", value=name, inline=True)
    embed.add_field(name="코인", value=f"{coin:,}개", inline=True)
    embed.add_field(name="상태", value=status, inline=True)
    await inter.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="참가", description="현재 게임에 참가")
async def 참가(inter: discord.Interaction):
    if game["game_started"]:
        await inter.response.send_message("이미 게임이 시작되었어요! 다음 게임에 합류해줘요.", ephemeral=True); return
    uid = inter.user.id
    if uid in players:
        await inter.response.send_message("이미 참가 중이에요!", ephemeral=True); return
    
    async with aiosqlite.connect("test.db") as db:
        # DB에서 in_game 플래그 확인 (다른 봇 인스턴스나 크래시 대비)
        cur_db = await db.execute("SELECT name, coin, in_game FROM character WHERE user_id=?", (uid,))
        row_db = await cur_db.fetchone()
        if not row_db:
            await inter.response.send_message("먼저 `/등록`으로 캐릭터 생성!", ephemeral=True); return
        
        name, coin, in_game_db = row_db
        
        if in_game_db == 1:
             logging.warning(f"{name}({uid})가 DB상 in_game=1이지만, 로컬 캐시(players)에 없어 강제 참가 처리")
             # DB 상태를 0으로 리셋하고 참가를 허용
             await db.execute("UPDATE character SET in_game=0 WHERE user_id=?", (uid,))
             await db.commit()

        if coin <= 0:
            await inter.response.send_message("코인이 0이라 참가 불가!", ephemeral=True); return
        
        players[uid] = {"name": name, "coins": coin, "bet": 0, "contrib": 0, "cards": [], "folded": False, "all_in": False}
        
        await db.execute("UPDATE character SET in_game=1 WHERE user_id=?", (uid,))
        await db.commit()
        
    await inter.response.send_message(f"✅ 참가! 현재 인원 {len(players)}명")

@bot.tree.command(name="퇴장", description="현재 게임에서 퇴장")
async def 퇴장(inter: discord.Interaction):
    uid = inter.user.id
    if uid not in players:
        await inter.response.send_message("현재 게임에 참가하지 않았어요.", ephemeral=True); return
    if game["game_started"]:
        await inter.response.send_message("게임 진행 중에는 퇴장할 수 없어요!", ephemeral=True); return
    
    p = players.pop(uid)
    name = p["name"]; coin = p["coins"]
    
    async with aiosqlite.connect("test.db") as db:
        await db.execute("UPDATE character SET in_game=0, coin=? WHERE user_id=?", (coin, uid))
        await db.commit()
    await inter.response.send_message(f"🚪 {name} 퇴장 완료")

@bot.tree.command(name="시작", description="텍사스 홀덤 게임 시작")
async def 시작(inter: discord.Interaction):
    if game["game_started"]:
        await inter.response.send_message("이미 게임이 진행 중이에요!", ephemeral=True); return
    if len(players) < 2:
        await inter.response.send_message("최소 2명이 필요해요!", ephemeral=True); return
    if len(players) > 10:
        await inter.response.send_message("최대 10명까지 가능해요!", ephemeral=True); return

    game.update({
        "deck": [], "community": [], "pot": 0, "round": "preflop",
        "turn_order": list(players.keys()), "idx": 0,
        "current_bet
