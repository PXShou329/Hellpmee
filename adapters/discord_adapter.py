"""
Discord 適配器（v2.1.2 音樂修正版）
─────────────────────────────────────────────────────────────────
核心修正：
    ✅ 音樂流程合併成單一進入點 _add_and_play
    ✅ 每個失敗點都有詳細 console log（含 traceback）
    ✅ 第一首歌加入時保證觸發播放
    ✅ /搜尋歌曲 修復
    ✅ /歌姬啦 加搜尋類型 + 來源參數
    ✅ /佇列 顯示來源
    ✅ 新增 /喝什麼

指令清單：

🎼 音樂
    /歌姬啦 <關鍵字> [搜尋類型] [來源]   ── 播放
    /搜尋歌曲 <關鍵字>                  ── 5 選 1 手動挑歌
    /佇列                              ── 查看目前歌單（含來源）
    /跳過                              ── 跳過目前歌曲
    /現正播放
    /循環
    /清空佇列                          ── 管理員限定
    /停止

🌍 生活
    /天氣 <地點>
    /吃飯飯                            ── 隨機食物
    /喝什麼                            ── 隨機飲料【v2.1.2 新增】
    /吃什麼 <食物> [地點]

🛠️ 工具
    /算算數 <算式>
    /翻譯姬 <內容> <語言> [模式]
    /洗芭樂 <類型> <內容>
    /提醒醒 <事項>

❓ /幫助
"""
import ast
import asyncio
import operator
import random
import traceback
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from core.music_engine import MusicEngine
from core.music_queue import (
    QueueManager, QueuedSong, LoopMode, format_duration,
)
from core.weather_engine import WeatherEngine
from core.food_engine import FoodEngine, GooglePlacesNotConfiguredError, GooglePlacesPermissionError
from core.translate_engine import TranslateEngine
from core.utility_ai import UtilityAI
from core import food_picker, drink_picker
from database.base import BaseRepository
from database.models import FoodSearchHistory, ReminderEntry
from database.redis_cache import RedisCache


# ════════════════════════════════════════════════════════════════
#  顏色 & Embed 樣式
# ════════════════════════════════════════════════════════════════
COLOR_MUSIC    = 0xE74C3C
COLOR_WEATHER  = 0x3498DB
COLOR_FOOD     = 0xFF6B35
COLOR_DRINK    = 0xAA66CC
COLOR_TOOL     = 0x95A5A6
COLOR_REMINDER = 0xF1C40F
COLOR_INFO     = 0x9B59B6
COLOR_ERROR    = 0xFF4444
EMBED_FOOTER   = "🐾 黑優浦蜜 ～喵♡ | 牢大的專屬助手"


def make_embed(title: str, description: str = "", color: int = COLOR_INFO) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=EMBED_FOOTER)
    return e


# ════════════════════════════════════════════════════════════════
#  Google Maps 搜尋 URL（v2.3.0：巴豆妖找附近主流程）
# ════════════════════════════════════════════════════════════════
def build_google_maps_search_url(query: str) -> str:
    """
    產生 Google Maps 搜尋連結。
    用 urllib.parse.quote_plus 正確 URL-encode（空白→+、繁中正常）。
    例：「紅燒肉 捷運大安站」→
        https://www.google.com/maps/search/?api=1&query=%E7%B4%85...
    """
    from urllib.parse import quote_plus
    q = (query or "").strip()
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


# ════════════════════════════════════════════════════════════════
#  權限檢查
# ════════════════════════════════════════════════════════════════
def is_admin(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    p = member.guild_permissions
    return p.administrator or p.manage_guild or p.manage_messages


# ════════════════════════════════════════════════════════════════
#  安全數學計算
# ════════════════════════════════════════════════════════════════
_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.Mod: operator.mod,
}

def safe_eval(expr: str) -> float:
    tree = ast.parse(expr.strip(), mode='eval')
    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](_eval(node.operand))
        raise ValueError("不允許的運算")
    return _eval(tree)


async def send_view_and_track(sender_coro_factory, view):
    """
    發送帶 view 的訊息後，把回傳的 message 記到 view.message，
    讓 on_timeout 能 edit 訊息顯示「已過期」。
    sender_coro_factory: 一個 callable，回傳 send 的 coroutine（需 wait=True 才有 message）
    """
    msg = await sender_coro_factory()
    try:
        if hasattr(view, "message"):
            view.message = msg
    except Exception:
        pass
    return msg


# ════════════════════════════════════════════════════════════════
#  v2.2.0：互動 View 過期處理 mixin
#  timeout 後 disable 所有非 link 元件 + 編輯訊息提示「已過期」
# ════════════════════════════════════════════════════════════════
class ExpiringViewMixin:
    message = None   # 發送後請設 view.message = <Message>

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 第一次互動時記錄 message，讓 on_timeout 能 edit
        if getattr(self, "message", None) is None:
            try:
                self.message = interaction.message
            except Exception:
                pass
        return True

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style == discord.ButtonStyle.link:
                continue
            item.disabled = True
        msg = getattr(self, "message", None)
        if msg is not None:
            try:
                await msg.edit(
                    content="這個選單已過期喵，牢大請重新下指令。",
                    view=self,
                )
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════
#  /提醒醒 View
# ════════════════════════════════════════════════════════════════
class ReminderView(discord.ui.View):
    def __init__(self, repo: BaseRepository,
                 user_id: int, channel_id: int, task: str):
        super().__init__(timeout=180)
        self.repo, self.user_id, self.channel_id, self.task = repo, user_id, channel_id, task
        self.am_pm: Optional[str] = None
        self.hour:  Optional[int] = None
        self.minute: Optional[int] = None

    @discord.ui.select(
        placeholder="① 上午還是下午？",
        options=[
            discord.SelectOption(label="上午（00:00 ~ 11:59）", value="AM", emoji="🌅"),
            discord.SelectOption(label="下午（12:00 ~ 23:59）", value="PM", emoji="🌇"),
        ],
    )
    async def s_period(self, interaction, select):
        self.am_pm = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="② 幾點？",
        options=[discord.SelectOption(label=f"{i} 點", value=str(i)) for i in range(1, 13)],
    )
    async def s_hour(self, interaction, select):
        self.hour = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="③ 幾分？",
        options=[discord.SelectOption(label=f"{m:02d} 分", value=str(m)) for m in [0, 15, 30, 45]],
    )
    async def s_min(self, interaction, select):
        self.minute = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="✅ 確認設定", style=discord.ButtonStyle.success, row=4)
    async def confirm(self, interaction, button):
        if self.am_pm is None or self.hour is None or self.minute is None:
            await interaction.response.send_message(
                "牢大～三個選單都要選完才能確認喵！", ephemeral=True,
            )
            return
        if self.am_pm == "PM":
            h24 = self.hour if self.hour == 12 else self.hour + 12
        else:
            h24 = 0 if self.hour == 12 else self.hour
        now = datetime.now()
        target = now.replace(hour=h24, minute=self.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        try:
            await self.repo.add_reminder(ReminderEntry(
                user_id=self.user_id, channel_id=self.channel_id,
                task=self.task, target_time=target,
            ))
        except Exception as e:
            await interaction.response.send_message(
                embed=make_embed("出錯了喵...",
                    f"存資料庫時出錯了，牢大稍後再試。\n錯誤：{str(e)[:60]}",
                    COLOR_ERROR),
                ephemeral=True,
            )
            return

        delta = target - now
        h, r = divmod(int(delta.total_seconds()), 3600)
        m = r // 60
        countdown = f"{h}小時{m}分鐘" if h else f"{m}分鐘"

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=make_embed(
                "⏰ 排程已掛載！",
                f"事項：**{self.task}**\n"
                f"時間：**{target.strftime('%Y-%m-%d %H:%M')}**\n"
                f"還有 **{countdown}** 喵♡",
                COLOR_REMINDER,
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="❌ 取消", style=discord.ButtonStyle.danger, row=4)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="取消設定喵～", view=self, embed=None,
        )
        self.stop()


