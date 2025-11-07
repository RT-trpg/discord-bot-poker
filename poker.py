import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from PIL import Image
import io, os, random, asyncio
from itertools import combinations
import logging

# ====== 로깅 ======
logging.basicConfig(level=logging.INFO)

# ====== 인텐트 최소 권한 권장 ======
intents = discord.Intents.default()
intents.message_content = True   # 메시지 내용 접근이 필요한 경우
intents.members = True           # 길드 멤버 정보가 필요한 경우만 True

bot = commands.Bot(command_prefix="!", intents=intents)

# ====== 봇 준비 이벤트 ======
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")

# 슬래시 명령(app_commands) 사용 시, 시작할 때 동기화해 두면 편해요
@bot.event
async def setup_hook():
    try:
        synced = await bot.tree.sync()
        logging.info("Slash commands synced: %s", [c.name for c in synced])
    except Exception as e:
        logging.exception("Slash sync failed: %s", e)

# ====== 예시 슬래시 커맨드 ======
@bot.tree.command(name="ping", description="핑 확인")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# ====== 실행부: 환경변수에서 토큰 읽기 ======
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        # 로컬 개발에서 .env를 쓴다면 주석 해제 후 사용 가능
        # from dotenv import load_dotenv
        # load_dotenv()
        # token = os.getenv("TOKEN")
        # if not token:
        raise RuntimeError(
            "환경변수 TOKEN이 없습니다 — 로컬에선 `$env:TOKEN=...` 설정 후 실행하거나, "
            "Railway Variables에 TOKEN을 추가해 주세요"
        )
    bot.run(token)


# ====== 카드 이미지 경로/크기 ======
CARDS_DIR = r"C:\Users\서나영\Desktop\봇\card"  # 예: As.png, 10h.png ...
CARD_W, CARD_H = 67, 92          # 원본
SCALE = 0.9                      # 1/4로 축소 전송
GAP = 6

