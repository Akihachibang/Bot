"""
Discord Key Bot (v2 - ตรวจชื่อคอลัมน์อัตโนมัติ)
แจ้งเตือน: สร้างคีย์ / รีเซ็ต HWID / ผูก HWID / ยกเลิกคีย์ / ลบคีย์ / แก้ไขคีย์
คำสั่ง: /chekstock , /downdownload

ติดตั้ง:  pip install discord.py aiohttp python-dotenv
.env:
    DISCORD_TOKEN=...
    SUPABASE_URL=https://cgsuxxyezqhxayllhzfs.supabase.co
    SUPABASE_KEY=...
"""
import os
import re
import json
import asyncio
import datetime as dt

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ================= ตั้งค่า =================
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://cgsuxxyezqhxayllhzfs.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

GUILD_ID = 1572061109714948166
LOG_CHANNEL_ID = 1572143297219137650
DOWNLOAD_URL = "https://drive.google.com/file/d/11POxfP50cmKD1RaXWdeFOOcaDD6csVwx/view"

KEYS_TABLE = "license_keys"
EVENTS_TABLE = "license_events"
WATCH_EVENTS = False   # True = แจ้งเตือนทุกแถวใหม่ในตาราง license_events ด้วย
POLL_SECONDS = 10

# รูป QR ชำระเงิน (แสดงใน ticket สั่งซื้อ)
# ถ้ามีไฟล์ qr.png วางข้าง key_bot.py บอทจะใช้ไฟล์นี้ก่อน (ไม่มีวันหมดอายุ แนะนำวิธีนี้)
# ถ้าไม่มี จะใช้ลิงก์ด้านล่าง (ลิงก์ Discord CDN มีวันหมดอายุ)
QR_LOCAL_FILE = "qr.png"
QR_IMAGE_URL = os.getenv(
    "QR_IMAGE_URL",
    "https://cdn.discordapp.com/attachments/1572106348022337677/1572260772615032913/"
    "Instagram_Post_1.png?ex=6afdb973&is=6afc67f3&"
    "hm=87d582dee30e43dac526fbfbf080cad9d47a7791c25668f65f98319720f7fc56&",
)

# ถ้าตรวจอัตโนมัติผิด ให้ใส่ชื่อคอลัมน์เองตรงนี้ (เว้นว่าง = ตรวจเอง)
FORCE_ID_COL = ""
FORCE_KEY_COL = ""
FORCE_HWID_COL = ""
# ===========================================

HEADERS = {"apikey": SUPABASE_KEY or "", "Authorization": f"Bearer {SUPABASE_KEY}"}

BOOL_REVOKED = {"revoked", "is_revoked", "banned", "is_banned", "disabled",
                "is_disabled", "cancelled", "canceled", "blacklisted", "is_blacklisted"}
BOOL_SOLD = {"is_sold", "sold", "is_used", "used", "redeemed", "is_redeemed",
             "activated", "is_activated", "claimed", "is_claimed"}
OWNER_COLS = {"owner", "buyer", "used_by", "user_id", "discord_id", "customer",
              "claimed_by", "assigned_to", "sold_to"}
STATUS_COLS = {"status", "state"}
REVOKED_VALUES = {"revoked", "banned", "cancelled", "canceled", "disabled",
                  "blacklisted", "suspended"}
SOLD_VALUES = {"sold", "used", "redeemed", "activated", "active", "claimed", "assigned"}
IGNORE_CHANGE = {"updated_at", "ip", "last_ip", "last_seen", "last_used",
                 "last_check", "last_login"}

intents = discord.Intents.default()
# เปิดเป็น 1 เมื่อเปิด "Message Content Intent" ใน Developer Portal แล้ว (ใช้เก็บข้อความใน transcript)
intents.message_content = os.getenv("ENABLE_MESSAGE_CONTENT") == "1"
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

session: aiohttp.ClientSession | None = None
snapshot: dict = {}
first_run = True
events_seen: set | None = None
schema: dict = {}


# ---------- ตัวช่วย ----------
def pick(cols, cands):
    low = {c.lower(): c for c in cols}
    for c in cands:
        if c in low:
            return low[c]
    return None