# ════════════════════════════════════════════════════════════════
#  /搜尋歌曲、/歌姬啦 中信心情境用：選歌 View
# ════════════════════════════════════════════════════════════════
# v2.3.0：「找附近哪裡有賣」= 純 Google Maps 外開
#   按「🔍 找附近哪裡有賣」→ 兩個按鈕：
#     1. 🗺️ 打開 Google Maps （Link Button，不打任何 API）
#     2. ❌ 不需要
#   已移除：我輸入位置 / 地址 Modal / 半徑 / Places / Geocoding
# ════════════════════════════════════════════════════════════════
class FindNearbyView(ExpiringViewMixin, discord.ui.View):
    """抽到食物 / 飲料後的「🔍 找附近哪裡有賣」入口按鈕。"""
    def __init__(self, cog: 'MainCog', *, display_name: str, maps_query: str):
        super().__init__(timeout=300)
        self.cog          = cog
        self.display_name = display_name
        self.maps_query   = maps_query

    @discord.ui.button(label="🔍 找附近哪裡有賣", style=discord.ButtonStyle.primary)
    async def find_nearby(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        view = LocationChoiceView(
            self.cog, display_name=self.display_name, maps_query=self.maps_query,
        )
        await interaction.response.send_message(
            embed=make_embed(
                "📍 需要位置才能找",
                "牢大，需要本喵幫你找嗎？",
                COLOR_FOOD,
            ),
            view=view,
            ephemeral=True,
        )
        button.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


class LocationChoiceView(ExpiringViewMixin, discord.ui.View):
    """
    找附近選擇框（v2.3.0 簡化）：
       1. 🗺️ 打開 Google Maps → Link Button（不打 API）
       2. ❌ 不需要
    """
    def __init__(self, cog: 'MainCog', *, display_name: str, maps_query: str):
        super().__init__(timeout=300)
        self.cog          = cog
        self.display_name = display_name
        self.maps_query   = maps_query

        # 按鈕 1: 打開 Google Maps（URL Link Button）
        maps_url = build_google_maps_search_url(maps_query)
        btn_maps = discord.ui.Button(
            label="🗺️ 打開 Google Maps",
            style=discord.ButtonStyle.link,
            url=maps_url,
            row=0,
        )
        self.add_item(btn_maps)

        # 按鈕 2: 不需要
        btn_no = discord.ui.Button(
            label="❌ 不需要",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        btn_no.callback = self._refuse
        self.add_item(btn_no)

    async def _refuse(self, interaction: discord.Interaction):
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
        await interaction.response.edit_message(
            content="哼！那牢大自己想辦法吧~本喵也幫不了你",
            embed=None,
            view=self,
        )
        self.stop()



class VoiceChannelPickerView(ExpiringViewMixin, discord.ui.View):
    """
    多語音頻道選擇 select menu（v2.1.7 新增，規格 4.2）。
    使用者不在語音頻道、且伺服器多個語音頻道都有人時用這個讓使用者選。
    """
    def __init__(self, cog: 'MainCog', channels: list,
                 requester: discord.Member,
                 pending_song: dict, insert_front: bool = False):
        super().__init__(timeout=120)
        self.cog          = cog
        self.channels     = channels   # list[discord.VoiceChannel]
        self.requester    = requester
        self.pending_song = pending_song
        self.insert_front = insert_front
        self._build_select()

    def _build_select(self):
        options = []
        for i, ch in enumerate(self.channels[:25]):  # Discord 上限 25
            options.append(discord.SelectOption(
                label=f"{ch.name}",
                description=f"{len(ch.members)} 人在裡面",
                value=str(i),
            ))
        select = discord.ui.Select(
            placeholder="選一個語音頻道讓本喵加入～（120 秒內）",
            options=options, min_values=1, max_values=1,
        )

        async def on_select(interaction: discord.Interaction):
            try:
                if interaction.user.id != self.requester.id:
                    await interaction.response.send_message(
                        "這是別人的選單喵！", ephemeral=True,
                    )
                    return
                idx = int(select.values[0])
                target = self.channels[idx]
                for item in self.children:
                    item.disabled = True
                await interaction.response.edit_message(
                    content=f"✅ 已選擇：**{target.name}**", view=self,
                )
                # 呼叫 _add_and_play，但強制指定 voice_channel
                await self.cog._add_and_play(
                    guild=interaction.guild,
                    channel=interaction.channel,
                    song_dict=self.pending_song,
                    requester=self.requester,
                    via_followup=False,
                    forced_voice_channel=target,
                    insert_front=self.insert_front,
                )
                self.stop()
            except Exception as e:
                print(f"[VoiceChannelPicker] on_select 失敗：{e}")
                traceback.print_exc()
                try:
                    await interaction.channel.send(embed=make_embed(
                        "出錯了喵...",
                        f"加入語音頻道失敗，牢大稍後再試。\n錯誤：{str(e)[:80]}",
                        COLOR_ERROR,
                    ))
                except Exception:
                    pass

        select.callback = on_select
        self.add_item(select)


class LocationPickerView(ExpiringViewMixin, discord.ui.View):
    """
    /天氣 多候選地點 select menu（v2.1.6 新增）。
    使用者選完後直接呼叫 fetch_overseas_weather。
    """
    def __init__(self, cog: 'MainCog', candidates, requester: discord.Member):
        super().__init__(timeout=120)
        self.cog        = cog
        self.candidates = candidates
        self.requester  = requester
        self._build_select()

    def _build_select(self):
        options = []
        for i, c in enumerate(self.candidates[:5]):
            label = c.display()[:90]
            options.append(discord.SelectOption(
                label=f"{i+1}. {label}",
                description=f"lat {c.latitude:.2f}, lng {c.longitude:.2f}"[:100],
                value=str(i),
            ))
        select = discord.ui.Select(
            placeholder="選一個地點喵～（120 秒內）",
            options=options, min_values=1, max_values=1,
        )

        async def on_select(interaction: discord.Interaction):
            try:
                if interaction.user.id != self.requester.id:
                    await interaction.response.send_message(
                        "這是別人的天氣選單喵！", ephemeral=True,
                    )
                    return
                idx = int(select.values[0])
                cand = self.candidates[idx]

                for item in self.children:
                    item.disabled = True
                await interaction.response.edit_message(
                    content=f"⏳ 查詢中：**{cand.display()[:80]}**",
                    view=self,
                )

                text = await self.cog.weather.fetch_overseas_weather(cand)
                await interaction.channel.send(embed=make_embed(
                    "☀️ 天氣查詢", text, COLOR_WEATHER,
                ))
                self.stop()
            except Exception as e:
                print(f"[LocationPicker] on_select 失敗：{e}")
                traceback.print_exc()
                try:
                    await interaction.channel.send(embed=make_embed(
                        "出錯了喵...",
                        f"查詢天氣失敗，牢大稍後再試。\n錯誤：{str(e)[:80]}",
                        COLOR_ERROR,
                    ))
                except Exception:
                    pass

        select.callback = on_select
        self.add_item(select)


class SongPickerView(ExpiringViewMixin, discord.ui.View):
    """
    v2.1.8 重寫：10 個結果分 2 頁（每頁 5 個）+ 翻頁按鈕。
    """

    def __init__(self, cog, results, requester, guild_id, insert_front=False):
        super().__init__(timeout=120)
        self.cog          = cog
        self.results      = results[:15]
        self.requester    = requester
        self.guild_id     = guild_id
        self.insert_front = insert_front
        self.current_page = 0
        self.max_page     = (len(self.results) - 1) // 5 if self.results else 0  # 0,1,2 → 最多 3 頁
        self._build()

    def _build(self):
        self.clear_items()
        self._add_select()
        if self.max_page > 0:
            self._add_nav_buttons()
        self._add_cancel_button()

    def _add_select(self):
        start = self.current_page * 5
        end = start + 5
        page_results = self.results[start:end]

        options = []
        for i, r in enumerate(page_results):
            real_idx = start + i
            title = (r.get("title") or "未知曲目")[:90]
            uploader = (r.get("uploader") or "未知頻道")[:35]
            views = r.get("view_count") or 0
            dur_str = format_duration(r.get("duration"))

            desc_parts = [uploader, dur_str]
            if views > 10_000_000:
                desc_parts.append(f"{views//10_000_000:d}千萬+")
            elif views > 10_000:
                desc_parts.append(f"{views//10_000:d}萬")

            options.append(discord.SelectOption(
                label=f"{real_idx+1}. {title}",
                description=" · ".join(desc_parts)[:100],
                value=str(real_idx),
            ))

        placeholder = f"挑一首喵～（第 {self.current_page + 1} / {self.max_page + 1} 頁，120 秒內）"
        select = discord.ui.Select(
            placeholder=placeholder, options=options,
            min_values=1, max_values=1, row=0,
        )
        select.callback = self._on_song_select
        self.add_item(select)

    def _add_nav_buttons(self):
        prev_btn = discord.ui.Button(
            label="◀ 上一頁", style=discord.ButtonStyle.secondary,
            disabled=(self.current_page == 0), row=1,
        )
        async def _go_prev(interaction):
            if interaction.user.id != self.requester.id:
                await interaction.response.send_message("這是別人的選單喵！", ephemeral=True)
                return
            self.current_page = max(0, self.current_page - 1)
            self._build()
            await interaction.response.edit_message(view=self)
        prev_btn.callback = _go_prev
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="下一頁 ▶", style=discord.ButtonStyle.secondary,
            disabled=(self.current_page >= self.max_page), row=1,
        )
        async def _go_next(interaction):
            if interaction.user.id != self.requester.id:
                await interaction.response.send_message("這是別人的選單喵！", ephemeral=True)
                return
            self.current_page = min(self.max_page, self.current_page + 1)
            self._build()
            await interaction.response.edit_message(view=self)
        next_btn.callback = _go_next
        self.add_item(next_btn)

    def _add_cancel_button(self):
        cancel = discord.ui.Button(
            label="✖ 取消", style=discord.ButtonStyle.danger, row=1,
        )
        async def _cancel(interaction):
            if interaction.user.id != self.requester.id:
                await interaction.response.send_message("這是別人的選單喵！", ephemeral=True)
                return
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content="❎ 取消選歌喵～", view=self)
            self.stop()
        cancel.callback = _cancel
        self.add_item(cancel)

    async def _on_song_select(self, interaction):
        try:
            if interaction.user.id != self.requester.id:
                await interaction.response.send_message(
                    "這是別人的選歌介面喵！自己開一個指令啦～", ephemeral=True,
                )
                return

            select = None
            for item in self.children:
                if isinstance(item, discord.ui.Select):
                    select = item
                    break
            if not select or not select.values:
                return

            idx = int(select.values[0])
            picked = self.results[idx]

            print(f"\n[SongPicker] 使用者選了第 {idx+1} 首")
            print(f"             title    = {picked.get('title')}")
            print(f"             url      = {picked.get('webpage_url')}")

            for item in self.children:
                item.disabled = True

            await interaction.response.edit_message(
                content=f"⏳ 驗證中：**{(picked.get('title') or '未知')[:80]}**...",
                view=self,
            )

            ok, reason = await self.cog.music.verify_playable(picked)
            if not ok:
                print(f"[SongPicker] 驗證失敗：{reason}")
                await interaction.edit_original_response(
                    content=f"❌ 跳過：**{(picked.get('title') or '未知')[:60]}**",
                )
                await interaction.channel.send(embed=make_embed(
                    "出錯了喵...",
                    f"這個結果看起來不能播放（{reason}），牢大換一個版本試試。",
                    COLOR_ERROR,
                ))
                self.stop()
                return

            await interaction.edit_original_response(
                content=f"✅ 已選擇：**{(picked.get('title') or '未知')[:80]}**",
            )

            await self.cog._add_and_play(
                guild=interaction.guild,
                channel=interaction.channel,
                song_dict=picked,
                requester=self.requester,
                via_followup=False,
                insert_front=self.insert_front,
            )
            self.stop()
        except Exception as e:
            print(f"[SongPicker] on_select 失敗：{e}")
            traceback.print_exc()
            try:
                await interaction.channel.send(embed=make_embed(
                    "出錯了喵...",
                    f"選歌之後出錯了，但佇列狀態沒受影響。\n錯誤：{str(e)[:80]}",
                    COLOR_ERROR,
                ))
            except Exception:
                pass



