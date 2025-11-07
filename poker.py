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
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== 봇 준비 이벤트 ======
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")

@bot.event
async def setup_hook():
    try:
        await init_db()
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %s", [c.name for c in synced])
    except Exception as e:
        logging.exception("setup_hook failed: %s", e)

# ====== 카드 이미지 경로/크기 ======
CARDS_DIR = os.getenv("CARDS_DIR", "./cards")
CARD_W, CARD_H = 67, 92
SCALE = 0.9
GAP = 6

# ====== 게임 캐시 ======
# players: {uid: {name, coins, bet, contrib, cards, folded, all_in, afk_kicked}}
players = {}
game = {
    "deck": [],
    "community": [],
    "pot": 0,
    "round": None,
    "turn_order": [],
    "idx": 0,
    "current_bet": 0,
    "acted": set(),
    "game_started": False,
    "last_prompt_msg_id": None,
    "channel_id": None,
    "dealer_pos": -1,
    "sb": 10,
    "bb": 20,
    "timer_task": None,
    "deadline_ts": None,
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
        # 자동 참가를 위해 봇 재시작 시 DB를 초기화하지 않음
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
        # 게임 시작 시 플레이어 상태 초기화
        players[uid]["cards"] = [deck.pop(), deck.pop()]
        players[uid]["bet"] = 0
        players[uid]["contrib"] = 0
        players[uid]["folded"] = False
        players[uid]["all_in"] = False
        # [수정] AFK 퇴장 플래그 초기화 (게임이 시작되어야 초기화됨)
        players[uid]["afk_kicked"] = False

def compose(card_codes):
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
                img = Image.new("RGBA", (w_scaled, h_scaled), (200, 200, 200, 255))
            else:
                img = Image.open(path).convert("RGBA").resize((w_scaled, h_scaled), Image.LANCZOS)
            imgs.append(img)
        total_w = w_scaled * len(imgs) + GAP * (len(imgs) - 1)
        if total_w <= 0: total_w = 1
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
    """폴드/파산(올인 제외)하지 않은 플레이어"""
    return [uid for uid, p in players.items() if not p["folded"] and (p["coins"] > 0 or p["all_in"])]

def can_act(uid):
    """현재 턴에 행동(체크/콜/레이즈/폴드)이 가능한 플레이어"""
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
    """start_from (포함) 부터 시작해서, 행동 가능한 다음 플레이어의 인덱스를 반환"""
    i = game["idx"] if start_from is None else start_from
    n = len(game["turn_order"])
    if n == 0: return None
    for k in range(n):
        j = (i + k) % n
        uid = game["turn_order"][j]
        if can_act(uid):
            return j
    return None # 행동 가능한 플레이어 없음

# ====== 핸드 평가 (생략) ======
RANK_ORDER = {'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':11,'Q':12,'K':13,'A':14}
def parse_card(code):
    if code.startswith('10'): return '10', code[2]
    return code[0], code[1]
def hand_strength(cards7):
    if len(cards7) < 5: return (0,)
    best = None
    for combo in combinations(cards7, 5):
        score = score_5cards(combo)
        if (best is None) or (score > best): best = score
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
        if {14, 2, 3, 4, 5}.issubset(set(vs)): return 5 # A-5 마운틴
        for i in range(len(vs)-4):
            window = vs[i:i+5]
            if window == list(range(window[0], window[0]-5, -1)): return window[0]
        return None
    sh = straight_high(uniq)
    if is_flush and sh:             return (8, sh)
    if 4 in counts.values():
        four = max([v for v,c in counts.items() if c==4])
        kicker = max([v for v in vals if v != four])
        return (7, four, kicker)
    trips = sorted([v for v,c in counts.items() if c==3], reverse=True)
    pairs = sorted([v for v,c in counts.items() if c==2], reverse=True)
    if trips and (pairs or len(trips) >= 2):
        t = trips[0]; p = pairs[0] if pairs else trips[1]
        return (6, t, p)
    if is_flush:                            return (5, *vals)
    if sh:                                  return (4, sh)
    if trips:
        t = trips[0]; kick = sorted([v for v in vals if v!=t], reverse=True)[:2]
        return (3, t, *kick)
    if len(pairs) >= 2:
        p1,p2 = pairs[:2]; kicker = max([v for v in vals if v!=p1 and v!=p2])
        return (2, p1, p2, kicker)
    if len(pairs) == 1:
        p1 = pairs[0]; kick = sorted([v for v in vals if v!=p1], reverse=True)[:3]
        return (1, p1, *kick)
    return (0, *vals)
def hand_name(tup):
    names = {8:"스트레이트 플러시",7:"포카드",6:"풀하우스",5:"플러시",4:"스트레이트",3:"트리플",2:"투페어",1:"원페어",0:"하이카드"}
    return names.get(tup[0], "알 수 없음") if tup else "알 수 없음"

# ====== 사이드팟 (생략) ======
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
    order = sorted(winners)
    for i in range(rem):
        dist[order[i]] += 1
    return dist

# ====== 라운드/턴 진행 ======
async def disable_prev_prompt(channel: discord.abc.Messageable):
    task = game.get("timer_task")
    if task and not task.done():
        try:
            task.cancel()
            await task
        except asyncio.CancelledError: pass
        except Exception as e: logging.debug(f"timer_task await error: {e}")
    game["timer_task"] = None
    game["deadline_ts"] = None
    msg_id = game.get("last_prompt_msg_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(view=None)
        except Exception as e:
            logging.debug(f"disable_prev_prompt failed: {e}")
    game["last_prompt_msg_id"] = None

async def prompt_action(channel):
    if not game["turn_order"] or game["idx"] >= len(game["turn_order"]):
        logging.error("잘못된 턴 상태 (prompt_action)"); return
    
    # [수정] 현재 인덱스(game["idx"])부터 행동 가능한 사람을 찾음
    next_idx = next_actor_index(game["idx"])
    if next_idx is None:
        # 행동할 플레이어가 아무도 없음 (예: 모두 올인/폴드)
        logging.info("행동할 플레이어 없음, 다음 스트리트로 강제 진행")
        await go_next_street(channel)
        return

    game["idx"] = next_idx # 실제 턴 인덱스 업데이트
    uid = game["turn_order"][game["idx"]]
    
    # [버그 수정] 이 로직이 120초 타이머가 시작되는 것을 막아줌
    alive = [u for u in active_players() if not players[u]["folded"]]
    if len(alive) <= 1:
        await handle_single_winner(channel, alive); return

    p = players[uid]; cur_bet = game["current_bet"]
    need_to_call = max(0, cur_bet - p["bet"])

    await disable_prev_prompt(channel)

    # 턴이 돌아올 때마다 120초 타이머 리셋
    deadline = datetime.utcnow() + timedelta(seconds=120)
    game["deadline_ts"] = int(deadline.timestamp()) # [버그 수정] 턴마다 고유한 마감 시간 생성

    base_text = (
        f"🎯 **{p['name']}**의 차례!\n"
        f"라운드: **{game['round'] or 'preflop'}** / 팟: **{game['pot']}** / "
        f"콜 필요: **{need_to_call}** / 보유: **{p['coins']}**"
    )
    # [버그 수정] 고유한 마감 시간을 뷰에도 전달
    view = ActionPromptView(actor_id=uid, deadline_ts=game["deadline_ts"])
    msg = await channel.send(
        base_text + f"\n⏳ 마감: <t:{game['deadline_ts']}:R> (<t:{game['deadline_ts']}:T>)",
        view=view
    )
    game["last_prompt_msg_id"] = msg.id
    # 타이머 갱신 작업 시작
    game["timer_task"] = asyncio.create_task(_run_countdown(msg, base_text, game["deadline_ts"]))

async def advance_or_next_round(channel):
    """
    행동이 완료되었는지(ready_to_advance) 체크하고,
    완료 시 -> go_next_street()
    미완료 시 -> 다음 턴(prompt_action)
    """
    
    # [버그 수정] advance_or_next_round가 호출되기 직전에 이미 승자가 결정됐는지 확인
    alive = [u for u in active_players() if not players[u]["folded"]]
    if len(alive) <= 1:
        await handle_single_winner(channel, alive); return

    if ready_to_advance() or next_actor_index() is None:
        await go_next_street(channel)
    else:
        # 다음 턴 (현재 턴 + 1)
        next_idx = next_actor_index(game["idx"] + 1)
        if next_idx is not None:
            game["idx"] = next_idx
            await prompt_action(channel)
        else:
            # 다음 사람이 없으면 (예: 현재 턴이 마지막이었음)
            # 하지만 ready_to_advance()가 False (예: A 100벳, B 200벳)
            # -> 다시 처음(SB)부터 돌아서 행동해야 함
            first_actor_i = (game["dealer_pos"] + 1) % len(game["turn_order"])
            next_idx_from_start = next_actor_index(first_actor_i)
            
            if next_idx_from_start is not None:
                 game["idx"] = next_idx_from_start
                 await prompt_action(channel)
            else:
                 # 그래도 없으면 (모두 행동했는데 ready_to_advance가 False? -> 오류 상황이거나, 모두 올인/폴드)
                 await go_next_street(channel)

# end_game 함수: 플레이어를 유지하고 상태만 초기화
async def end_game():
    global game, players # 'players' 딕셔너리를 유지합니다.

    # 1. 타이머 정리
    task = game.get("timer_task")
    if task and not task.done():
        try:
            task.cancel()
            await task
        except asyncio.CancelledError: pass
    game["timer_task"] = None
    game["deadline_ts"] = None

    # 2. 다음 게임에서 제외할 플레이어 확인 (AFK 또는 파산)
    channel = bot.get_channel(game["channel_id"])
    if not channel:
        logging.error("end_game: Channel not found, cannot send messages.")

    uids_to_remove = []
    uids_to_keep = []
    
    # .items() 대신 list(players)로 순회 (딕셔너리 변경 중 에러 방지)
    for uid in list(players.keys()):
        p = players[uid]
        if p.get("afk_kicked", False):
            uids_to_remove.append((uid, "AFK(시간 초과)로 인해 퇴장합니다."))
        elif p["coins"] <= 0:
            uids_to_remove.append((uid, "코인을 모두 잃어 퇴장합니다. (파산)"))
        else:
            uids_to_keep.append(uid)

    # 3. DB 업데이트 및 로컬 캐시(players) 정리
    async with aiosqlite.connect("test.db") as db:
        for uid, reason in uids_to_remove:
            if channel:
                # 플레이어 객체가 아직 남아있을 때 메시지 전송
                if uid in players:
                    await channel.send(f"🚪 **{players[uid]['name']}**님: {reason}")
            # DB: in_game=0 (퇴장), 코인 저장
            await db.execute("UPDATE character SET in_game=0, coin=? WHERE user_id=?", (players[uid]['coins'], uid))
            if uid in players:
                players.pop(uid) # 로컬 캐시에서 제거
        
        for uid in uids_to_keep:
            # DB: in_game=1 (유지), 코인 저장
            await db.execute("UPDATE character SET in_game=1, coin=? WHERE user_id=?", (players[uid]['coins'], uid))
            # [추가] 로비에 남는 유저의 AFK 플래그를 즉시 초기화
            if uid in players:
                players[uid]["afk_kicked"] = False

    # 4. 'game' 상태만 초기화 ('players'는 유지)
    game = {
        "deck": [], "community": [], "pot": 0, "round": None,
        "turn_order": [], "idx": 0, "current_bet": 0, "acted": set(),
        "game_started": False, # <-- 게임 종료 상태
        "last_prompt_msg_id": None, "channel_id": game.get("channel_id"), # 채널 ID 유지
        "dealer_pos": game.get("dealer_pos", -1), # 딜러 위치 유지
        "sb": 10, "bb": 20,
        "timer_task": None, "deadline_ts": None,
    }

    # 5. 다음 게임 로비 안내
    if channel:
        if players: # 남아있는 플레이어가 있다면
            names = ", ".join([p['name'] for p in players.values()])
            await channel.send(
                f"✅ 게임 종료! 다음 게임을 준비합니다.\n"
                f"현재 참가자 ({len(players)}명): {names}\n\n"
                f"`/시작`을 눌러 다음 게임을 시작하세요!\n"
                f"(새로운 참가자는 `/참가`, 나가시려면 `/퇴장`)"
            )
        else:
            await channel.send("✅ 게임 종료! 모든 플레이어가 퇴장했습니다.")

async def go_next_street(channel):
    # 1) 이번 스트리트 베팅을 팟으로 이동
    for uid, p in players.items():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0
    game["current_bet"] = 0
    game["acted"].clear()

    current_round = game.get("round", "preflop")
    
    if current_round == "preflop":
        game["round"] = "flop"
        if len(game["deck"]) >= 3:
            game["community"] = [game["deck"].pop(), game["deck"].pop(), game["deck"].pop()]
            await channel.send("🔥 **플랍 공개!**")
        else:
            logging.error("덱 카드 부족"); await end_game(); return
        n = len(game["turn_order"])
        if n > 0:
            first_postflop_i = (game["dealer_pos"] + 1) % n
            maybe = next_actor_index(first_postflop_i)
            if maybe is not None: game["idx"] = maybe

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
            if maybe is not None: game["idx"] = maybe

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
            if maybe is not None: game["idx"] = maybe
    else: # river
        await resolve_showdown(channel)
        return

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
        # 정상적으로 다음 턴 진행 (단, 행동할 사람이 1명이라도 있어야 함)
        if remaining_to_act:
            await prompt_action(channel)
        else:
            # 행동할 사람이 아무도 없으면 (모두 올인/폴드) 다음 스트리트
            await go_next_street(channel)


# [수정] 단독 승리 시 핸드 공개/래빗 헌팅 로직 추가
async def handle_single_winner(channel, alive):
    # 1. 팟 정산
    for p in players.values():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0
    
    current_pot = game["pot"]

    # 2. 승자가 없는 경우 (모두 폴드?)
    if not alive:
        await channel.send("모두 폴드하여 팟이 증발했습니다...")
        await end_game() # 게임 종료
        return
    
    # 3. 승자가 있는 경우 (10초 뷰 표시)
    winner_uid = alive[0]
    p = players.get(winner_uid)
    if not p:
        logging.error(f"handle_single_winner: 승리자 {winner_uid} 정보를 찾을 수 없음")
        await end_game()
        return
        
    winner_name = p["name"]
    
    # [수정] WinnerOptionsView (래빗 헌팅 포함)
    view = WinnerOptionsView(winner_uid=winner_uid, winner_name=winner_name, pot=current_pot)
    await channel.send(
        f"🏆 **{winner_name}** 단독 승리! 래빗 헌팅 또는 핸드 공개를 선택하세요. (10초)",
        view=view
    )
    
    # [중요] 팟 지급 및 end_game() 호출은 WinnerOptionsView의 콜백/타임아웃으로 이동됨


# ====== 쇼다운/정산 ======
async def resolve_showdown(channel):
    # 1. 마지막 베팅 이동
    for uid, p in players.items():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0

    # 2. 쇼다운 대상자 확인
    remaining = [uid for uid, p in players.items() if not p["folded"]]
    if len(remaining) <= 1:
        await handle_single_winner(channel, remaining)
        return

    # 3. 사이드팟 빌드
    contrib = {uid: players[uid].get("contrib", 0) for uid in players}
    pots = build_side_pots(contrib)

    # 4. 핸드 평가
    board = game["community"]
    winnings = {uid: 0 for uid in players}
    strength_cache = {}
    for uid, p in players.items():
        if p["folded"]: continue
        strength_cache[uid] = hand_strength(p["cards"] + board)

    if board:
        buf = compose(board)
        if buf: await channel.send("🃏 **최종 보드:**", file=discord.File(buf, filename="final_board.png"))

    # 5. 핸드 공개
    desc_lines = []
    sorted_showdown = sorted(strength_cache.keys(), key=lambda u: players[u]["name"])

    for uid in sorted_showdown:
        st = strength_cache[uid]
        desc_lines.append(f"**{players[uid]['name']}**: {hand_name(st)}")
        buf = compose(players[uid]["cards"])
        if buf:
            await channel.send(f"{players[uid]['name']}의 핸드: `{players[uid]['cards'][0]}`, `{players[uid]['cards'][1]}`", file=discord.File(buf, filename=f"hand_{players[uid]['name']}.png"))
    
    if desc_lines:
        await channel.send("🎯 **쇼다운 요약:**\n" + "\n".join(desc_lines))

    # 6. 팟 분배
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

    # 7. 최종 정산
    total_distributed = 0
    result_lines = []
    for uid, p in players.items():
        won = winnings.get(uid, 0)
        p["coins"] += won
        total_distributed += won
        if won > 0:
            result_lines.append(f"**{p['name']}**: +{won} 코인 (현재: {p['coins']})")
        
    await channel.send(f"💰 **총 {total_distributed} 코인 분배 완료!**\n" + "\n".join(result_lines))

    # 8. 게임 종료 (end_game이 DB 업데이트 및 캐시 정리)
    await end_game()

# ====== UI ======

# [추가] 단독 승리 시 10초간 옵션(공개/숨기기/래빗)을 묻는 공개 뷰
class WinnerOptionsView(discord.ui.View):
    def __init__(self, winner_uid: int, winner_name: str, pot: int):
        super().__init__(timeout=10.0)
        self.winner_uid = winner_uid
        self.winner_name = winner_name
        self.pot = pot
        self.already_acted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.winner_uid:
            await interaction.response.send_message("승리자만 결정할 수 있습니다.", ephemeral=True)
            return False
        if self.already_acted:
            await interaction.response.send_message("이미 결정했습니다.", ephemeral=True)
            return False
        return True

    async def _finish_game(self, interaction: discord.Interaction, show_hand: bool = False, rabbit_hunt: bool = False):
        if self.already_acted:
            await interaction.response.defer() # 이미 처리 중이면 무시
            return
        self.already_acted = True
        
        p = players.get(self.winner_uid)
        if not p:
             logging.error(f"WinnerOptionsView: 승리자 {self.winner_uid} 정보를 찾을 수 없음")
             await interaction.response.edit_message(content="오류: 승리자 정보를 찾을 수 없습니다.", view=None)
             await end_game() # 그냥 게임 종료
             return

        # 1. 래빗 헌팅 처리
        if rabbit_hunt:
            await interaction.response.edit_message(content=f"🐇 **{self.winner_name}**님이 래빗 헌팅을 선택!", view=None)
            
            # 덱에서 남은 카드 팝
            needed = 5 - len(game["community"])
            if needed > 0 and len(game["deck"]) >= needed:
                game["community"].extend([game["deck"].pop() for _ in range(needed)])
            
            # 보드 공개
            board_buf = compose(game["community"])
            if board_buf:
                await interaction.channel.send("🃏 **전체 보드 (래빗 헌팅):**", file=discord.File(board_buf, "rabbit_board.png"))
            
            # 핸드도 즉시 공개
            hand_buf = compose(p.get("cards", []))
            if hand_buf:
                await interaction.channel.send(f"🎴 **{p['name']}**님의 핸드:", file=discord.File(hand_buf, "shown_hand.png"))

        # 2. 핸드 공개 처리 (래빗 헌팅 안 했을 때)
        elif show_hand:
            await interaction.response.edit_message(content=f"🏆 **{self.winner_name}** (승리)", view=None)
            cards = p.get("cards", [])
            buf = compose(cards)
            if buf:
                await interaction.channel.send(f"🎴 **{p['name']}**님이 승리 핸드를 공개합니다:", file=discord.File(buf, "shown_hand.png"))
        
        # 3. 숨기기 처리
        else: # (show_hand=False and rabbit_hunt=False)
            await interaction.response.edit_message(content=f"🏆 **{self.winner_name}** (승리)", view=None)

        # 4. 팟 지급 및 게임 종료
        p["coins"] += self.pot
        await interaction.channel.send(f"💰 **{self.winner_name}**님이 팟 {self.pot} 코인을 획득했습니다!")
        
        await end_game()

    @discord.ui.button(label="핸드 공개", style=discord.ButtonStyle.success, row=0)
    async def _show(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish_game(interaction, show_hand=True, rabbit_hunt=False)

    @discord.ui.button(label="숨기기", style=discord.ButtonStyle.danger, row=0)
    async def _hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish_game(interaction, show_hand=False, rabbit_hunt=False)
        
    @discord.ui.button(label="래빗 헌팅 (보드/핸드 모두 공개)", style=discord.ButtonStyle.primary, row=1)
    async def _rabbit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish_game(interaction, show_hand=True, rabbit_hunt=True)

    async def on_timeout(self):
        if self.already_acted:
            return
        self.already_acted = True
        logging.info(f"WinnerOptionsView timed out for {self.winner_uid}")
        
        channel = bot.get_channel(game["channel_id"])
        if not channel:
            logging.error("WinnerOptionsView timeout: 채널을 찾을 수 없음")
            await end_game()
            return

        p = players.get(self.winner_uid)
        if not p:
             logging.error(f"WinnerOptionsView timeout: 승리자 {self.winner_uid} 정보를 찾을 수 없음")
             await end_game()
             return

        # 타임아웃 = 숨기기
        p["coins"] += self.pot
        await channel.send(f"💰 (시간 초과) **{self.winner_name}**님이 팟 {self.pot} 코인을 획득했습니다!")
        
        await end_game()


# 폴드 시 10초간 핸드 공개 여부를 묻는 에페메럴 뷰
class ShowHandOnFoldView(discord.ui.View):
    def __init__(self, actor_id: int, channel: discord.abc.Messageable):
        super().__init__(timeout=10.0)
        self.actor_id = actor_id
        self.channel = channel
        self.already_acted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("당신이 결정할 수 없습니다.", ephemeral=True)
            return False
        if self.already_acted:
            await interaction.response.send_message("이미 결정했습니다.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, show: bool):
        if self.already_acted:
            await interaction.response.defer()
            return
        self.already_acted = True
        
        p = players.get(self.actor_id)
        if not p:
            await interaction.response.edit_message(content="플레이어 정보를 찾을 수 없습니다.", view=None)
            return

        if show:
            cards = p.get("cards", [])
            buf = compose(cards)
            if buf:
                await self.channel.send(f"🎴 **{p['name']}**님이 폴드하며 핸드를 공개합니다:", file=discord.File(buf, "shown_hand.png"))
            else:
                await self.channel.send(f"🎴 **{p['name']}**님이 핸드를 공개하려 했으나 이미지 생성에 실패했습니다.")

        await interaction.response.edit_message(content="🚫 폴드 확인.", view=None)
        
        # 다음 턴 진행
        await advance_or_next_round(self.channel)

    @discord.ui.button(label="핸드 공개", style=discord.ButtonStyle.success)
    async def _show(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, show=True)

    @discord.ui.button(label="숨기기", style=discord.ButtonStyle.danger)
    async def _hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, show=False)

    async def on_timeout(self):
        if self.already_acted:
            return
        self.already_acted = True
        logging.info(f"ShowHandOnFoldView timed out for {self.actor_id}")
        
        # 타임아웃 시 interaction이 없으므로 메시지를 수정할 수 없음.
        # 그냥 다음 턴으로 진행
        await advance_or_next_round(self.channel)

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
    # [버그 수정] 턴마다 고유한 deadline_ts를 받도록 수정
    def __init__(self, actor_id: int, deadline_ts: int, timeout=120):
        super().__init__(timeout=timeout)
        self.actor_id = actor_id
        self.deadline_ts = deadline_ts # 이 뷰가 생성된 시점의 마감 시간
    
    async def on_timeout(self):
        """
        뷰 자체가 타임아웃 (플레이어가 '행동하기' 버튼조차 누르지 않음)
        """
        logging.info(f"ActionPromptView timed out for {self.actor_id} (ts={self.deadline_ts})")
        
        # [버그 수정] 이 타임아웃이 현재 게임 턴의 타임아웃인지 확인
        if self.deadline_ts != game.get("deadline_ts"):
            logging.warning(f"유령 타임아웃(PromptView) 무시: {self.actor_id} (뷰: {self.deadline_ts}, 게임: {game.get('deadline_ts')})")
            return
            
        # 타임아웃 시 자동으로 폴드 처리
        await handle_afk_fold(self.actor_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("아직 네 차례가 아니야!", ephemeral=True); return False
        if not game["game_started"]:
            await interaction.response.send_message("게임이 시작되지 않았어요!", ephemeral=True); return False
        
        # [버그 수정] game["idx"]가 턴 순서를 벗어났는지 먼저 확인
        if game["idx"] >= len(game["turn_order"]):
             await interaction.response.send_message("턴 정보가 잘못되었습니다.", ephemeral=True); return False
             
        current_actor = game["turn_order"][game["idx"]]
        if current_actor != self.actor_id:
            await interaction.response.send_message(f"이미 턴이 지나갔어요! (현재: {players.get(current_actor, {}).get('name', '알수없음')})", ephemeral=True); return False
        
        # [버그 수정] 이 뷰가 현재 턴의 뷰인지 확인
        if self.deadline_ts != game.get("deadline_ts"):
            await interaction.response.send_message("이전 턴의 버튼입니다. 새로고침/채팅방을 확인하세요.", ephemeral=True); return False

        return True
    
    @discord.ui.button(label="🎰 행동하기", style=discord.ButtonStyle.primary)
    async def _open_actions(self, interaction: discord.Interaction, button: discord.ui.Button):
        # [버그 수정] ActionView에도 고유한 deadline_ts 전달
        await interaction.response.send_message("액션을 선택하세요:", view=ActionView(self.actor_id, self.deadline_ts), ephemeral=True)

class ActionView(discord.ui.View):
    """에페메럴: 체크/콜/레이즈/폴드"""
    # [버그 수정] 턴마다 고유한 deadline_ts를 받도록 수정
    def __init__(self, actor_id: int, deadline_ts: int, timeout=120):
        super().__init__(timeout=timeout)
        self.actor_id = actor_id
        self.deadline_ts = deadline_ts # 이 뷰가 생성된 시점의 마감 시간
        
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
        logging.info(f"ActionView timed out for {self.actor_id} (ts={self.deadline_ts})")

        # [버그 수정] 이 타임아웃이 현재 게임 턴의 타임아웃인지 확인
        if self.deadline_ts != game.get("deadline_ts"):
            logging.warning(f"유령 타임아웃(ActionView) 무시: {self.actor_id} (뷰: {self.deadline_ts}, 게임: {game.get('deadline_ts')})")
            return

        # 타임아웃 시 자동으로 폴드 처리
        await handle_afk_fold(self.actor_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not game["game_started"]:
            await interaction.response.send_message("게임이 종료되었습니다.", ephemeral=True); return False
        if game["idx"] >= len(game["turn_order"]):
            await interaction.response.send_message("턴 정보가 없습니다.", ephemeral=True); return False
            
        current_actor = game["turn_order"][game["idx"]]
        if interaction.user.id != self.actor_id or current_actor != self.actor_id:
            await interaction.response.send_message("당신의 턴이 아니거나 턴이 지났습니다.", ephemeral=True); return False
            
        # [버그 수정] 이 뷰가 현재 턴의 뷰인지 확인
        if self.deadline_ts != game.get("deadline_ts"):
            await interaction.response.send_message("이전 턴의 버튼입니다. 새로고침/채팅방을 확인하세요.", ephemeral=True); return False

        return True
    
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
                
                p = players.get(target_uid)
                if not p:
                    await interaction.response.send_message("게임이 시작되지 않았거나 참가자가 아닙니다!", ephemeral=True); return

                cards = p.get("cards")
                if not cards:
                    await interaction.response.send_message("아직 카드가 배분되지 않았어요!", ephemeral=True); return
                
                buf = compose(cards)
                if buf:
                    # [수정] "홀카드" -> "핸드"
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


# [수정] 폴드 시 핸드 공개 로직 추가
async def handle_fold(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    
    # 1. 일단 폴드 상태로 만듦
    p["folded"] = True
    game["acted"].add(uid)
    
    # 2. 이전 120초 타이머(ActionPromptView) 정리
    await disable_prev_prompt(inter.channel)
    
    # 3. 10초짜리 "핸드 공개?" 뷰를 에페메럴 응답으로 보냄
    view = ShowHandOnFoldView(actor_id=uid, channel=inter.channel)
    await inter.response.edit_message(content="🚫 폴드했습니다. 핸드를 공개하시겠습니까?", view=view)
    
    # [중요] advance_or_next_round는 ShowHandOnFoldView의 콜백/타임아웃에서 호출됨
    # (여기서는 advance_or_next_round를 호출하지 않음)


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
    # [버그 수정] game["idx"]가 턴 순서를 벗어났는지 먼저 확인
    if not game["turn_order"] or game["idx"] >= len(game["turn_order"]):
        logging.info(f"AFK: 턴 정보가 이미 없음, 무시")
        return
        
    current_turn_uid = game["turn_order"][game["idx"]]
    if current_turn_uid != uid:
        # 타임아웃이 발생했지만, 그 직전에 유저가 행동했거나 턴이 이미 넘어간 경우
        logging.info(f"AFK: 턴이 이미 {uid}가 아님 (현재: {current_turn_uid}), 무시")
        return

    # 3. 플레이어 정보 확인
    p = players.get(uid)
    if not p or p["folded"] or p["all_in"]:
        return

    # 4. 강제 폴드 처리
    logging.info(f"AFK: {p['name']} ({uid}) 자동 폴드 처리")
    p["folded"] = True
    p["afk_kicked"] = True # [수정] AFK 플래그 설정 (게임 종료 시 퇴장 처리용)
    game["acted"].add(uid) 
    await channel.send(f"⏰ **{p['name']}**님의 턴 시간이 초과되어 자동으로 **폴드**합니다. (다음 게임에서 제외됩니다)")
    
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
        cur = await db.execute("SELECT name, coin, in_game FROM character WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        await inter.response.send_message("먼저 `/등록`으로 캐릭터를 만들어줘!", ephemeral=True); return
    
    name, coin, in_game_db = row
    
    # 로컬 캐시(players)와 DB(in_game) 상태 동기화
    status = "알 수 없음"
    if uid in players:
        status = "게임 참가 중"
        if game["game_started"]:
            status = "게임 플레이 중"
        else:
            status = "게임 대기 중"
    elif in_game_db == 1:
        status = "참가 중 (봇 재시작됨, /참가 필요)"
    else:
        status = "대기 중 (미참가)"

    embed = discord.Embed(title="🎮 캐릭터 정보", color=0x00ff00)
    embed.add_field(name="이름", value=name, inline=True)
    embed.add_field(name="코인", value=f"{coin:,}개", inline=True)
    embed.add_field(name="상태", value=status, inline=True)
    await inter.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="참가", description="현재 게임 로비에 참가")
async def 참가(inter: discord.Interaction):
    global players # players 딕셔너리를 수정
    
    if game["game_started"]:
        await inter.response.send_message("이미 게임이 시작되었어요! 다음 게임에 합류해줘요.", ephemeral=True); return
    uid = inter.user.id
    
    # 1. 이미 로컬 캐시(players)에 있는가? (정상 참가 상태)
    if uid in players:
        await inter.response.send_message("이미 참가 중이에요!", ephemeral=True); return
    
    # 2. 로컬 캐시(players)에는 없지만, DB에는 있는가? (봇 재시작 복구)
    async with aiosqlite.connect("test.db") as db:
        cur_db = await db.execute("SELECT name, coin, in_game FROM character WHERE user_id=?", (uid,))
        row_db = await cur_db.fetchone()
        
        if not row_db:
            await inter.response.send_message("먼저 `/등록`으로 캐릭터 생성!", ephemeral=True); return
        
        name, coin, in_game_db = row_db

        if coin <= 0:
            await inter.response.send_message("코인이 0이라 참가 불가! (파산)", ephemeral=True)
            # DB 상태도 0으로 클린
            if in_game_db == 1:
                 await db.execute("UPDATE character SET in_game=0 WHERE user_id=?", (uid,))
                 await db.commit()
            return
        
        # 3. 로컬 캐시에도 없고, DB에도 in_game=0인가? (신규 참가)
        if in_game_db == 0:
            players[uid] = {"name": name, "coins": coin, "bet": 0, "contrib": 0, "cards": [], "folded": False, "all_in": False, "afk_kicked": False}
            await db.execute("UPDATE character SET in_game=1 WHERE user_id=?", (uid,))
            await db.commit()
            # [수정] 공개 메시지로 변경
            await inter.response.send_message(f"✅ **{name}**님이 참가했습니다! (현재 인원 {len(players)}명)")
        
        # 4. 로컬 캐시에는 없는데, DB에는 in_game=1인가? (봇 재시작 복구)
        elif in_game_db == 1:
            logging.info(f"봇 재시작 복구: {name}({uid}) 님을 로비에 다시 추가합니다.")
            players[uid] = {"name": name, "coins": coin, "bet": 0, "contrib": 0, "cards": [], "folded": False, "all_in": False, "afk_kicked": False}
            # DB는 이미 1이므로 건드릴 필요 없음
            # [수정] 공개 메시지로 변경
            await inter.response.send_message(f"✅ 봇 재시작 복구 완료! (**{name}**님 참가 처리)\n현재 인원 {len(players)}명")

@bot.tree.command(name="퇴장", description="현재 게임 로비에서 퇴장 (다음 게임부터 미참여)")
async def 퇴장(inter: discord.Interaction):
    uid = inter.user.id
    if uid not in players:
        await inter.response.send_message("현재 게임에 참가하지 않았어요.", ephemeral=True); return
    
    if game["game_started"]:
        await inter.response.send_message("게임 진행 중에는 퇴장할 수 없어요! (AFK 시 자동 퇴장)", ephemeral=True); return
    
    # 게임 대기 중일 때만 퇴장 가능
    p = players.pop(uid)
    name = p["name"]; coin = p["coins"]
    
    async with aiosqlite.connect("test.db") as db:
        await db.execute("UPDATE character SET in_game=0, coin=? WHERE user_id=?", (coin, uid))
        await db.commit()
    await inter.response.send_message(f"🚪 **{name}**님이 퇴장했습니다.")

@bot.tree.command(name="시작", description="텍사스 홀덤 게임 시작")
async def 시작(inter: discord.Interaction):
    global game # game 딕셔너리 수정
    
    if game["game_started"]:
        await inter.response.send_message("이미 게임이 진행 중이에요!", ephemeral=True); return
    if len(players) < 2:
        await inter.response.send_message("최소 2명이 필요해요!", ephemeral=True); return
    if len(players) > 10:
        await inter.response.send_message("최대 10명까지 가능해요!", ephemeral=True); return

    game.update({
        "deck": [], "community": [], "pot": 0, "round": "preflop",
        "turn_order": list(players.keys()), "idx": 0,
        "current_bet": 0, "acted": set(), "game_started": True,
        "last_prompt_msg_id": None, "channel_id": inter.channel_id
    })
    # 딜러 버튼 회전
    n = len(game["turn_order"])
    game["dealer_pos"] = (game["dealer_pos"] + 1) % n

    # 핸드 배분 (및 플레이어 상태 초기화)
    deal_hole()

    # 블라인드 게시
    dealer_i = game["dealer_pos"]
    sb_i = (dealer_i + 1) % n if n > 2 else dealer_i
    bb_i = (sb_i + 1) % n if n > 2 else (dealer_i + 1) % n
    sb_uid = game["turn_order"][sb_i]; bb_uid = game["turn_order"][bb_i]

    def post_blind(uid: int, amount: int):
        p = players[uid]
        pay = min(amount, p["coins"])
        p["coins"] -= pay
        p["bet"] += pay
        if p["coins"] == 0: p["all_in"] = True
        return pay

    sb_paid = post_blind(sb_uid, game["sb"])
    bb_paid = post_blind(bb_uid, game["bb"])
    game["current_bet"] = max(bb_paid, sb_paid) # current_bet은 BB 금액

    # 프리플랍 선행
    first_to_act_i = (bb_i + 1) % n if n > 2 else sb_i
    game["idx"] = first_to_act_i # next_actor_index는 prompt_action에서 처리

    # 시작 임베드
    embed = discord.Embed(title="🃏 텍사스 홀덤 시작!", color=0x0099ff)
    embed.add_field(name="참가자", value=", ".join([p["name"] for p in players.values()]), inline=False)
    embed.add_field(name="블라인드", value=f"SB {game['sb']}, BB {game['bb']}", inline=True)
    embed.add_field(name="딜러", value=players[game["turn_order"][game["dealer_pos"]]]["name"], inline=True)
    embed.add_field(name="라운드", value="프리플랍", inline=True)
    await inter.response.send_message(embed=embed)

    # “내 카드 보기” — 모든 플레이어 이름 버튼을 한 메시지에 가로로
    uid_name_pairs = [(uid, p["name"]) for uid, p in players.items()]
    view = MultiPeekCardsView(uid_name_pairs)
    # [수정] "홀카드" -> "핸드"
    await inter.channel.send("🎴 **내 핸드 보기** — 자신의 이름 버튼을 눌러 확인하세요!", view=view)

    # 블라인드 안내 + 첫 액터 안내
    # [수정] 첫 액터를 next_actor_index로 정확히 찾아서 안내
    real_first_actor_i = next_actor_index(first_to_act_i)
    if real_first_actor_i is None:
         # (예: SB, BB가 모두 올인)
         await inter.channel.send(
            f"🪙 블라인드 게시 — SB: **{players[sb_uid]['name']}** {sb_paid} (올인), "
            f"BB: **{players[bb_uid]['name']}** {bb_paid} (올인)\n"
            f"🎯 행동할 플레이어가 없습니다. 즉시 다음 스트리트로 넘어갑니다."
         )
         await asyncio.sleep(1)
         await go_next_street(inter.channel)
         return

    game["idx"] = real_first_actor_i # 턴 인덱스 확정
    first_actor_name = players[game['turn_order'][game['idx']]]['name']
    
    await inter.channel.send(
        f"🪙 블라인드 게시 — SB: **{players[sb_uid]['name']}** {sb_paid}, "
        f"BB: **{players[bb_uid]['name']}** {bb_paid}\n"
        f"🎯 프리플랍 선행: **{first_actor_name}**"
    )

    # 첫 턴 시작
    await asyncio.sleep(1)
    await prompt_action(inter.channel)

# [수정] "홀카드" -> "핸드"
@bot.tree.command(name="내핸드", description="내 핸드 보기 (나만)")
async def 내핸드(inter: discord.Interaction):
    uid = inter.user.id
    p = players.get(uid)
    if not p or not p.get("cards"):
        await inter.response.send_message("아직 카드가 없어요! (게임이 시작되지 않았거나, 참가자가 아님)", ephemeral=True); return
    
    buf = compose(p["cards"])
    if buf:
        # [수정] "홀카드" -> "핸드"
        await inter.response.send_message("🎴 당신의 핸드:", file=discord.File(buf, filename="my_cards.png"), ephemeral=True)
    else:
        await inter.response.send_message("카드 이미지를 생성할 수 없습니다.", ephemeral=True)

@bot.tree.command(name="상태", description="현재 게임 상태 확인")
async def 상태(inter: discord.Interaction):
    if not game["game_started"]:
        if players:
            embed = discord.Embed(title="🎰 게임 대기 중", color=0xffaa00)
            embed.add_field(name="참가자 수", value=f"{len(players)}명", inline=True)
            embed.add_field(name="참가자", value=", ".join([p["name"] for p in players.values()]), inline=False)
            embed.add_field(name="게임 시작", value="2명 이상일 때 `/시작`", inline=False)
        else:
            embed = discord.Embed(title="🎰 참가자 없음", color=0x666666)
            embed.description = "`/참가` 명령어로 게임에 참가하세요!"
        await inter.response.send_message(embed=embed); return

    embed = discord.Embed(title="🃏 게임 진행 중", color=0x00ff00)
    embed.add_field(name="라운드", value=game.get("round", "preflop"), inline=True)
    embed.add_field(name="현재 팟", value=f"{game['pot']} 코인", inline=True)
    embed.add_field(name="현재 베팅", value=f"{game['current_bet']} 코인", inline=True)
    try:
        dealer_name = players[game["turn_order"][game["dealer_pos"]]]["name"]
        embed.add_field(name="딜러", value=dealer_name, inline=True)
        embed.add_field(name="블라인드", value=f"SB {game['sb']}, BB {game['bb']}", inline=True)
    except Exception:
        pass
    
    if game["idx"] < len(game["turn_order"]):
        try:
             actor_name = players[game["turn_order"][game["idx"]]]["name"]
             embed.add_field(name="현재 턴", value=actor_name, inline=True)
        except (KeyError, IndexError):
             embed.add_field(name="현재 턴", value="알 수 없음", inline=True)


    lines = []
    for uid in game.get("turn_order", []): # 턴 순서대로 표시
        p = players.get(uid)
        if not p: continue
        
        status = "폴드" if p["folded"] else ("올인" if p["all_in"] else f"{p['coins']}코인")
        bet = f" / 베팅:{p['bet']}" if p["bet"] > 0 else ""
        contrib = f" / 총액:{p['contrib']}" if p.get("contrib", 0) > 0 else ""
        lines.append(f"{p['name']}: {status}{bet}{contrib}")
    
    embed.add_field(name="플레이어 상태", value="\n".join(lines), inline=False)
    
    if game["community"]:
        embed.add_field(name="보드 카드", value=f"{' '.join(game['community'])}", inline=False)
        buf = compose(game["community"])
        if buf:
            await inter.response.send_message(embed=embed, file=discord.File(buf, "board_state.png"))
            return

    await inter.response.send_message(embed=embed)

@bot.tree.command(name="강제종료", description="게임 강제 종료 및 로비 초기화 (관리자)")
async def 강제종료(inter: discord.Interaction):
    global game, players

    if not inter.user.guild_permissions.administrator:
        await inter.response.send_message("관리자만 가능!", ephemeral=True); return
    
    if not game["game_started"] and not players:
         await inter.response.send_message("진행 중인 게임이나 대기 중인 플레이어가 없어요.", ephemeral=True); return
    
    channel_id = game.get("channel_id") or inter.channel_id
    channel = bot.get_channel(channel_id)

    if channel:
        await disable_prev_prompt(channel) # 이전 프롬프트 정리
            
    # DB에 모든 플레이어(players 캐시 기준)를 'in_game=0'으로 설정
    async with aiosqlite.connect("test.db") as db:
        for uid, p in players.items():
            await db.execute("UPDATE character SET coin=?, in_game=0, bet=0, all_in=0 WHERE user_id=?", (p["coins"], uid))
        await db.commit()

    # 메모리 초기화
    players = {}
    game = {
        "deck": [], "community": [], "pot": 0, "round": None,
        "turn_order": [], "idx": 0, "current_bet": 0, "acted": set(),
        "game_started": False, "last_prompt_msg_id": None, "channel_id": channel_id,
        "dealer_pos": -1, "sb": 10, "bb": 20,
        "timer_task": None, "deadline_ts": None,
    }
            
    await inter.response.send_message(f"🛑 게임 강제 종료 및 로비 초기화 (관리자: {inter.user.name})")


def _progress_bar(seconds_left: int, total: int = 120, width: int = 12) -> str:
    seconds_left = max(0, min(total, seconds_left))
    elapsed = total - seconds_left
    filled = int(round(elapsed / total * width))
    return "█" * filled + "░" * (width - filled)

async def _run_countdown(msg: discord.Message, base_text: str, deadline_ts: int):
    try:
        while True:
            await asyncio.sleep(5)  # 5초 간격 갱신
            now = int(datetime.utcnow().timestamp())
            left = max(0, deadline_ts - now)
            
            # 턴이 이미 넘어갔는지 (deadline_ts가 바뀌었는지)
            if game.get("deadline_ts") != deadline_ts:
                 logging.debug("카운트다운: 턴이 이미 넘어감, 중지")
                 return

            bar = _progress_bar(left, 120) # 120초 기준
            extra = f"\n⏳ 마감: <t:{deadline_ts}:R> (<t:{deadline_ts}:T>)\n`[{bar}] {left}s`"
            
            try:
                await msg.edit(content=base_text + extra)
            except discord.NotFound:
                 logging.debug("카운트다운 편집 실패 (메시지 삭제됨), 중지")
                 return
            except Exception as e:
                logging.debug(f"카운트다운 편집 실패: {e}")
                return # 편집 실패 시 루프 중단
                
            if left == 0:
                logging.debug("카운트다운 0초 도달, 종료")
                return
            
    except asyncio.CancelledError:
        logging.debug("카운트다운 작업 취소됨")
        pass
    except Exception as e:
        logging.exception(f"카운트다운 루프 에러: {e}")



# ====== 실행부: 환경변수에서 토큰 읽기 ======
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            token = os.getenv("TOKEN")
        except ImportError:
            pass
            
        if not token:
            raise RuntimeError(
                "환경변수 TOKEN이 없습니다 — 로컬에선 .env 파일에 TOKEN=... 를 추가하거나, "
                "배포 환경(Railway 등)의 Variables에 TOKEN을 추가해 주세요"
            )
    bot.run(token)