def detect_schema(rows: list[dict]):
    """ตรวจชื่อคอลัมน์จากแถวตัวอย่าง"""
    global schema
    if not rows:
        return
    cols = list(rows[0].keys())
    key_col = FORCE_KEY_COL or pick(cols, ["license_key", "key", "key_value", "license",
                                            "serial", "code", "key_code"])
    hwid_col = FORCE_HWID_COL or next((c for c in cols if "hwid" in c.lower()), None) \
        or pick(cols, ["machine_id", "device_id", "hardware_id"])
    id_col = FORCE_ID_COL or pick(cols, ["id", "uuid", "license_id", "key_id"]) \
        or key_col or cols[0]
    new = {"id": id_col, "key": key_col, "hwid": hwid_col, "cols": cols}
    if new != schema:
        schema = new
        print(f"[schema] คอลัมน์ทั้งหมด: {cols}")
        print(f"[schema] id={id_col} | key={key_col} | hwid={hwid_col}")


def truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "t")
    return bool(v)


def is_revoked(row: dict) -> bool:
    for c, v in row.items():
        lc = c.lower()
        if lc in BOOL_REVOKED and truthy(v):
            return True
        if lc in STATUS_COLS and str(v or "").lower() in REVOKED_VALUES:
            return True
    return False


def is_sold(row: dict) -> bool:
    found_info = False
    for c, v in row.items():
        lc = c.lower()
        if lc in BOOL_SOLD:
            found_info = True
            if truthy(v):
                return True
        elif lc in STATUS_COLS:
            found_info = True
            if str(v or "").lower() in SOLD_VALUES:
                return True
        elif lc in OWNER_COLS:
            found_info = True
            if v not in (None, "", 0):
                return True
    if not found_info and schema.get("hwid"):      # ไม่มีคอลัมน์บอกสถานะเลย
        return bool(row.get(schema["hwid"]))       # ถือว่าผูก HWID แล้ว = ใช้แล้ว
    return False


def mask(v) -> str:
    s = str(v if v not in (None, "") else "-")
    return s if len(s) <= 8 else f"{s[:4]}…{s[-4:]}"


def key_of(row: dict) -> str:
    c = schema.get("key") or schema.get("id")
    return mask(row.get(c))


async def fetch_all(table: str) -> list[dict]:
    out, start = [], 0
    while True:
        h = {**HEADERS, "Range-Unit": "items", "Range": f"{start}-{start + 999}"}
        async with session.get(f"{SUPABASE_URL}/rest/v1/{table}?select=*", headers=h) as r:
            if r.status not in (200, 206):
                raise RuntimeError(f"Supabase {r.status} ({table}): {await r.text()}")
            page = await r.json()
        out += page
        if len(page) < 1000:
            return out
        start += 1000


def make_embed(title: str, color: int, row: dict, extra: str = "") -> discord.Embed:
    e = discord.Embed(title=title, color=color, timestamp=dt.datetime.now(dt.timezone.utc))
    e.add_field(name="Key", value=f"`{key_of(row)}`", inline=True)
    if schema.get("id") and schema["id"] != schema.get("key"):
        e.add_field(name="ID", value=str(row.get(schema["id"])), inline=True)
    if extra:
        e.add_field(name="รายละเอียด", value=extra[:1000], inline=False)
    return e


def deleted_embed(row: dict) -> discord.Embed:
    """แจ้งลบคีย์ พร้อมข้อมูลครบทุกคอลัมน์ของแถวที่ถูกลบ"""
    if is_revoked(row):
        state = "⛔ ถูกยกเลิกอยู่"
    elif is_sold(row):
        state = "💰 ขายแล้ว/ถูกใช้งานแล้ว"
    else:
        state = "📦 ยังอยู่ใน Stock (ยังไม่ขาย)"

    e = discord.Embed(title="🗑️ ลบคีย์ออกจากระบบ", color=0x95A5A6,
                      timestamp=dt.datetime.now(dt.timezone.utc))
    kc = schema.get("key") or schema.get("id")
    e.add_field(name="Key (เต็ม)", value=f"`{row.get(kc)}`", inline=False)
    e.add_field(name="สถานะก่อนลบ", value=state, inline=True)
    hw = schema.get("hwid")
    if hw:
        e.add_field(name="HWID ที่ผูกอยู่",
                    value=f"`{row.get(hw)}`" if row.get(hw) else "ยังไม่ผูก", inline=True)

    lines = []
    for c, v in row.items():
        if v in (None, ""):
            continue
        lines.append(f"**{c}**: {str(v)[:150]}")
    e.add_field(name="ข้อมูลทั้งหมดของคีย์นี้",
                value=("\n".join(lines) or "-")[:1000], inline=False)
    e.set_footer(text="ระบบตรวจพบว่าแถวนี้หายไปจากตาราง "
                      "(ไม่ทราบผู้ลบ) เวลาตรวจพบตามข้างบน")
    return e