# ════════════════════════════════════════════════════════════════
#  主 Cog
# ════════════════════════════════════════════════════════════════
class MainCog(commands.Cog):

    # ⭐ v2.2.0：5 個 command group，註冊順序依規格七：
    #   /巴豆妖 → /查詢 → /音樂 → /工具 → /黑優浦蜜
    eat_g    = app_commands.Group(name="巴豆妖", description="🍱 隨機抽菜 / 飲料 / 找餐廳")
    query_g  = app_commands.Group(name="查詢",   description="🔎 天氣 / 翻譯")
    music_g  = app_commands.Group(name="音樂",   description="🎵 音樂播放與佇列")
    tool_g   = app_commands.Group(name="工具",   description="🧰 計算 / 隨機 / 提醒")
    system_g = app_commands.Group(name="黑優浦蜜", description="🐾 系統指令")

    def __init__(self, bot: commands.Bot,
                 music: MusicEngine, weather: WeatherEngine,
                 food: FoodEngine, translate: TranslateEngine,
                 utility_ai: UtilityAI, repo: BaseRepository,
                 cache: RedisCache):
        super().__init__()
        self.bot       = bot
        self.music     = music
        self.weather   = weather
        self.food      = food
        self.translate = translate
        self.ai        = utility_ai
        self.repo      = repo
        self.cache     = cache
        self.qm        = QueueManager()

    async def cog_load(self):
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    # ── 統一格式的 State Log ──
    def _state_log(self, guild_id: int, action: str, song_title: str = "",
                    song_url: str = "", extra: str = ""):
        """規格 III 要求的 9 點 State Log，集中格式"""
        q = self.qm.get_or_create(guild_id)
        vc_connected  = q.vc is not None and q.vc.is_connected() if q.vc else False
        vc_is_playing = q.vc.is_playing() if (q.vc and vc_connected) else False
        vc_is_paused  = q.vc.is_paused()  if (q.vc and vc_connected) else False
        current_title = q.current.title if q.current else "<None>"

        print(
            f"[Music State] guild={guild_id} action={action}\n"
            f"              current_song = {current_title}\n"
            f"              queue_len    = {len(q)}\n"
            f"              vc_connected = {vc_connected}\n"
            f"              vc_is_playing= {vc_is_playing}\n"
            f"              vc_is_paused = {vc_is_paused}"
            + (f"\n              song_title   = {song_title}" if song_title else "")
            + (f"\n              song_url     = {song_url}" if song_url else "")
            + (f"\n              {extra}" if extra else "")
        )

    # ════════════════════════════════════════════════════════
    #  🎵 音樂播放：單一進入點（v2.1.7 加多語音頻道 + 防重複 + 插播）
    # ════════════════════════════════════════════════════════
    async def _select_voice_channel(self, guild: discord.Guild,
                                     requester: discord.Member) -> tuple:
        """
        多語音頻道處理（規格 4.2）。
        回傳 (kind, value):
            ("found", channel)             → 找到唯一頻道直接用
            ("picker", [channels])         → 多個有人 → 用 view 讓使用者選
            ("none", None)                 → 沒人在語音頻道
        """
        # 規則 1：使用者在語音 → 用他的
        if requester.voice and requester.voice.channel:
            return ("found", requester.voice.channel)

        # 規則 2/3/C：使用者不在，看伺服器哪些語音頻道有人
        occupied = []
        for ch in guild.voice_channels:
            members = [m for m in ch.members if not m.bot]
            if members:
                occupied.append(ch)

        if len(occupied) == 0:
            return ("none", None)
        if len(occupied) == 1:
            return ("found", occupied[0])
        return ("picker", occupied)

    async def _connect_or_reuse_voice(self, guild: discord.Guild,
                                       q, target_channel) -> tuple[bool, str]:
        """
        防重複連線（規格 4.3）。
        回傳 (ok, message_if_not_ok)
        """
        # 規則 1: 已在同頻道
        if q.vc and q.vc.is_connected() and q.vc.channel == target_channel:
            return (True, "")

        # 規則 2: 已在其他頻道 + 正在播 / 佇列不空
        if q.vc and q.vc.is_connected() and q.vc.channel != target_channel:
            if q.vc.is_playing() or q.vc.is_paused() or q.current or len(q) > 0:
                return (False,
                    f"黑優浦蜜目前正在「{q.vc.channel.name}」唱歌喵。\n"
                    f"如果要換頻道，請先用 `/音樂 停止`，或請管理員操作。")

            # 規則 3: 已在其他頻道但 idle + 佇列空 → move_to
            try:
                await q.vc.move_to(target_channel)
                return (True, "")
            except Exception as e:
                return (False, f"move_to 失敗：{str(e)[:60]}")

        # 規則 4: 完全沒連線
        try:
            q.vc = await target_channel.connect()
            return (True, "")
        except Exception as e:
            return (False, f"連到語音頻道失敗：{str(e)[:60]}")

    async def _add_and_play(self, *,
                             guild: discord.Guild,
                             channel: discord.abc.Messageable,
                             song_dict: dict,
                             requester: discord.Member,
                             via_followup: bool = False,
                             interaction: Optional[discord.Interaction] = None,
                             forced_voice_channel=None,
                             insert_front: bool = False):
        """
        音樂播放單一入口。新參數（v2.1.7）：
            forced_voice_channel : 強制使用某語音頻道（多頻道選擇後用）
            insert_front         : True 時走「插播」，把歌塞到佇列最前面
        """
        song_title = song_dict.get('title') or '未知曲目'
        song_url   = song_dict.get('webpage_url') or song_dict.get('url') or ''

        self._state_log(guild.id, "_add_and_play:enter",
                         song_title=song_title, song_url=song_url,
                         extra=f"requester={requester.id} insert_front={insert_front}")

        q = self.qm.get_or_create(guild.id)

        # ── Step 1：決定要連哪個語音頻道 ──
        if forced_voice_channel is not None:
            target_channel = forced_voice_channel
        else:
            kind, value = await self._select_voice_channel(guild, requester)
            if kind == "none":
                await self._send_msg(channel, interaction, via_followup, make_embed(
                    "出錯了喵...",
                    "牢大目前不在語音頻道，也找不到有人正在聊天的語音頻道。\n"
                    "請先進語音頻道，或選一個語音頻道讓本小姐去唱歌。",
                    COLOR_ERROR,
                ))
                return
            if kind == "picker":
                # 多個語音頻道有人 → 顯示 select menu
                await self._send_msg(channel, interaction, via_followup,
                    make_embed(
                        "🎤 選一個語音頻道",
                        "請選擇要讓黑優浦蜜加入的語音頻道：",
                        COLOR_MUSIC,
                    ),
                )
                # 用 channel.send 再發 view（避免 interaction 二次 send）
                try:
                    await channel.send(view=VoiceChannelPickerView(
                        self, value, requester, song_dict, insert_front,
                    ))
                except Exception as e:
                    print(f"[Music] 發 VoiceChannelPickerView 失敗：{e}")
                return
            target_channel = value

        # ── Step 2：防重複連線 ──
        ok, msg = await self._connect_or_reuse_voice(guild, q, target_channel)
        if not ok:
            await self._send_msg(channel, interaction, via_followup, make_embed(
                "🚫 無法切換語音頻道", msg, COLOR_ERROR,
            ))
            return

        # ── Step 3：在 q.add 之前判斷 idle ──
        idle_before_add = q.is_idle()
        self._state_log(guild.id, "before_enqueue",
                         song_title=song_title, song_url=song_url,
                         extra=f"idle_before_add={idle_before_add} insert_front={insert_front}")

        # ── Step 4：加入佇列（插播 vs 一般）──
        queued = QueuedSong.from_dict(song_dict, requester.id, requester.display_name)
        if insert_front and not idle_before_add:
            # 插播：塞最前面（但只在「已經有歌在播」時才有意義）
            added = q.add_front(queued)
        else:
            added = q.add(queued)
        if not added:
            await self._send_msg(channel, interaction, via_followup, make_embed(
                "佇列已滿喵...",
                f"目前佇列已達上限（{Config.MUSIC_QUEUE_MAX}首），請稍後再點歌。",
                COLOR_ERROR,
            ))
            return

        self._state_log(guild.id, "after_enqueue",
                         song_title=song_title, song_url=song_url,
                         extra=f"queue_len={len(q)}")

        # ── Step 5：通知 ──
        if idle_before_add:
            embed = make_embed(
                "▶️ 開始播放",
                f"**{queued.title}**\n"
                f"🎤 {queued.uploader}　⏱️ {queued.duration_str()}　📡 {queued.display_source}\n"
                f"點歌人：{requester.mention}",
                COLOR_MUSIC,
            )
        elif insert_front:
            embed = make_embed(
                "⏭️ 已插播到佇列最前面",
                f"**{queued.title}**\n"
                f"🎤 {queued.uploader}　⏱️ {queued.duration_str()}　📡 {queued.display_source}\n"
                f"點歌人：{requester.mention}\n\n"
                f"_目前正在播放：**{q.current.title if q.current else '?'}**_\n"
                f"_目前歌曲播完後會接著播放這首_",
                COLOR_MUSIC,
            )
        else:
            current_info = ""
            if q.current:
                current_info = (
                    f"\n\n_目前正在播放：**{q.current.title}**_\n"
                    f"_點歌人：{q.current.requester_name}_"
                )
            embed = make_embed(
                "📥 已加入佇列",
                f"**{queued.title}**\n"
                f"🎤 {queued.uploader}　⏱️ {queued.duration_str()}　📡 {queued.display_source}\n"
                f"點歌人：{requester.mention}"
                f"{current_info}",
                COLOR_MUSIC,
            )

        if queued.thumbnail:
            embed.set_thumbnail(url=queued.thumbnail)
        await self._send_msg(channel, interaction, via_followup, embed)

        # ── Step 6：開播 ──
        if idle_before_add:
            self._state_log(guild.id, "before_start_next",
                             song_title=song_title, song_url=song_url)
            await self._start_next(guild.id, channel)
        else:
            print(f"[Music] idle_before_add=False，已在播放中")

    # ── 內部：統一發訊息（不管走 interaction 還是 channel） ──
    async def _send_msg(self, channel, interaction, via_followup, embed):
        try:
            if via_followup and interaction:
                await interaction.followup.send(embed=embed)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            print(f"[Music] 發訊息失敗：{e}")

    # ════════════════════════════════════════════════════════
    #  🎵 _start_next：從佇列取下一首並播放
    # ════════════════════════════════════════════════════════
    async def _start_next(self, guild_id: int,
                           channel: Optional[discord.abc.Messageable] = None):
        """從佇列取出下一首並播放。失敗會跳過繼續試下一首。"""
        q = self.qm.get_or_create(guild_id)

        self._state_log(guild_id, "_start_next:enter")

        if not q.vc or not q.vc.is_connected():
            print(f"[Music] _start_next: vc 沒連線，退出")
            return

        # ⭐ 規格：避免 race condition，如果已經在播放就不要重複觸發
        if q.vc.is_playing() or q.current is not None:
            print(f"[Music] _start_next: 已在播放中（current={q.current.title if q.current else None}），跳過")
            return

        # ⭐ 迴圈式：連續嘗試直到有一首能播，或佇列清空（疑難雜症 7.10）
        while True:
            next_song = q.pop_next()
            if not next_song:
                print(f"[Music] _start_next: 佇列空了，停止播放")
                q.current = None
                # ⭐ v2.2.0：佇列自然清空 → 輸出本回合歌單
                await self._send_session_playlist(guild_id, channel)
                return

            self._state_log(guild_id, "_start_next:pop_next",
                             song_title=next_song.title, song_url=next_song.webpage_url)

            # ⭐ 解析可播放串流：原版 → 同曲 fallback（被擋也不會亂播別首歌）
            result = await self.music.resolve_playable_stream(next_song)

            if not result.get('ok'):
                cat = result.get('failure_category') or 'unknown'
                print(f"[Music] _start_next:「{next_song.title}」解析失敗 cat={cat} attempts={result.get('attempts')}")
                if channel:
                    try:
                        await channel.send(embed=make_embed(
                            "⏭️ 跳過一首",
                            self._music_fail_message(next_song.title, cat),
                            COLOR_ERROR,
                        ))
                    except Exception:
                        pass
                continue  # 試下一首

            stream_url = result['stream_url']

            # ⭐ fallback 換了版本 → 更新追蹤欄位 + 通知使用者（疑難雜症 P1.7）
            if result.get('fallback_used'):
                next_song.fallback_used = True
                next_song.fallback_reason = result.get('fallback_reason') or ''
                next_song.actual_played_title = result.get('actual_title') or next_song.title
                next_song.actual_played_url = result.get('actual_url') or next_song.webpage_url
                if channel:
                    try:
                        await channel.send(embed=make_embed(
                            "🔁 改播可用版本",
                            f"原本那個版本被 YouTube 擋住了，黑優浦蜜幫牢大找到同一首歌的可播放版本：\n"
                            f"**{result.get('actual_title')}**",
                            COLOR_MUSIC,
                        ))
                    except Exception:
                        pass

            # 建立音訊來源
            try:
                source = self.music.get_discord_audio_source(stream_url)
            except Exception as e:
                print(f"[Music] _start_next: 建立音訊來源失敗：{e}")
                traceback.print_exc()
                if channel:
                    try:
                        await channel.send(embed=make_embed(
                            "出錯了喵...",
                            f"「{next_song.title}」建立音訊來源失敗，跳過這首。",
                            COLOR_ERROR,
                        ))
                    except Exception:
                        pass
                continue

            q.current = next_song

            # after callback（用預設參數綁定 title，避免迴圈 late-binding）
            def _on_finish(error, _title=next_song.title):
                if error:
                    print(f"[Music] _on_finish: 播放結束（錯誤）：{error}")
                else:
                    print(f"[Music] _on_finish: 播放結束（正常）：{_title}")
                coro = self._handle_song_finished(guild_id, channel)
                try:
                    asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                except Exception as e:
                    print(f"[Music] _on_finish run_coroutine 失敗：{e}")

            try:
                q.vc.play(source, after=_on_finish)
                self._state_log(guild_id, "ffmpeg_started",
                                 song_title=next_song.title)
                print(f"[Music] ✅ 開始播放：{result.get('actual_title') or next_song.title}")
                # ⭐ v2.2.0：成功開始播放才記錄本回合歷史
                q.record_played(next_song)
                return
            except Exception as e:
                print(f"[Music] q.vc.play() 失敗：{e}")
                traceback.print_exc()
                q.current = None
                continue

    # ════════════════════════════════════════════════════════
    #  🎵 音樂失敗訊息（依 YouTube 錯誤分類給友善提示，疑難雜症 7.9）
    # ════════════════════════════════════════════════════════
    @staticmethod
    def _music_fail_message(title: str, category: str) -> str:
        if category == 'youtube_bot_check':
            return (f"「{title}」這個版本被 YouTube 要求驗證擋住了，"
                    f"黑優浦蜜也找不到同一首歌的可播放版本，先跳過喵～\n"
                    f"牢大可以補上歌手名稱、或直接貼 YouTube 連結再試一次。")
        if category in ('video_unavailable', 'private_video', 'geo_blocked'):
            return f"「{title}」這個影片無法播放（不可用 / 私人 / 地區限制），跳過喵～"
        if category in ('no_candidate', 'no_playable_format', 'extract_failed'):
            return (f"黑優浦蜜找不到夠吻合「{title}」又能播放的版本，"
                    f"為了避免播錯，先跳過。牢大可以換關鍵字或貼 YouTube 連結。")
        return f"「{title}」無法取得音源，跳過喵～"

    # ════════════════════════════════════════════════════════
    #  🎵 本回合歌單輸出（v2.2.0 規格六）
    # ════════════════════════════════════════════════════════
    async def _send_session_playlist(self, guild_id: int,
                                      channel: Optional[discord.abc.Messageable],
                                      reason: str = "natural"):
        """
        輸出本回合播放過的歌單。
        reason: 'natural'（自然播完）/ 'stop'（/音樂 停止）
        played_history 為空就不輸出（規格二答案）。
        """
        q = self.qm.get_or_create(guild_id)
        history = q.played_history
        if not history or channel is None:
            q.reset_history()
            return

        lines = []
        for i, item in enumerate(history, 1):
            title = item.get("title", "未知曲目")
            count = item.get("count", 1)
            if count > 1:
                lines.append(f"{i}. {title} ×{count} 次")
            else:
                lines.append(f"{i}. {title}")

        body = (
            "這次黑優浦蜜唱過：\n\n" + "\n".join(lines) +
            "\n\n牢大，歌單唱完了喵。"
        )
        try:
            await channel.send(embed=make_embed(
                "🎵 本回合歌單結束", body, COLOR_MUSIC,
            ))
        except Exception as e:
            print(f"[Music] 輸出本回合歌單失敗：{e}")
        finally:
            q.reset_history()

    # ════════════════════════════════════════════════════════
    #  🎵 _handle_song_finished：歌曲結束處理（含循環模式）
    # ════════════════════════════════════════════════════════
    async def _handle_song_finished(self, guild_id: int,
                                      channel: Optional[discord.abc.Messageable] = None):
        """歌結束時（after callback）：依循環模式決定下一步"""
        q = self.qm.get_or_create(guild_id)
        finished = q.current
        q.current = None

        self._state_log(guild_id, "_handle_song_finished:enter",
                         song_title=finished.title if finished else "<None>")

        # 依循環模式重排
        if finished and q.loop_mode == LoopMode.SINGLE:
            q._queue.appendleft(finished)
        elif finished and q.loop_mode == LoopMode.QUEUE:
            q._queue.append(finished)

        await self._start_next(guild_id, channel)

    # ════════════════════════════════════════════════════════
    #  /歌姬啦
    # ════════════════════════════════════════════════════════
    _SEARCH_TYPE_CHOICES = [
        app_commands.Choice(name="歌曲（預設）", value="song"),
        app_commands.Choice(name="歌手 / 樂團",   value="artist"),
    ]
    _SOURCE_CHOICES = [
        app_commands.Choice(name="自動（預設）",        value="auto"),
        app_commands.Choice(name="YouTube",          value="youtube"),
        app_commands.Choice(name="Spotify (metadata)", value="spotify"),
    ]

    @music_g.command(
        name="歌姬啦",
        description="🎵 播放或加入音樂佇列",
    )
    @app_commands.describe(
        關鍵字="YouTube 網址 / Spotify 網址 / 歌名 / 歌手",
        搜尋類型="純文字搜尋時用，預設『歌曲』",
        來源="預設『自動』：URL 自動判斷、純文字用 YouTube",
    )
    @app_commands.choices(搜尋類型=_SEARCH_TYPE_CHOICES, 來源=_SOURCE_CHOICES)
    async def play(self, interaction: discord.Interaction,
                   關鍵字: str,
                   搜尋類型: Optional[app_commands.Choice[str]] = None,
                   來源: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer()

        # FFmpeg 檢查
        if not self.music.ffmpeg_ok:
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                "FFmpeg 沒裝，本喵的喉嚨壞掉了。\n"
                "請參考 `.env.example` 設定 `FFMPEG_PATH` 後重啟。",
                COLOR_ERROR,
            ))
            return

        search_type = (搜尋類型.value if 搜尋類型 else "song")
        source      = (來源.value      if 來源     else "auto")

        kind = self.music.detect_input_type(關鍵字)
        print(f"\n[/歌姬啦] 關鍵字='{關鍵字}' 類型={search_type} 來源={source} detected={kind}")

        # ════════════════════════════════════════════════════
        #  URL 類型（無視搜尋類型/來源）
        # ════════════════════════════════════════════════════
        if kind == 'youtube_url':
            info = await self.music.resolve_youtube_url(關鍵字)
            if not info:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    "這個 YouTube 連結本喵搞不定，牢大檢查一下網址。",
                    COLOR_ERROR,
                ))
                return
            info['requested_query'] = 關鍵字
            await self._add_and_play(
                guild=interaction.guild, channel=interaction.channel,
                song_dict=info, requester=interaction.user,
                via_followup=True, interaction=interaction,
            )
            return

        if kind == 'spotify_url':
            if not self.music.spotify_available:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    "Spotify 目前還沒設定，牢大先用 YouTube 或直接貼 YouTube 網址。",
                    COLOR_ERROR,
                ))
                return
            results = await self.music.resolve_spotify(關鍵字)
            if not results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    "Spotify 連結解析失敗。牢大改貼 YouTube 連結或直接輸入曲名試試。",
                    COLOR_ERROR,
                ))
                return
            # Spotify 可能多首（playlist），全部加入
            first = True
            for item in results:
                await self._add_and_play(
                    guild=interaction.guild, channel=interaction.channel,
                    song_dict=item, requester=interaction.user,
                    via_followup=first, interaction=interaction if first else None,
                )
                first = False
            return

        # ════════════════════════════════════════════════════
        #  純文字
        # ════════════════════════════════════════════════════

        # 來源 = spotify 時走 Spotify 文字搜尋
        if source == "spotify":
            if not self.music.spotify_available:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    "Spotify 目前還沒設定，牢大先用 YouTube。",
                    COLOR_ERROR,
                ))
                return
            sp_results = await self.music.search_spotify_then_youtube(關鍵字, max_results=15)
            if not sp_results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    f"Spotify 找不到「{關鍵字}」相關結果，牢大試試別的關鍵字。",
                    COLOR_ERROR,
                ))
                return
            for r in sp_results:
                r['requested_query'] = 關鍵字
            # Spotify 文字搜尋永遠顯示 Top 5 讓使用者選
            await interaction.followup.send(
                embed=make_embed(
                    "🎵 Spotify 搜尋結果",
                    "牢大挑一首喵～\n_實際音源來自 YouTube_",
                    COLOR_MUSIC,
                ),
                view=SongPickerView(self, sp_results, interaction.user, interaction.guild_id),
            )
            return

        # 來源 = youtube 或 auto + 純文字
        if search_type == "artist":
            # ⭐ 規格 1+2：用 music_engine 統一的 top tracks 搜尋
            print(f"[/歌姬啦] 歌手模式 → search_top_tracks_for_artist({關鍵字!r})")
            results = await self.music.search_top_tracks_for_artist(
                關鍵字, max_results=15,
            )
            if not results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    f"找不到歌手「{關鍵字}」的任何可播放結果。\n"
                    f"牢大可以試試「歌手 + 歌名」，或直接貼 YouTube 網址。",
                    COLOR_ERROR,
                ))
                return
            print(f"[/歌姬啦] 歌手模式：{len(results)} 筆可播放結果")
            await interaction.followup.send(
                embed=make_embed(
                    f"🎤 歌手 / 樂團：{關鍵字}",
                    "牢大，本小姐找到幾個熱門版本，自己挑一首別再選擇障礙了。",
                    COLOR_MUSIC,
                ),
                view=SongPickerView(self, results, interaction.user, interaction.guild_id),
            )
            return

        # 歌曲模式（預設）
        await self._handle_song_search(interaction, 關鍵字)

    # ── 歌曲模式內部處理 ──
    # ⭐ v2.2.0：純文字一律顯示 15 個 / 3 頁，不做高信心直接播
    async def _handle_song_search(self, interaction: discord.Interaction, query: str):
        print(f"[/歌姬啦] _handle_song_search: query='{query}'")
        # 拉 25 個，過濾後保留 Top 15
        results = await self.music.search_youtube(query, max_results=25, scored=True)
        results = results[:15]
        # ⭐ 帶上使用者原始輸入，供 fallback 同曲驗證用（避免「遇見」播成「遇到」）
        for r in results:
            r['requested_query'] = query
        print(f"[/歌姬啦] 搜尋結果（過濾後）數量：{len(results)}")

        if not results:
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                f"本小姐找不到能播放的結果。\n"
                f"牢大可以試試「歌手 + 歌名」，或直接貼 YouTube 網址。",
                COLOR_ERROR,
            ))
            return

        await interaction.followup.send(
            embed=make_embed(
                "🔍 搜尋結果",
                "牢大，本小姐找到幾個版本，自己挑一首喵～（120 秒內）",
                COLOR_MUSIC,
            ),
            view=SongPickerView(self, results, interaction.user, interaction.guild_id),
        )

    # ════════════════════════════════════════════════════════
    #  /佇列（全欄位防呆）
    # ════════════════════════════════════════════════════════
    @music_g.command(name="佇列", description="📜 查看目前的音樂佇列")
    async def queue_cmd(self, interaction: discord.Interaction):
        try:
            q = self.qm.get_or_create(interaction.guild_id)
            if q.is_empty:
                await interaction.response.send_message(embed=make_embed(
                    "📭 佇列空空的", "目前沒有任何歌曲喵～用 `/歌姬啦` 來點歌！", COLOR_MUSIC,
                ))
                return

            lines = []
            if q.current:
                c = q.current
                lines.append(
                    f"🎶 **現正播放**\n"
                    f"> **{c.title or '未知曲目'}**\n"
                    f"> 來源：{c.display_source or '未知'}　"
                    f"點歌人：{c.requester_name or '匿名'}　"
                    f"⏱️ {c.duration_str()}"
                )

            upcoming = q.upcoming
            if upcoming:
                lines.append("\n📜 **接下來**")
                for i, s in enumerate(upcoming[:10], 1):
                    title    = (s.title or '未知曲目')[:55]
                    source   = s.display_source or '未知'
                    req_name = s.requester_name or '匿名'
                    lines.append(
                        f"**{i}.** {title}\n"
                        f"　來源：{source}　點歌人：{req_name}　⏱️ {s.duration_str()}"
                    )
                if len(upcoming) > 10:
                    lines.append(f"\n_...還有 {len(upcoming) - 10} 首未顯示_")

            lines.append(
                f"\n📊 共 **{q.total}** 首　"
                f"循環模式：{q.loop_mode.emoji()} {q.loop_mode.display_name()}"
            )

            await interaction.response.send_message(embed=make_embed(
                "🎼 音樂佇列", "\n".join(lines), COLOR_MUSIC,
            ))
        except Exception as e:
            print(f"[/佇列] 顯示失敗：{e}")
            traceback.print_exc()
            try:
                await interaction.response.send_message(embed=make_embed(
                    "出錯了喵...",
                    f"顯示佇列時出錯，但播放本身沒受影響。\n錯誤：{str(e)[:60]}",
                    COLOR_ERROR,
                ), ephemeral=True)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════
    #  /現正播放（全欄位防呆）
    # ════════════════════════════════════════════════════════
    @music_g.command(name="現正播放", description="🎶 查看目前播放中的歌曲")
    async def now_playing(self, interaction: discord.Interaction):
        try:
            q = self.qm.get_or_create(interaction.guild_id)
            if not q.current:
                await interaction.response.send_message(embed=make_embed(
                    "🔇 沒在播", "目前沒有播放任何歌曲喵～", COLOR_MUSIC,
                ))
                return

            s = q.current
            title    = s.title          or '未知曲目'
            uploader = s.uploader       or '未知頻道'
            source   = s.display_source or '未知'
            url      = s.webpage_url    or ''
            req_name = s.requester_name or '匿名'

            link_text = f"\n🔗 [連結]({url})" if url else ""

            embed = make_embed(
                "🎵 現正播放",
                f"**{title}**\n"
                f"🎤 {uploader}　⏱️ {s.duration_str()}　📡 {source}\n"
                f"點歌人：<@{s.requester_id}>（{req_name}）\n"
                f"循環模式：{q.loop_mode.emoji()} {q.loop_mode.display_name()}"
                f"{link_text}",
                COLOR_MUSIC,
            )
            if s.thumbnail:
                embed.set_thumbnail(url=s.thumbnail)
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            print(f"[/現正播放] 顯示失敗：{e}")
            traceback.print_exc()
            try:
                await interaction.response.send_message(embed=make_embed(
                    "出錯了喵...",
                    f"顯示時出錯，但播放本身沒受影響。\n錯誤：{str(e)[:60]}",
                    COLOR_ERROR,
                ), ephemeral=True)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════
    #  /跳過（管理員 + 點歌者）
    # ════════════════════════════════════════════════════════
    @music_g.command(name="跳過", description="⏭️ 跳過目前歌曲（管理員或點歌者可用）")
    async def skip(self, interaction: discord.Interaction):
        q = self.qm.get_or_create(interaction.guild_id)
        if not q.current or not q.vc or not q.vc.is_playing():
            await interaction.response.send_message(embed=make_embed(
                "🔇 沒在播", "目前沒有歌可以跳過喵～", COLOR_MUSIC,
            ), ephemeral=True)
            return

        is_requester = (interaction.user.id == q.current.requester_id)
        if not is_admin(interaction.user) and not is_requester:
            await interaction.response.send_message(embed=make_embed(
                "🚫 沒有權限",
                f"只有管理員或點歌者（{q.current.requester_name}）可以跳過這首歌喵～",
                COLOR_ERROR,
            ), ephemeral=True)
            return

        title = q.current.title
        q.vc.stop()
        await interaction.response.send_message(embed=make_embed(
            "⏭️ 已跳過", f"跳過了：**{title}** 喵♡", COLOR_MUSIC,
        ))

    # ════════════════════════════════════════════════════════
    #  /循環（v2.1.4：加模式參數）
    # ════════════════════════════════════════════════════════
    _LOOP_MODE_CHOICES = [
        app_commands.Choice(name="關閉",     value="off"),
        app_commands.Choice(name="單曲循環", value="single"),
        app_commands.Choice(name="歌單循環", value="queue"),
    ]

    @music_g.command(
        name="循環",
        description="🎵 設定循環模式（關閉 / 單曲 / 歌單）",
    )
    @app_commands.describe(模式="不選的話，會在 關閉 → 單曲 → 歌單 之間循環切換")
    @app_commands.choices(模式=_LOOP_MODE_CHOICES)
    async def loop_cmd(self, interaction: discord.Interaction,
                       模式: Optional[app_commands.Choice[str]] = None):
        try:
            q = self.qm.get_or_create(interaction.guild_id)
            if 模式 is None:
                # 沒指定模式 → 切換到下一個（向後相容）
                new_mode = q.cycle_loop_mode()
            else:
                # 直接設定到指定模式
                new_mode = LoopMode(模式.value)
                q.loop_mode = new_mode

            print(f"[Music] loop_mode 切換 → {new_mode.value} ({new_mode.display_name()})")

            await interaction.response.send_message(embed=make_embed(
                "🔁 循環模式",
                f"目前模式：{new_mode.emoji()} **{new_mode.display_name()}** 喵♡\n\n"
                f"{self._loop_mode_explanation(new_mode)}",
                COLOR_MUSIC,
            ))
        except Exception as e:
            print(f"[/循環] 失敗：{e}")
            traceback.print_exc()
            await interaction.response.send_message(embed=make_embed(
                "出錯了喵...",
                f"切換循環模式時出錯。\n錯誤：{str(e)[:60]}",
                COLOR_ERROR,
            ), ephemeral=True)

    @staticmethod
    def _loop_mode_explanation(mode: LoopMode) -> str:
        return {
            LoopMode.OFF:    "_播完後播下一首，佇列空就停。_",
            LoopMode.SINGLE: "_目前這首播完會再重播同一首，不會消耗佇列。_",
            LoopMode.QUEUE:  "_播完後會把這首放到佇列尾端，依序輪播。_",
        }[mode]

    # ════════════════════════════════════════════════════════
    #  /清空佇列（管理員限定）
    # ════════════════════════════════════════════════════════
    @music_g.command(name="清空佇列", description="🗑️ 清空整個音樂佇列（管理員限定）")
    async def clear_queue(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(embed=make_embed(
                "🚫 沒有權限",
                "只有伺服器管理員可以清空音樂佇列。",
                COLOR_ERROR,
            ), ephemeral=True)
            return
        q = self.qm.get_or_create(interaction.guild_id)
        count = len(q)
        q.clear()
        await interaction.response.send_message(embed=make_embed(
            "🧹 佇列已清空",
            f"清掉了 **{count}** 首佇列裡的歌喵～\n（目前播的不會被中斷）",
            COLOR_MUSIC,
        ))

    # ════════════════════════════════════════════════════════
    #  /停止
    # ════════════════════════════════════════════════════════
    @music_g.command(name="停止", description="⏹️ 停止音樂並離開語音")
    async def stop(self, interaction: discord.Interaction):
        q = self.qm.get_or_create(interaction.guild_id)
        if q.vc and q.vc.is_connected():
            if q.vc.is_playing():
                q.vc.stop()
            await q.vc.disconnect()
            q.vc = None
            q.current = None
            q.clear()
            await interaction.response.send_message(embed=make_embed(
                "⏹️ 已停止", "音樂停止，本喵下線囉～喵♡", COLOR_MUSIC,
            ))
            # ⭐ v2.2.0：停止時輸出本回合歌單（只列已播過的，不列 queue）
            await self._send_session_playlist(
                interaction.guild_id, interaction.channel, reason="stop",
            )
        else:
            await interaction.response.send_message(embed=make_embed(
                "❓ 本喵沒在播", "本來就沒在播音樂喵～", COLOR_MUSIC,
            ), ephemeral=True)

    # ════════════════════════════════════════════════════════
    #  /音樂 插播（v2.1.7 新增 - 規格 4.1）
    # ════════════════════════════════════════════════════════
    @music_g.command(
        name="插播",
        description="🎵 把歌曲插到佇列最前面（不打斷目前在播的歌）",
    )
    @app_commands.describe(
        關鍵字="YouTube 網址 / Spotify 網址 / 歌名",
        搜尋類型="純文字搜尋時用，預設『歌曲』",
        來源="預設『自動』",
    )
    @app_commands.choices(搜尋類型=_SEARCH_TYPE_CHOICES, 來源=_SOURCE_CHOICES)
    async def insert(self, interaction: discord.Interaction,
                      關鍵字: str,
                      搜尋類型: Optional[app_commands.Choice[str]] = None,
                      來源: Optional[app_commands.Choice[str]] = None):
        """
        插播：邏輯與 /音樂 歌姬啦 共用，差別是加進佇列最前面。
        若目前 idle（無播放也無佇列），行為等同 /音樂 歌姬啦。
        """
        await interaction.response.defer()

        if not self.music.ffmpeg_ok:
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...", "FFmpeg 沒裝，本喵的喉嚨壞掉了。", COLOR_ERROR,
            ))
            return

        search_type = (搜尋類型.value if 搜尋類型 else "song")
        source      = (來源.value      if 來源     else "auto")
        kind = self.music.detect_input_type(關鍵字)
        print(f"\n[/音樂 插播] 關鍵字={關鍵字!r} 類型={search_type} 來源={source} kind={kind}")

        # URL 類型可以直接 add_front
        if kind == 'youtube_url':
            info = await self.music.resolve_youtube_url(關鍵字)
            if not info:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", "這個 YouTube 連結本喵搞不定，牢大檢查一下網址。",
                    COLOR_ERROR,
                ))
                return
            await self._add_and_play(
                guild=interaction.guild, channel=interaction.channel,
                song_dict=info, requester=interaction.user,
                via_followup=True, interaction=interaction,
                insert_front=True,
            )
            return

        if kind == 'spotify_url':
            if not self.music.spotify_available:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", "Spotify 目前還沒設定。", COLOR_ERROR,
                ))
                return
            results = await self.music.resolve_spotify(關鍵字)
            if not results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", "Spotify 連結解析失敗。", COLOR_ERROR,
                ))
                return
            # Spotify 多首 playlist 插播：全部加進前面，順序反轉避免逆序
            for item in reversed(results):
                await self._add_and_play(
                    guild=interaction.guild, channel=interaction.channel,
                    song_dict=item, requester=interaction.user,
                    via_followup=True if item is results[-1] else False,
                    interaction=interaction if item is results[-1] else None,
                    insert_front=True,
                )
            return

        # 純文字：用選歌邏輯（共用 SongPickerView）
        if 來源 and 來源.value == "spotify":
            if not self.music.spotify_available:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", "Spotify 目前還沒設定。", COLOR_ERROR,
                ))
                return
            sp_results = await self.music.search_spotify_then_youtube(關鍵字, max_results=15)
            if not sp_results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", f"Spotify 找不到「{關鍵字}」。", COLOR_ERROR,
                ))
                return
            await interaction.followup.send(
                embed=make_embed("⏭️ 插播 — Spotify 搜尋結果",
                    "牢大挑一首喵～選的會插到佇列最前面。", COLOR_MUSIC),
                view=SongPickerView(self, sp_results, interaction.user,
                                      interaction.guild_id, insert_front=True),
            )
            return

        if search_type == "artist":
            results = await self.music.search_top_tracks_for_artist(關鍵字, max_results=15)
            if not results:
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...", f"找不到歌手「{關鍵字}」的可播放結果。",
                    COLOR_ERROR,
                ))
                return
            await interaction.followup.send(
                embed=make_embed(f"⏭️ 插播 — 歌手：{關鍵字}",
                    "選一首喵～會插到佇列最前面。", COLOR_MUSIC),
                view=SongPickerView(self, results, interaction.user,
                                      interaction.guild_id, insert_front=True),
            )
            return

        # 純文字歌曲模式：一律顯示 Top 5（規格 v2.1.3 VI）
        results = await self.music.search_youtube(關鍵字, max_results=15, scored=True)
        if not results:
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                "本小姐找不到能插播的歌曲，牢大換個寫法或直接貼 YouTube 網址。",
                COLOR_ERROR,
            ))
            return
        await interaction.followup.send(
            embed=make_embed("⏭️ 插播 — 搜尋結果",
                "牢大挑一首喵～選的會插到佇列最前面。", COLOR_MUSIC),
            view=SongPickerView(self, results, interaction.user,
                                  interaction.guild_id, insert_front=True),
        )

    # ════════════════════════════════════════════════════════
    #  /天氣（v2.1.6：中英日地名 + 多候選 select menu）
    # ════════════════════════════════════════════════════════
    @query_g.command(
        name="天氣",
        description="🔎 查詢天氣（台灣 CWA / 國外 Open-Meteo / 支援中英日地名）",
    )
    @app_commands.describe(
        地點="例如：台北、高雄、東京、Tokyo、とうきょう、雷克雅維克、Reykjavik",
    )
    async def weather_cmd(self, interaction: discord.Interaction, 地點: str):
        await interaction.response.defer()
        try:
            res = await self.weather.resolve_location(地點, ai=self.ai)
        except Exception as e:
            print(f"[/天氣] resolve_location 失敗：{e}")
            traceback.print_exc()
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                f"查地名時出錯了，牢大稍後再試。\n錯誤：{str(e)[:80]}",
                COLOR_ERROR,
            ))
            return

        print(f"[/天氣] '{地點}' resolved via: {res.resolved_via}, "
              f"is_taiwan={res.is_taiwan}, candidates={len(res.candidates)}")

        # ── 台灣縣市 → 直接 CWA ──
        if res.is_taiwan and res.cwa_city:
            try:
                text = await self.weather.fetch_taiwan_weather(
                    res.forecast_location or res.cwa_city,
                    display_name=res.display_name,
                    district=res.district,
                )
            except Exception as e:
                print(f"[/天氣] CWA 查詢失敗：{e}")
                traceback.print_exc()
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    f"中央氣象署查詢失敗。\n錯誤：{str(e)[:80]}",
                    COLOR_ERROR,
                ))
                return
            await interaction.followup.send(embed=make_embed(
                "☀️ 天氣查詢", text, COLOR_WEATHER,
            ))
            return

        # ── 國外 ──
        if not res.candidates:
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                f"本喵找不到「{地點}」這個地點。\n"
                f"牢大可以試試：\n"
                f"・換個寫法（中/英/日皆可）\n"
                f"・加上國家名稱（例：雷克雅維克 冰島）\n"
                f"・直接用英文（例：Reykjavik）",
                COLOR_ERROR,
            ))
            return

        # 只有 1 個候選 → 直接查
        if len(res.candidates) == 1:
            cand = res.candidates[0]
            try:
                text = await self.weather.fetch_overseas_weather(cand)
            except Exception as e:
                print(f"[/天氣] 國外查詢失敗：{e}")
                traceback.print_exc()
                await interaction.followup.send(embed=make_embed(
                    "出錯了喵...",
                    f"查天氣 API 失敗。\n錯誤：{str(e)[:80]}",
                    COLOR_ERROR,
                ))
                return
            await interaction.followup.send(embed=make_embed(
                "☀️ 天氣查詢", text, COLOR_WEATHER,
            ))
            return

        # ⭐ >= 2 候選 → 顯示 select menu（規格 三 + 十.3）
        await interaction.followup.send(
            embed=make_embed(
                f"🔎 找到 {len(res.candidates)} 個可能地點",
                "本小姐找到幾個可能地點，牢大選一下：",
                COLOR_WEATHER,
            ),
            view=LocationPickerView(self, res.candidates, interaction.user),
        )

    # ════════════════════════════════════════════════════════
    #  /吃飯飯（v2.1.5：500 道菜版 + 找附近按鈕）
    # ════════════════════════════════════════════════════════
    @eat_g.command(name="吃什麼", description="🍱 不知道吃什麼？讓本喵幫你抽一道菜")
    async def random_food(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from core import flavor_text
        food = food_picker.pick_random_food()

        # AI 小語（須通過驗證）→ 不合格 fallback 本地模板
        reason = await self.ai.generate_food_reason(food, interaction.user.id)
        if not reason or not flavor_text.is_valid_flavor_text(reason.strip(), food):
            reason = flavor_text.pick_food_text(food)
        else:
            reason = reason.strip()

        await interaction.followup.send(
            embed=make_embed(
                "🍽️ 來個大吉吧！",
                f"🎯 **{food}**\n\n{reason}\n\n需要本喵幫你找哪裡有賣嗎？",
                COLOR_FOOD,
            ),
            view=FindNearbyView(
                self, display_name=food, maps_query=food,
            ),
        )

    # ════════════════════════════════════════════════════════
    #  /喝什麼（v2.1.5：JSON 驅動 + 同名合併）
    # ════════════════════════════════════════════════════════
    @eat_g.command(name="喝什麼", description="🍱 不知道喝什麼？隨機抽一杯飲料")
    async def random_drink(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from core import flavor_text

        drink = drink_picker.pick_random_drink()
        drink_display = drink_picker.format_drink(drink)

        # AI 小語（須通過驗證）→ 不合格 fallback 本地模板
        reason = await self.ai.generate_drink_reason(drink_display, interaction.user.id)
        if not reason or not flavor_text.is_valid_flavor_text(reason.strip(), drink_display):
            text = flavor_text.pick_drink_text(drink_display)
        else:
            text = reason.strip()

        # maps_query 用 drink_picker 既有的擴展（通用→加「手搖飲」等）
        _places_unused, maps_q = drink_picker.primary_search_keyword(drink)
        await interaction.followup.send(
            embed=make_embed(
                "🥤 今天就喝這杯！",
                f"🎯 **{drink_display}**\n\n{text}\n\n需要本喵幫你找哪裡有賣嗎？",
                COLOR_DRINK,
            ),
            view=FindNearbyView(
                self, display_name=drink_display, maps_query=maps_q,
            ),
        )

    # ════════════════════════════════════════════════════════
    #  /巴豆妖 吃飯飯（v2.3.0：直接產生 Google Maps 連結，不打任何 API）
    # ════════════════════════════════════════════════════════
    @eat_g.command(
        name="吃飯飯",
        description="🍱 幫你把關鍵字丟進 Google Maps 找餐廳",
    )
    @app_commands.describe(
        食物="想吃什麼？例：牛肉湯、拉麵、火鍋（必填）",
        地點="可選：地標 / 地址 / 捷運站。例：捷運大安站、台北車站、高雄巨蛋",
    )
    async def find_food(self, interaction: discord.Interaction,
                        食物: str,
                        地點: Optional[str] = None):
        食物 = (食物 or "").strip()
        地點 = (地點 or "").strip()

        if 地點:
            query = f"{食物} {地點}"
            maps_url = build_google_maps_search_url(query)
            desc = (
                f"牢大，本喵幫你把關鍵字填好了。\n"
                f"直接用 Google Maps 找「{query}」比較快。"
            )
        else:
            query = 食物
            maps_url = build_google_maps_search_url(query)
            desc = (
                f"牢大，本喵幫你把「{食物}」丟進 Google Maps 了。\n"
                f"它會用你裝置上的地圖定位去找，比本喵在 Discord 裡瞎猜準。"
            )

        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(
            label="🗺️ 打開 Google Maps",
            style=discord.ButtonStyle.link,
            url=maps_url,
        ))
        await interaction.response.send_message(
            embed=make_embed("🍴 找餐廳", desc, COLOR_FOOD),
            view=view,
        )


    # ════════════════════════════════════════════════════════
    @tool_g.command(name="算算數", description="🧰 工程計算機（sqrt/sin/log/sum，空白看說明）")
    @app_commands.describe(表達式="例：sqrt(2)、sin(pi/2)、sum(i^2,i=1..n)。留空看符號速查")
    async def calc(self, interaction: discord.Interaction,
                   表達式: Optional[str] = None):
        from core import calc_engine

        # 空白 → 顯示符號速查（規格三.3）
        if not 表達式 or not 表達式.strip():
            await interaction.response.send_message(embed=make_embed(
                "🔢 算算數使用說明", calc_engine.help_text(), COLOR_TOOL,
            ), ephemeral=True)
            return

        try:
            result = calc_engine.evaluate(表達式)
            await interaction.response.send_message(embed=make_embed(
                "🔢 算好了喵！",
                f"`{表達式}`\n= **{result}** 喵♡",
                COLOR_TOOL,
            ))
        except calc_engine.CalcError as e:
            await interaction.response.send_message(embed=make_embed(
                "出錯了喵...",
                f"{str(e)}\n\n💡 用 `/工具 算算數`（留空）看符號速查喵～",
                COLOR_ERROR,
            ))
        except Exception as e:
            print(f"[/算算數] 失敗：{e}")
            await interaction.response.send_message(embed=make_embed(
                "出錯了喵...",
                "這題本喵算不出來，先給本喵明確算式喵～\n"
                "用 `/工具 算算數`（留空）看符號速查。",
                COLOR_ERROR,
            ))

    # ════════════════════════════════════════════════════════
    #  /翻譯姬
    # ════════════════════════════════════════════════════════
    _LANG_CHOICES = [
        app_commands.Choice(name=n, value=n)
        for n in TranslateEngine.SUPPORTED_LANGS
    ]
    _MODE_CHOICES = [
        app_commands.Choice(name="直譯",   value="direct"),
        app_commands.Choice(name="自然語氣", value="natural"),
    ]

    @query_g.command(name="翻譯姬",
                          description="🔎 翻譯文字（可選直譯或自然語氣）")
    @app_commands.describe(
        內容="要翻譯的文字",
        來源語言="原文是哪一種語言？（不確定就選『自動偵測』）",
        目標語言="要翻譯成哪一種語言？",
        模式="直譯（預設、快速）/ 自然語氣（更通順）",
    )
    @app_commands.choices(
        來源語言=_LANG_CHOICES, 目標語言=_LANG_CHOICES, 模式=_MODE_CHOICES,
    )
    async def translate_cmd(
        self, interaction: discord.Interaction, 內容: str,
        來源語言: app_commands.Choice[str],
        目標語言: app_commands.Choice[str],
        模式: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer()
        mode_value = 模式.value if 模式 else "direct"

        try:
            base = await self.translate.translate(
                text=內容, source_lang=來源語言.value, target_lang=目標語言.value,
            )
        except Exception as e:
            print(f"[/翻譯姬] 直譯失敗：{e}")
            traceback.print_exc()
            await interaction.followup.send(embed=make_embed(
                "出錯了喵...",
                f"Google 翻譯壞掉了，牢大稍後再試。\n錯誤：{str(e)[:80]}",
                COLOR_ERROR,
            ))
            return

        final = base
        if mode_value == "natural":
            polished = await self.ai.polish_translation(
                original=內容, base_translation=base,
                target_lang=目標語言.value, user_id=interaction.user.id,
            )
            if polished:
                final = polished

        await interaction.followup.send(embed=make_embed(
            f"🔠 {來源語言.name} ➡️ {目標語言.name}",
            f"**原文：**\n{內容}\n\n**翻譯：**\n{final}",
            COLOR_TOOL,
        ))

    # ════════════════════════════════════════════════════════
    #  /洗芭樂
    # ════════════════════════════════════════════════════════
    @tool_g.command(name="洗芭樂", description="🧰 隨機產生器（文字或數字範圍）")
    @app_commands.describe(
        類型="文字（多選一）或範圍（產生數字）",
        內容="文字類型：空格分隔；範圍類型：格式 1-100",
        數量="抽幾個？預設 1",
    )
    @app_commands.choices(類型=[
        app_commands.Choice(name="文字（空格分隔選項）", value="text"),
        app_commands.Choice(name="範圍（最小-最大）",    value="range"),
    ])
    async def random_tool(
        self, interaction: discord.Interaction,
        類型: app_commands.Choice[str], 內容: str, 數量: int = 1,
    ):
        try:
            if 類型.value == "range":
                start, end = map(lambda x: int(x.strip()), 內容.split('-'))
                if start >= end:
                    raise ValueError("起點要小於終點")
                results = [
                    str(random.randint(start, end))
                    for _ in range(max(1, min(數量, 50)))
                ]
            else:
                opts = 內容.split()
                if not opts:
                    raise ValueError("沒有選項")
                results = random.sample(opts, min(max(1, 數量), len(opts)))
            await interaction.response.send_message(embed=make_embed(
                "🎲 洗到了！",
                f"✨ **{'、'.join(results)}** ✨",
                COLOR_TOOL,
            ))
        except Exception as e:
            await interaction.response.send_message(embed=make_embed(
                "出錯了喵...",
                f"格式不對（{str(e)[:50]}）\n\n"
                f"範例：\n"
                f"・類型=文字, 內容=`雞排 滷肉飯 牛肉麵`\n"
                f"・類型=範圍, 內容=`1-100`",
                COLOR_ERROR,
            ), ephemeral=True)

    # ════════════════════════════════════════════════════════
    #  /提醒醒
    # ════════════════════════════════════════════════════════
    @tool_g.command(name="提醒醒",
                          description="🧰 設定提醒（用選單選時間）")
    @app_commands.describe(事項="要提醒什麼？例：去倒垃圾、開會")
    async def reminder(self, interaction: discord.Interaction, 事項: str):
        view = ReminderView(
            repo=self.repo,
            user_id=interaction.user.id,
            channel_id=interaction.channel_id,
            task=事項,
        )
        await interaction.response.send_message(
            embed=make_embed(
                f"📝 設定提醒：{事項}",
                "請依序選好下方三個選單，然後按「✅ 確認設定」喵♡\n"
                "💡 如果今天的時間已經過了，會自動設成明天～",
                COLOR_REMINDER,
            ),
            view=view, ephemeral=True,
        )

    # ════════════════════════════════════════════════════════
    #  /黑優浦蜜 幫助
    # ════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════
    #  /黑優浦蜜 狀態（v2.1.8 新增）
    # ════════════════════════════════════════════════════════
    @system_g.command(name="狀態", description="🐾 顯示 Bot 環境與功能狀態（不呼叫 AI）")
    async def status_cmd(self, interaction: discord.Interaction):
        try:
            from config import Config

            def check(b): return "✅ 可用" if b else "⚠️ 未設定"

            ffmpeg_ok    = self.music.ffmpeg_ok
            spotify_ok   = self.music.spotify_available
            cwa_ok       = bool(Config.CWA_API_KEY)
            places_ok    = bool(Config.GOOGLE_PLACES_API_KEY)
            ai_enabled   = self.ai.is_available()
            ai_backend   = (" → ".join(self.ai._provider_order()) if ai_enabled else "—")
            redis_status = "✅ 已連線" if (self.cache and getattr(self.cache, 'enabled', False)) else "⚠️ 未設定（記憶體模式）"
            db_backend = type(self.repo).__name__

            q = self.qm.get_or_create(interaction.guild_id)
            voice_text = "未連線"
            if q.vc and q.vc.is_connected():
                voice_text = f"在「{q.vc.channel.name}」"
            queue_len = q.total

            lines = [
                "**🐾 黑優浦蜜狀態**",
                "",
                "**版本：** v2.3.0",
                "",
                "**外部服務**",
                f"・FFmpeg：{check(ffmpeg_ok)}",
                f"・Spotify：{check(spotify_ok)}",
                f"・CWA 氣象署：{check(cwa_ok)}",
                f"・Google Places：{check(places_ok)}",
                f"・Utility AI：{check(ai_enabled)}" + (f"（{ai_backend}）" if ai_enabled else ""),
                "",
                "**儲存**",
                f"・Redis：{redis_status}",
                f"・Database：✅ {db_backend}",
                "",
                "**目前音樂狀態**",
                f"・語音頻道：{voice_text}",
                f"・佇列：{queue_len} 首",
            ]

            await interaction.response.send_message(embed=make_embed(
                "🐾 系統狀態", "\n".join(lines), COLOR_INFO,
            ), ephemeral=True)
        except Exception as e:
            print(f"[/狀態] 失敗：{e}")
            traceback.print_exc()
            await interaction.response.send_message(embed=make_embed(
                "出錯了喵...",
                f"查狀態時出錯了。\n錯誤：{str(e)[:80]}",
                COLOR_ERROR,
            ), ephemeral=True)

    # ════════════════════════════════════════════════════════
    #  /黑優浦蜜 幫助
    # ════════════════════════════════════════════════════════
    @system_g.command(name="幫助", description="🐾 顯示所有指令說明")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = make_embed(
            "🐾 黑優浦蜜指令表（v2.3.0）",
            "v2.1.9 整理：/吃喝 → /巴豆妖、搜尋歌曲合進歌姬啦、找附近改成「先問位置」流程",
            COLOR_INFO,
        )
        embed.add_field(
            name="🎵 /音樂",
            value=(
                "🎵 `/音樂 歌姬啦` — 播放或加入佇列（純文字 → 15 個分 3 頁手動選歌）\n"
                "　・搜尋類型：歌曲（預設）/ 歌手\n"
                "　・來源：自動（預設）/ YouTube / Spotify\n"
                "🎵 `/音樂 插播` — 把歌插到佇列最前面（不打斷正在播的）\n"
                "📜 `/音樂 佇列` — 查看目前音樂佇列\n"
                "🎶 `/音樂 現正播放` — 查看目前播放中的歌曲\n"
                "⏭️ `/音樂 跳過` — 跳過目前歌曲（管理員或點歌者）\n"
                "🔁 `/音樂 循環` — 設定循環模式（關閉/單曲/歌單）\n"
                "⏹️ `/音樂 停止` — 停止音樂並離開語音\n"
                "🗑️ `/音樂 清空佇列` — 清空整個音樂佇列（管理員限定）"
            ),
            inline=False,
        )
        embed.add_field(
            name="🍱 /巴豆妖",
            value=(
                f"`/巴豆妖 吃什麼` — 隨機抽一道菜（{food_picker.total_food_count()} 道）\n"
                f"`/巴豆妖 吃飯飯` — 幫你把關鍵字丟進 Google Maps 找餐廳\n"
                f"`/巴豆妖 喝什麼` — 隨機抽一杯飲料（{drink_picker.total_drink_count()} 杯 / "
                f"{drink_picker.total_store_count()} 家店）"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔎 /查詢",
            value=(
                "`/查詢 翻譯姬` — 翻譯文字（可選直譯或自然語氣）\n"
                "`/查詢 天氣` — 中英日地名 / 台灣 CWA / 國外 Open-Meteo"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧰 /工具",
            value=(
                "`/工具 算算數` — 安全計算數學算式\n"
                "`/工具 提醒醒` — 設定提醒\n"
                "`/工具 洗芭樂` — 隨機產生器"
            ),
            inline=False,
        )
        embed.add_field(
            name="🐾 /黑優浦蜜",
            value=(
                "`/黑優浦蜜 狀態` — 顯示 Bot 環境與功能狀態（debug 用）\n"
                "`/黑優浦蜜 幫助` — 顯示所有指令說明"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ════════════════════════════════════════════════════════
    #  提醒排程器（每分鐘檢查一次）
    # ════════════════════════════════════════════════════════
    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        now = datetime.now()
        try:
            due = await self.repo.get_due_reminders(now)
        except Exception as e:
            print(f"[Reminder] 查詢失敗：{e}")
            return

        for r in due:
            channel = self.bot.get_channel(r.channel_id)
            if channel:
                try:
                    await channel.send(
                        f"🔔 **【時間到啦喵！】**\n"
                        f"> 📌 <@{r.user_id}> 該去「{r.task}」了喵♡"
                    )
                except Exception:
                    pass
            try:
                await self.repo.delete_reminder(r.id)
            except Exception:
                pass

    @reminder_loop.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()



#  Bot 主體 + 全域錯誤處理（詳細 log）
# ════════════════════════════════════════════════════════════════
class HelpmeBot(commands.Bot):

    def __init__(self, music, weather, food, translate, utility_ai, repo, cache):
        super().__init__(
            command_prefix='!',
            intents=discord.Intents.all(),
            help_command=None,
        )
        self._music     = music
        self._weather   = weather
        self._food      = food
        self._translate = translate
        self._ai        = utility_ai
        self._repo      = repo
        self._cache     = cache

    async def setup_hook(self):
        cog = MainCog(
            self, music=self._music, weather=self._weather, food=self._food,
            translate=self._translate, utility_ai=self._ai,
            repo=self._repo, cache=self._cache,
        )
        await self.add_cog(cog)

        # ⭐ v2.2.0 規格八：DEBUG_COMMANDS_ENABLED=false 時不註冊 /黑優浦蜜 狀態
        from config import Config
        if not Config.DEBUG_COMMANDS_ENABLED:
            try:
                status_cmd = cog.system_g.get_command("狀態")
                if status_cmd:
                    cog.system_g.remove_command("狀態")
                    print("ℹ️  DEBUG_COMMANDS_ENABLED=false → 不註冊 /黑優浦蜜 狀態")
            except Exception as e:
                print(f"[setup] 移除 /狀態 失敗（不影響啟動）：{e}")
        else:
            print("🔧 DEBUG_COMMANDS_ENABLED=true → 已註冊 /黑優浦蜜 狀態")

        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ):
            # 開發者：詳細 traceback + 上下文
            cmd_name = interaction.command.name if interaction.command else '?'
            user_id  = interaction.user.id if interaction.user else '?'
            guild_id = interaction.guild_id or '?'

            # 嘗試提取使用者輸入的參數
            try:
                ns = interaction.namespace
                params = {k: v for k, v in ns.__dict__.items()}
            except Exception:
                params = {}

            print("\n" + "═" * 60)
            print(f"[Command Error] /{cmd_name}")
            print(f"  guild_id: {guild_id}")
            print(f"  user_id : {user_id}")
            print(f"  params  : {params}")
            print(f"  error   : {type(error).__name__}: {error}")
            print("─" * 60)
            traceback.print_exception(type(error), error, error.__traceback__)
            print("═" * 60 + "\n")

            # 使用者：友善訊息
            msg = (
                "本喵已經把錯誤紀錄起來，請等等再試喵♡\n"
                "如果一直發生，請告訴牢大檢查 console log。"
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        embed=make_embed("出錯了喵...", msg, COLOR_ERROR),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        embed=make_embed("出錯了喵...", msg, COLOR_ERROR),
                        ephemeral=True,
                    )
            except Exception:
                pass

        await self.tree.sync()
        print("✅ 黑優浦蜜：所有指令已註冊！")

    async def on_ready(self):
        print(f"🐾 已登入：{self.user}（ID: {self.user.id}）")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="🎻 牢大的指令 | /幫助",
        ))


def create_discord_bot(music, weather, food, translate, utility_ai, repo, cache) -> HelpmeBot:
    return HelpmeBot(music, weather, food, translate, utility_ai, repo, cache)