# ====== 게임 캐시 ======
# players: {uid: {name, coins, bet, contrib, cards, folded, all_in}}
players = {}
game = {
    "deck": [],
    "community": [],
    "pot": 0,
    "round": None,          # "preflop"|"flop"|"turn"|"river"
    "turn_order": [],
    "idx": 0,               # 현재 턴 인덱스
    "current_bet": 0,       # 이번 스트리트 기준 베팅
    "acted": set(),         # 이번 스트리트에서 최소 1회 행동한 uid
    "game_started": False,
    "last_prompt_msg_id": None,
    "channel_id": None,

    # 블라인드/딜러
    "dealer_pos": -1,       # 딜러 버튼(턴오더 인덱스). 매 게임마다 회전
    "sb": 10,               # 스몰블라인드
    "bb": 20,               # 빅블라인드
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
        for i in range(len(vs)-4):
            window = vs[i:i+5]
            if window == list(range(window[0], window[0]-5, -1)):
                return window[0]
        if {14,5,4,3,2}.issubset(set(vs)):  # A-5
            return 5
        return None
    sh = straight_high(uniq)

    if is_flush and sh:                 return (8, sh)          # 스트레이트 플러시
    if 4 in counts.values():            # 포카드
        four = max([v for v,c in counts.items() if c==4])
        kicker = max([v for v in vals if v != four])
        return (7, four, kicker)
    trips = sorted([v for v,c in counts.items() if c==3], reverse=True)
    pairs = sorted([v for v,c in counts.items() if c==2], reverse=True)
    if trips and (pairs or len(trips) >= 2): # 풀하우스
        t = trips[0]; p = pairs[0] if pairs else trips[1]
        return (6, t, p)
    if is_flush:                            return (5, *vals)       # 플러시
    if sh:                                  return (4, sh)          # 스트레이트
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
async def disable_prev_prompt(channel):
    if game["last_prompt_msg_id"]:
        try:
            msg = await channel.fetch_message(game["last_prompt_msg_id"])
            await msg.edit(view=None)
        except Exception as e:
            logging.warning(f"이전 프롬프트 비활성화 실패: {e}")
        game["last_prompt_msg_id"] = None

async def prompt_action(channel):
    if not game["turn_order"] or game["idx"] >= len(game["turn_order"]):
        logging.error("잘못된 턴 상태"); return
    uid = game["turn_order"][game["idx"]]
    alive = [u for u in active_players() if not players[u]["folded"]]
    if len(alive) <= 1:
        await handle_single_winner(channel, alive); return

    p = players[uid]; cur_bet = game["current_bet"]
    need_to_call = max(0, cur_bet - p["bet"])

    await disable_prev_prompt(channel)
    view = ActionPromptView(actor_id=uid)
    msg = await channel.send(
        f"🎯 **{p['name']}**의 차례!\n"
        f"라운드: **{game['round'] or 'preflop'}** / 팟: **{game['pot']}** / "
        f"콜 필요: **{need_to_call}** / 보유: **{p['coins']}**",
        view=view
    )
    game["last_prompt_msg_id"] = msg.id

async def advance_or_next_round(channel):
    if ready_to_advance() or next_actor_index() is None:
        await go_next_street(channel)
    else:
        next_idx = next_actor_index(game["idx"] + 1)
        if next_idx is not None:
            game["idx"] = next_idx
            await prompt_action(channel)
        else:
            await go_next_street(channel)

async def go_next_street(channel):
    # 현재 스트리트 종료: 베팅 → 팟/기여
    for uid, p in players.items():
        game["pot"] += p["bet"]
        p["contrib"] = p.get("contrib", 0) + p["bet"]
        p["bet"] = 0
    game["current_bet"] = 0
    game["acted"].clear()

    if game["round"] is None or game["round"] == "preflop":
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
            if maybe is not None: game["idx"] = maybe

    elif game["round"] == "flop":
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

    elif game["round"] == "turn":
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

    else:
        # 쇼다운
        await resolve_showdown(channel)
        return

    buf = compose(game["community"])
    if buf:
        await channel.send(file=discord.File(buf, filename=f"board_{game['round']}.png"))

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
            await db.execute("UPDATE character SET coin=? WHERE user_id=?", (players[winner]["coins"], winner))
            await db.commit()
        await channel.send(f"🏆 **{players[winner]['name']}** 단독 승리! 팟 {game['pot']} 코인 획득")
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
    for uid, st in strength_cache.items():
        desc_lines.append(f"**{players[uid]['name']}**: {hand_name(st)}")
        buf = compose(players[uid]["cards"])
        if buf:
            await channel.send(f"{players[uid]['name']}의 핸드:", file=discord.File(buf, filename=f"hand_{players[uid]['name']}.png"))
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
        await channel.send(f"🫙 **사이드팟 #{i}** {amount} → 승자: {', '.join(players[u]['name'] for u in winners)}")

    total_distributed = 0
    for uid, p in players.items():
        won = winnings.get(uid, 0)
        p["coins"] += won
        total_distributed += won

    async with aiosqlite.connect("test.db") as db:
        for uid, p in players.items():
            await db.execute("UPDATE character SET coin=? WHERE user_id=?", (p["coins"], uid))
        await db.commit()

    await channel.send(f"💰 **총 {total_distributed} 코인 분배 완료!**")
    await end_game()

async def end_game():
    game.update({
        "game_started": False, "round": None, "pot": 0,
        "current_bet": 0, "acted": set(), "deck": [], "community": [],
        "turn_order": [], "idx": 0, "last_prompt_msg_id": None, "channel_id": None
    })
    for p in players.values():
        p["cards"] = []; p["bet"] = 0; p["contrib"] = 0; p["folded"] = False; p["all_in"] = False

# ====== UI ======
class RaiseModal(discord.ui.Modal, title="레이즈 금액 입력"):
    def __init__(self, actor_id: int):
        super().__init__()
        self.actor_id = actor_id
        self.amount = discord.ui.TextInput(label="레이즈 금액", placeholder="정수로 입력 (예: 100)", required=True, max_length=10)
        self.add_item(self.amount)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(str(self.amount))
            if val <= 0: raise ValueError
        except Exception:
            await interaction.response.send_message("1 이상의 정수를 입력해 주세요!", ephemeral=True); return
        await handle_raise(interaction, self.actor_id, val)

class ActionPromptView(discord.ui.View):
    """공개 '행동하기' 버튼 → 현재 차례인 유저만 누를 수 있음(검증 후 에페메럴 버튼 제공)"""
    def __init__(self, actor_id: int, timeout=60): # 60초 타임아웃
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
    def __init__(self, actor_id: int, timeout=60): # 60초 타임아웃
        super().__init__(timeout=timeout); self.actor_id = actor_id

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
                cards = players.get(target_uid, {}).get("cards")
                if not cards:
                    await interaction.response.send_message("아직 카드가 배분되지 않았어요!", ephemeral=True); return
                buf = compose(cards)
                if buf:
                    await interaction.response.send_message(
                        "🎴 당신의 홀카드:", file=discord.File(buf, filename="my_cards.png"), ephemeral=True
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
        await inter.response.send_message(f"체크 불가! {need} 코인 부족", ephemeral=True); return
    await inter.response.send_message("✅ 체크!", ephemeral=True)
    game["acted"].add(uid)
    await advance_or_next_round(inter.channel)

async def handle_call(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    need = max(0, game["current_bet"] - p["bet"])
    if need == 0:
        await inter.response.send_message("이미 맞춰짐(콜 필요 없음)", ephemeral=True); return
    pay = min(need, p["coins"])
    p["coins"] -= pay; p["bet"] += pay
    if p["coins"] == 0:
        p["all_in"] = True; await inter.response.send_message(f"🔥 올인! {pay} 코인", ephemeral=True)
    else:
        await inter.response.send_message(f"📞 콜 {pay} 코인", ephemeral=True)
    game["acted"].add(uid)
    await advance_or_next_round(inter.channel)

async def handle_raise(inter: discord.Interaction, uid: int, raise_amt: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    need = max(0, game["current_bet"] - p["bet"])
    if raise_amt <= 0:
        await inter.response.send_message("레이즈 금액은 1 이상이어야 합니다!", ephemeral=True); return
    total_need = need + raise_amt
    if total_need > p["coins"]: total_need = p["coins"]  # 올인 레이즈
    if total_need <= 0:
        await inter.response.send_message("베팅할 코인이 없습니다!", ephemeral=True); return
    p["coins"] -= total_need; p["bet"] += total_need
    game["current_bet"] = max(game["current_bet"], p["bet"])
    if p["coins"] == 0:
        p["all_in"] = True; await inter.response.send_message(f"🔥 올인 레이즈! {total_need} 코인", ephemeral=True)
    else:
        await inter.response.send_message(f"📈 레이즈 {total_need} 코인 (현재 베팅: {game['current_bet']})", ephemeral=True)
    game["acted"] = {uid}  # 모두 다시 행동해야 함
    next_idx = next_actor_index(game["idx"] + 1)
    if next_idx is not None:
        game["idx"] = next_idx; await prompt_action(inter.channel)
    else:
        await go_next_street(inter.channel)

async def handle_fold(inter: discord.Interaction, uid: int):
    p = players.get(uid)
    if not p: await inter.response.send_message("플레이어 정보를 찾을 수 없습니다!", ephemeral=True); return
    p["folded"] = True
    await inter.response.send_message("🚫 폴드!", ephemeral=True)
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
        # (올인 유저는 턴이 없어야 하지만, 방어 코드로 추가)
        return

    # 4. 강제 폴드 처리
    p["folded"] = True
    game["acted"].add(uid) 
    await channel.send(f"⏰ **{p['name']}**님의 턴 시간이 초과되어 자동으로 **폴드**합니다.")

    # 5. 다음 턴으로 진행
    await advance_or_next_round(channel)

# ====== 봇 이벤트 ======
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print("✅ 텍사스 홀덤 봇 준비 완료!")
    print(f"봇 이름: {bot.user} / 서버 수: {len(bot.guilds)}")

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
        cur = await db.execute("SELECT name, coin FROM character WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        await inter.response.send_message("먼저 `/등록`으로 캐릭터 생성!", ephemeral=True); return
    name, coin = row
    if coin <= 0:
        await inter.response.send_message("코인이 0이라 참가 불가!", ephemeral=True); return
    players[uid] = {"name": name, "coins": coin, "bet": 0, "contrib": 0, "cards": [], "folded": False, "all_in": False}
    async with aiosqlite.connect("test.db") as db:
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
    name = players[uid]["name"]; coin = players[uid]["coins"]; players.pop(uid)
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
        "current_bet": 0, "acted": set(), "game_started": True,
        "last_prompt_msg_id": None, "channel_id": inter.channel_id
    })
    # 딜러 버튼 회전
    n = len(game["turn_order"])
    game["dealer_pos"] = (game["dealer_pos"] + 1) % n

    # 홀카드 배분
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
        p["contrib"] = p.get("contrib", 0) + pay
        if p["coins"] == 0: p["all_in"] = True
        return pay

    sb_paid = post_blind(sb_uid, game["sb"])
    bb_paid = post_blind(bb_uid, game["bb"])
    game["current_bet"] = max(players[bb_uid]["bet"], players[sb_uid]["bet"])

    # 프리플랍 선행
    first_to_act_i = (bb_i + 1) % n if n > 2 else sb_i
    game["idx"] = next_actor_index(first_to_act_i) or first_to_act_i

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
    await inter.channel.send("🎴 **내 카드 보기** — 자신의 이름 버튼을 눌러 확인하세요!", view=view)

    # 블라인드 안내 + 첫 액터 안내
    await inter.channel.send(
        f"🪙 블라인드 게시 — SB: **{players[sb_uid]['name']}** {sb_paid}, "
        f"BB: **{players[bb_uid]['name']}** {bb_paid}\n"
        f"🎯 프리플랍 선행: **{players[game['turn_order'][game['idx']]]['name']}**"
    )

    # 첫 턴 시작
    await asyncio.sleep(1)
    await prompt_action(inter.channel)

@bot.tree.command(name="내카드", description="내 홀카드 보기 (나만)")
async def 내카드(inter: discord.Interaction):
    uid = inter.user.id
    if uid not in players or not players[uid]["cards"]:
        await inter.response.send_message("아직 카드가 없어요!", ephemeral=True); return
    buf = compose(players[uid]["cards"])
    if buf:
        await inter.response.send_message("🎴 당신의 홀카드:", file=discord.File(buf, filename="my_cards.png"), ephemeral=True)
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
    embed.add_field(name="라운드", value=game["round"] or "preflop", inline=True)
    embed.add_field(name="현재 팟", value=f"{game['pot']} 코인", inline=True)
    embed.add_field(name="현재 베팅", value=f"{game['current_bet']} 코인", inline=True)
    try:
        dealer_name = players[game["turn_order"][game["dealer_pos"]]]["name"]
        embed.add_field(name="딜러", value=dealer_name, inline=True)
        embed.add_field(name="블라인드", value=f"SB {game['sb']}, BB {game['bb']}", inline=True)
    except Exception:
        pass
    if game["idx"] < len(game["turn_order"]):
        embed.add_field(name="현재 턴", value=players[game["turn_order"][game["idx"]]]["name"], inline=True)

    lines = []
    for uid, p in players.items():
        status = "폴드" if p["folded"] else ("올인" if p["all_in"] else f"{p['coins']}코인")
        bet = f" / 베팅:{p['bet']}" if p["bet"] > 0 else ""
        lines.append(f"{p['name']}: {status}{bet}")
    embed.add_field(name="플레이어 상태", value="\n".join(lines), inline=False)
    if game["community"]:
        embed.add_field(name="보드 카드 수", value=f"{len(game['community'])}장", inline=True)

    await inter.response.send_message(embed=embed)

@bot.tree.command(name="강제종료", description="게임 강제 종료 (관리자)")
async def 강제종료(inter: discord.Interaction):
    if not inter.user.guild_permissions.administrator:
        await inter.response.send_message("관리자만 가능!", ephemeral=True); return
    if not game["game_started"]:
        await inter.response.send_message("진행 중인 게임이 없어요.", ephemeral=True); return
    async with aiosqlite.connect("test.db") as db:
        for uid, p in players.items():
            await db.execute("UPDATE character SET coin=?, in_game=0 WHERE user_id=?", (p["coins"], uid))
        await db.commit()
    await end_game()
    await inter.response.send_message("🛑 게임 강제 종료")

# ====== 실행 ======
if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "여기에_토큰":
        print("❌ BOT_TOKEN을 설정해 주세요! (config.py 권장)")
        raise SystemExit(1)
    bot.run(BOT_TOKEN)