async def notify(e: discord.Embed):
    ch = client.get_channel(LOG_CHANNEL_ID) or await client.fetch_channel(LOG_CHANNEL_ID)
    await ch.send(embed=e)


# ---------- ตัวเฝ้าดูตารางคีย์ ----------
@tasks.loop(seconds=POLL_SECONDS)
async def watcher():
    global snapshot, first_run
    try:
        rows = await fetch_all(KEYS_TABLE)
        detect_schema(rows)
        idc = schema.get("id")
        if not idc:
            return
        current = {r.get(idc): r for r in rows}

        if first_run:
            snapshot, first_run = current, False
            return

        hw = schema.get("hwid")
        for rid, new in current.items():
            old = snapshot.get(rid)
            if old is None:
                await notify(make_embed("🆕 สร้างคีย์ใหม่", 0x2ECC71, new))
                continue
            if old == new:
                continue

            handled = False
            if not is_revoked(old) and is_revoked(new):
                await notify(make_embed("⛔ ยกเลิกคีย์", 0xE74C3C, new)); handled = True
            elif is_revoked(old) and not is_revoked(new):
                await notify(make_embed("♻️ ยกเลิกการแบนคีย์", 0x3498DB, new)); handled = True

            if hw and old.get(hw) != new.get(hw):
                if old.get(hw) and not new.get(hw):
                    await notify(make_embed("🔄 รีเซ็ต HWID", 0xF1C40F, new)); handled = True
                elif new.get(hw):
                    await notify(make_embed("🔗 ผูก HWID ใหม่", 0x9B59B6, new,
                                            f"`{mask(old.get(hw))}` → `{mask(new.get(hw))}`"))
                    handled = True

            if not handled:
                changed = [c for c in new
                           if old.get(c) != new.get(c)
                           and c.lower() not in IGNORE_CHANGE
                           and not c.lower().startswith("last_")]
                if changed:
                    await notify(make_embed("🛠️ จัดการคีย์", 0xE67E22, new,
                                            "แก้ไขคอลัมน์: " + ", ".join(changed)))

        for rid, old in snapshot.items():
            if rid not in current:
                await notify(deleted_embed(old))

        snapshot = current
    except Exception as ex:
        print("watcher error:", repr(ex))


# ---------- ตัวเฝ้าดูตาราง events (เปิดด้วย WATCH_EVENTS) ----------
@tasks.loop(seconds=POLL_SECONDS)
async def events_watcher():
    global events_seen
    try:
        rows = await fetch_all(EVENTS_TABLE)

        def ident(r):
            return r["id"] if "id" in r else json.dumps(r, sort_keys=True, default=str)

        ids = {ident(r) for r in rows}
        if events_seen is None:
            events_seen = ids
            return
        for r in rows:
            if ident(r) in events_seen:
                continue
            lines = []
            for c, v in list(r.items())[:10]:
                if v in (None, ""):
                    continue
                if any(w in c.lower() for w in ("key", "hwid")):
                    v = mask(v)
                lines.append(f"**{c}**: {str(v)[:80]}")
            e = discord.Embed(title="📋 เหตุการณ์ใหม่", description="\n".join(lines)[:3500],
                              color=0x1ABC9C, timestamp=dt.datetime.now(dt.timezone.utc))
            await notify(e)
        events_seen = ids
    except Exception as ex:
        print("events error:", repr(ex))


# ---------- คำสั่ง ----------
@tree.command(name="chekstock", description="ดูจำนวนคีย์ที่ยังไม่ถูกขาย",
              guild=discord.Object(id=GUILD_ID))
async def chekstock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await fetch_all(KEYS_TABLE)
        detect_schema(rows)
        n = sum(1 for r in rows if not is_revoked(r) and not is_sold(r))
        await interaction.followup.send(f"📦 สินค้าใน Stock เหลือ **{n}** คีย์ (จากทั้งหมด {len(rows)})")
    except Exception as ex:
        print("stock error:", repr(ex))
        await interaction.followup.send("❌ ดึงข้อมูลสต็อกไม่สำเร็จ")


@tree.command(name="downdownload", description="รับลิงก์ดาวน์โหลด",
              guild=discord.Object(id=GUILD_ID))
async def downdownload(interaction: discord.Interaction):
    await interaction.response.send_message(f"⬇️ ดาวน์โหลดได้ที่นี่\n{DOWNLOAD_URL}", ephemeral=True)


# =====================================================================
#                            ระบบ TICKET
# =====================================================================
TZ_TH = dt.timezone(dt.timedelta(hours=7))
G = discord.Object(id=GUILD_ID)

KINDS = {
    "order": {
        "label": "สั่งซื้อสินค้า", "emoji": "🛒", "style": discord.ButtonStyle.success,
        "modal": "แบบฟอร์มสั่งซื้อสินค้า", "color": 0x2ECC71, "title": "🛒 Ticket สั่งซื้อสินค้า",
        "fields": [
            ("สินค้า/แพ็กเกจที่ต้องการ", "เช่น คีย์ 30 วัน", True, False, None),
            ("จำนวน", "1", True, False, "1"),
            ("วิธีชำระเงิน", "โอนธนาคาร / พร้อมเพย์ / TrueMoney", True, False, None),
            ("หมายเหตุ (ถ้ามี)", "", False, True, None),
        ],
    },
    "help": {
        "label": "สอบถาม / แจ้งปัญหา", "emoji": "❓", "style": discord.ButtonStyle.primary,
        "modal": "แบบฟอร์มสอบถาม/แจ้งปัญหา", "color": 0x3498DB, "title": "❓ Ticket สอบถาม/แจ้งปัญหา",
        "fields": [
            ("หัวข้อ", "เช่น รีเซ็ต HWID / โหลดไม่ได้", True, False, None),
            ("รายละเอียด", "อธิบายปัญหาหรือคำถามของคุณ", True, True, None),
        ],
    },
}


def parse_topic(ch) -> dict | None:
    """ข้อมูล ticket เก็บไว้ที่ topic ของห้อง (ไม่ต้องใช้ฐานข้อมูล บอทรีสตาร์ทก็ไม่หาย)"""
    t = getattr(ch, "topic", None) or ""
    if not t.startswith("tk|"):
        return None
    try:
        p = t.split("|")
        return {"opener": int(p[1]), "kind": p[2], "claimed": int(p[3]),
                "log": int(p[4]), "role": int(p[5])}
    except (IndexError, ValueError):
        return None


def build_topic(d: dict) -> str:
    return f"tk|{d['opener']}|{d['kind']}|{d['claimed']}|{d['log']}|{d['role']}"


def is_staff(member: discord.Member, role_id: int) -> bool:
    return member.guild_permissions.administrator or any(r.id == role_id for r in member.roles)


async def stock_count() -> int:
    rows = await fetch_all(KEYS_TABLE)
    detect_schema(rows)
    return sum(1 for r in rows if not is_revoked(r) and not is_sold(r))


async def build_transcript(channel: discord.TextChannel) -> str:
    lines = [f"Transcript ของห้อง #{channel.name}",
             f"สร้างเมื่อ: {dt.datetime.now(TZ_TH):%Y-%m-%d %H:%M:%S} (GMT+7)", "=" * 50]
    if not intents.message_content:
        lines.append("(หมายเหตุ: ยังไม่เปิด Message Content Intent จึงไม่มีเนื้อหาข้อความของสมาชิก)")
    async for m in channel.history(limit=None, oldest_first=True):
        t = m.created_at.astimezone(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
        text = m.content or ""
        if m.attachments:
            text += " [ไฟล์: " + ", ".join(a.url for a in m.attachments) + "]"
        if not text and m.embeds:
            text = "[embed] " + (m.embeds[0].title or "")
        lines.append(f"[{t}] {m.author} ({m.author.id}): {text}")
    return "\n".join(lines)


async def create_ticket(interaction: discord.Interaction, kind: str, cat_id: int,
                        role_id: int, log_id: int, answers: list[tuple[str, str]]):
    await interaction.response.defer(ephemeral=True)
    guild, user = interaction.guild, interaction.user
    category = guild.get_channel(cat_id)
    role = guild.get_role(role_id)
    if not isinstance(category, discord.CategoryChannel) or role is None:
        return await interaction.followup.send(
            "❌ หมวดหมู่หรือยศทีมงานไม่ถูกต้อง แจ้งแอดมินให้สร้างปุ่มใหม่ด้วย /ticket_panel", ephemeral=True)

    for ch in guild.text_channels:                      # 1 คน เปิดได้ 1 ticket
        info = parse_topic(ch)
        if info and info["opener"] == user.id:
            return await interaction.followup.send(
                f"⚠️ คุณมี ticket ที่เปิดอยู่แล้ว: {ch.mention}", ephemeral=True)

    P = discord.PermissionOverwrite
    overwrites = {
        guild.default_role: P(view_channel=False),
        user: P(view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True, embed_links=True),
        role: P(view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True, manage_messages=True),
        guild.me: P(view_channel=True, send_messages=True, manage_channels=True,
                    manage_messages=True, embed_links=True, attach_files=True,
                    read_message_history=True),
    }
    slug = re.sub(r"[^a-z0-9_-]", "", user.name.lower()) or str(user.id)[-4:]
    info = {"opener": user.id, "kind": kind, "claimed": 0, "log": log_id, "role": role_id}
    try:
        ch = await guild.create_text_channel(
            name=f"{kind}-{slug}", category=category, overwrites=overwrites,
            topic=build_topic(info), reason=f"Ticket โดย {user}")
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ บอทไม่มีสิทธิ์สร้างห้องในหมวดหมู่นี้ (ต้องมี Manage Channels)", ephemeral=True)
    except discord.HTTPException as ex:
        return await interaction.followup.send(f"❌ สร้างห้องไม่สำเร็จ: {ex}", ephemeral=True)

    k = KINDS[kind]
    e = discord.Embed(title=k["title"], color=k["color"],
                      description=f"สวัสดี {user.mention} ทีมงานจะเข้ามาตอบโดยเร็วที่สุด กรุณารอสักครู่",
                      timestamp=dt.datetime.now(dt.timezone.utc))
    for label, val in answers:
        e.add_field(name=label, value=(val or "-")[:1000], inline=False)
    if kind == "order":
        try:
            e.add_field(name="📦 สินค้าใน Stock ตอนนี้", value=f"{await stock_count()} คีย์", inline=True)
        except Exception as ex:
            print("stock in ticket error:", repr(ex))
    send_kwargs = {}
    if kind == "order":
        if os.path.isfile(QR_LOCAL_FILE):
            send_kwargs["file"] = discord.File(QR_LOCAL_FILE, filename="qr.png")
            e.set_image(url="attachment://qr.png")
        elif QR_IMAGE_URL:
            e.set_image(url=QR_IMAGE_URL)
        e.add_field(name="💳 ชำระเงิน",
                    value="สแกน QR ด้านล่างเพื่อโอน แล้วส่งสลิปในห้องนี้", inline=False)
    e.set_footer(text="ปุ่มด้านล่างใช้รับเรื่อง/ปิดตั๋ว")
    await ch.send(content=f"{user.mention} {role.mention}", embed=e,
                  view=TicketControls(), **send_kwargs)

    log_ch = guild.get_channel(log_id)
    if log_ch:
        le = discord.Embed(title="🎫 มี Ticket ใหม่", color=k["color"],
                           timestamp=dt.datetime.now(dt.timezone.utc))
        le.add_field(name="ประเภท", value=f"{k['emoji']} {k['label']}", inline=True)
        le.add_field(name="ลูกค้า", value=f"{user.mention} (`{user.id}`)", inline=True)
        le.add_field(name="ห้อง", value=ch.mention, inline=True)
        for label, val in answers[:3]:
            le.add_field(name=label, value=(val or "-")[:200], inline=False)
        v = discord.ui.View()
        v.add_item(discord.ui.Button(label="เปิดดู Ticket", url=ch.jump_url))
        try:
            await log_ch.send(content=role.mention, embed=le, view=v)
        except discord.HTTPException as ex:
            print("ticket log error:", repr(ex))

    await interaction.followup.send(f"✅ เปิด ticket แล้ว: {ch.mention}", ephemeral=True)


async def close_ticket(channel: discord.TextChannel, closer: discord.Member):
    info = parse_topic(channel) or {}
    guild = channel.guild
    text = await build_transcript(channel)
    data = text.encode("utf-8")

    def make_file():
        import io
        return discord.File(io.BytesIO(data), filename=f"transcript-{channel.name}.txt")

    opener = guild.get_member(info.get("opener", 0))
    log_ch = guild.get_channel(info.get("log", 0))
    if log_ch:
        le = discord.Embed(title="🔒 ปิด Ticket", color=0xE74C3C,
                           timestamp=dt.datetime.now(dt.timezone.utc))
        le.add_field(name="ห้อง", value=f"#{channel.name}", inline=True)
        le.add_field(name="ลูกค้า", value=f"<@{info.get('opener', 0)}>", inline=True)
        le.add_field(name="ปิดโดย", value=closer.mention, inline=True)
        if info.get("claimed"):
            le.add_field(name="ผู้รับเรื่อง", value=f"<@{info['claimed']}>", inline=True)
        try:
            await log_ch.send(embed=le, file=make_file())
        except discord.HTTPException as ex:
            print("close log error:", repr(ex))
    if opener:
        try:
            await opener.send(f"🔒 Ticket **#{channel.name}** ของคุณถูกปิดแล้ว "
                              f"แนบบันทึกการสนทนาไว้ด้านล่าง", file=make_file())
        except discord.HTTPException:
            pass                                         # ลูกค้าปิดรับ DM
    await channel.send("🔒 กำลังปิดห้องนี้ใน 5 วินาที...")
    await asyncio.sleep(5)
    await channel.delete(reason=f"ปิด ticket โดย {closer}")


class ConfirmClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="ยืนยันปิดตั๋ว", style=discord.ButtonStyle.danger, emoji="🔒")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="⏳ กำลังปิดตั๋ว...", view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="ยกเลิกการปิดตั๋วแล้ว", view=None)


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="รับเรื่อง", style=discord.ButtonStyle.success, emoji="🙋", custom_id="tk:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        info = parse_topic(ch)
        if not info:
            return await interaction.response.send_message("❌ ไม่ใช่ห้อง ticket", ephemeral=True)
        if not is_staff(interaction.user, info["role"]):
            return await interaction.response.send_message("❌ เฉพาะทีมงานเท่านั้น", ephemeral=True)
        if info["claimed"]:
            return await interaction.response.send_message(
                f"⚠️ <@{info['claimed']}> รับเรื่องนี้ไปแล้ว", ephemeral=True)
        info["claimed"] = interaction.user.id
        button.disabled = True
        button.label = "รับเรื่องแล้ว"
        await interaction.response.edit_message(view=self)
        await ch.send(f"🙋 {interaction.user.mention} รับเรื่องนี้แล้ว")
        try:
            await ch.edit(topic=build_topic(info))
        except discord.HTTPException as ex:
            print("claim topic error:", repr(ex))
        log_ch = interaction.guild.get_channel(info["log"])
        if log_ch:
            await log_ch.send(embed=discord.Embed(
                title="🙋 รับเรื่อง Ticket", color=0xF1C40F,
                description=f"{interaction.user.mention} รับเรื่อง {ch.mention}"))

    @discord.ui.button(label="ปิดตั๋ว", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="tk:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = parse_topic(interaction.channel)
        if not info:
            return await interaction.response.send_message("❌ ไม่ใช่ห้อง ticket", ephemeral=True)
        if interaction.user.id != info["opener"] and not is_staff(interaction.user, info["role"]):
            return await interaction.response.send_message("❌ ไม่มีสิทธิ์ปิดตั๋วนี้", ephemeral=True)
        await interaction.response.send_message("ต้องการปิดตั๋วนี้จริงหรือไม่?", view=ConfirmClose(), ephemeral=True)


class TicketModal(discord.ui.Modal):
    def __init__(self, kind: str, cat: int, role: int, log: int):
        super().__init__(title=KINDS[kind]["modal"])
        self.kind, self.cat, self.role, self.log = kind, cat, role, log
        self.inputs: list[discord.ui.TextInput] = []
        for label, ph, req, long, default in KINDS[kind]["fields"]:
            ti = discord.ui.TextInput(
                label=label, placeholder=ph or None, required=req, default=default,
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                max_length=500 if long else 100)
            self.add_item(ti)
            self.inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.kind, self.cat, self.role, self.log,
                            [(i.label, i.value) for i in self.inputs])


class OpenTicketButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"tk:open:(?P<kind>[a-z]+):(?P<cat>\d+):(?P<role>\d+):(?P<log>\d+)"):
    """ปุ่มเปิด ticket: เก็บหมวดหมู่/ยศ/ห้อง log ไว้ใน custom_id จึงไม่ต้องเก็บค่าตั้งไว้ที่ไหน"""
    def __init__(self, kind: str, cat: int, role: int, log: int):
        k = KINDS[kind]
        super().__init__(discord.ui.Button(
            label=k["label"], emoji=k["emoji"], style=k["style"],
            custom_id=f"tk:open:{kind}:{cat}:{role}:{log}"))
        self.kind, self.cat, self.role, self.log = kind, cat, role, log

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], int(match["cat"]), int(match["role"]), int(match["log"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketModal(self.kind, self.cat, self.role, self.log))


@tree.command(name="ticket_panel", description="สร้างแผงปุ่มเปิด Ticket ในห้องที่เลือก", guild=G)
@app_commands.describe(
    channel="ห้องที่จะวางปุ่มเปิด ticket",
    category="หมวดหมู่ที่จะสร้างห้อง ticket",
    staff_role="ยศทีมงานที่เห็นและตอบ ticket",
    log_channel="ห้องแจ้งเตือนเมื่อมี ticket ใหม่ (ไม่เลือก = ห้องแจ้งเตือนหลักของบอท)",
    title="หัวข้อบนแผง (ไม่ใส่ = ใช้ค่าเริ่มต้น)",
    description="ข้อความอธิบายบนแผง (ไม่ใส่ = ใช้ค่าเริ่มต้น)",
    include_help="แสดงปุ่ม สอบถาม/แจ้งปัญหา ด้วย",
    image="ลิงก์รูปหรือ GIF ที่จะโชว์บนแผง (ขึ้นต้นด้วย https://)")
@app_commands.default_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction,
                       channel: discord.TextChannel,
                       category: discord.CategoryChannel,
                       staff_role: discord.Role,
                       log_channel: discord.TextChannel | None = None,
                       title: str | None = None,
                       description: str | None = None,
                       include_help: bool = True,
                       image: str | None = None):
    if image and not re.match(r"^https?://\S+$", image.strip()):
        return await interaction.response.send_message(
            "❌ ลิงก์รูปต้องขึ้นต้นด้วย http:// หรือ https:// และไม่มีช่องว่าง", ephemeral=True)
    me = interaction.guild.me
    if not category.permissions_for(me).manage_channels:
        return await interaction.response.send_message(
            f"❌ บอทไม่มีสิทธิ์ Manage Channels ในหมวดหมู่ **{category.name}**", ephemeral=True)
    log_id = log_channel.id if log_channel else LOG_CHANNEL_ID

    e = discord.Embed(
        title=title or "🎫 ศูนย์บริการลูกค้า",
        description=description or ("กดปุ่มด้านล่างเพื่อเปิด ticket\n\n"
                                    "🛒 **สั่งซื้อสินค้า** กรอกแบบฟอร์ม แล้วทีมงานจะติดต่อกลับ\n"
                                    "❓ **สอบถาม/แจ้งปัญหา** ติดต่อทีมงานได้โดยตรง\n\n"
                                    "*1 คนเปิดได้ครั้งละ 1 ticket*"),
        color=0x5865F2)
    if image:
        e.set_image(url=image.strip())
    view = discord.ui.View(timeout=None)
    view.add_item(OpenTicketButton("order", category.id, staff_role.id, log_id))
    if include_help:
        view.add_item(OpenTicketButton("help", category.id, staff_role.id, log_id))
    try:
        await channel.send(embed=e, view=view)
    except discord.Forbidden:
        return await interaction.response.send_message(
            f"❌ บอทส่งข้อความใน {channel.mention} ไม่ได้", ephemeral=True)
    await interaction.response.send_message(
        f"✅ สร้างแผงใน {channel.mention} แล้ว\n"
        f"หมวดหมู่: **{category.name}** | ทีมงาน: {staff_role.mention} | "
        f"ห้องแจ้งเตือน: <#{log_id}>", ephemeral=True)


@tree.command(name="tickets", description="ดูรายการ ticket ที่เปิดอยู่ทั้งหมด", guild=G)
@app_commands.default_permissions(manage_channels=True)
async def tickets_cmd(interaction: discord.Interaction):
    rows = []
    for ch in interaction.guild.text_channels:
        info = parse_topic(ch)
        if not info:
            continue
        k = KINDS.get(info["kind"], {"emoji": "🎫", "label": info["kind"]})
        who = f"<@{info['claimed']}>" if info["claimed"] else "ยังไม่มีคนรับ"
        rows.append(f"{k['emoji']} {ch.mention} · <@{info['opener']}> · {who} · "
                    f"{discord.utils.format_dt(ch.created_at, 'R')}")
    e = discord.Embed(title=f"🎫 Ticket ที่เปิดอยู่ ({len(rows)})", color=0x5865F2,
                      description=("\n".join(rows)[:4000] if rows else "ไม่มี ticket ที่เปิดอยู่"))
    await interaction.response.send_message(embed=e, ephemeral=True)


@tree.command(name="ticket_add", description="เพิ่มสมาชิกเข้า ticket นี้", guild=G)
@app_commands.default_permissions(manage_channels=True)
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    info = parse_topic(interaction.channel)
    if not info or not is_staff(interaction.user, info["role"]):
        return await interaction.response.send_message("❌ ใช้ได้เฉพาะทีมงานในห้อง ticket", ephemeral=True)
    await interaction.channel.set_permissions(
        member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    await interaction.response.send_message(f"➕ เพิ่ม {member.mention} เข้า ticket แล้ว")


@tree.command(name="ticket_remove", description="เอาสมาชิกออกจาก ticket นี้", guild=G)
@app_commands.default_permissions(manage_channels=True)
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    info = parse_topic(interaction.channel)
    if not info or not is_staff(interaction.user, info["role"]):
        return await interaction.response.send_message("❌ ใช้ได้เฉพาะทีมงานในห้อง ticket", ephemeral=True)
    if member.id == info["opener"]:
        return await interaction.response.send_message("❌ เอาเจ้าของ ticket ออกไม่ได้", ephemeral=True)
    await interaction.channel.set_permissions(member, overwrite=None)
    await interaction.response.send_message(f"➖ เอา {member.mention} ออกจาก ticket แล้ว")


_ticket_ready = False


@client.event
async def on_ready():
    global session, _ticket_ready
    if not _ticket_ready:                      # ให้ปุ่มเก่ายังใช้ได้หลังบอทรีสตาร์ท
        client.add_dynamic_items(OpenTicketButton)
        client.add_view(TicketControls())
        _ticket_ready = True
    if session is None:
        session = aiohttp.ClientSession()
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    if not watcher.is_running():
        watcher.start()
    if WATCH_EVENTS and not events_watcher.is_running():
        events_watcher.start()
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    if not TOKEN or not SUPABASE_KEY:
        raise SystemExit("ตั้งค่า DISCORD_TOKEN และ SUPABASE_KEY ในไฟล์ .env ก่อน")
    client.run(TOKEN)
