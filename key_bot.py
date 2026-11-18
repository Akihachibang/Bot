"""
Discord Key Bot (v2 - ตรวจชื่อคอลัมน์อัตโนมัติ)
แจ้งเตือน: สร้างคีย์ / รีเซ็ต HWID / ผูก HWID / ยกเลิกคีย์ / ลบคีย์ / แก้ไขคีย์
คำสั่ง: /chekstock , /downdownload

ติดตั้ง:  pip install discord.py aiohttp python-dotenv
.env:
    DISCORD_TOKEN=...
    SUPABASE_URL=https://cgsuxxyezqhxayllhzfs.supabase.co
    SUPABASE_KEY=...
    ENABLE_MESSAGE_CONTENT=1          # จำเป็น: ให้บอทเห็นรูปสลิปที่แนบใน ticket
    SLIP2GO_SECRET=...                # Secret Key จากหน้า API Connect > Authentication ของ Slip2Go
    RECEIVER_PHONE=0XXXXXXXXX         # เบอร์พร้อมเพย์/TrueMoney ที่รับเงิน (ใช้เช็คว่าสลิปโอนเข้าบัญชีเรา)
"""
import os
import re
import json
import asyncio
import base64
import io
import datetime as dt
from zoneinfo import ZoneInfo

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
SHOP_TIMEZONE = ZoneInfo("Asia/Bangkok")
SHOP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop_hours.json")
SHOP_CONFIGS: dict[str, dict] = {}

# รูป QR ชำระเงิน (แสดงใน ticket สั่งซื้อ)
# ถ้ามีไฟล์ qr.png วางข้าง key_bot.py บอทจะใช้ไฟล์นี้ก่อน (ไม่มีวันหมดอายุ แนะนำวิธีนี้)
# ถ้าไม่มี จะใช้ลิงก์ด้านล่าง (ลิงก์ Discord CDN มีวันหมดอายุ)
QR_LOCAL_FILE = "qr.png"
# จำกัดขนาด/จำนวนรูปสลิปที่บอทส่งต่อให้ทีมงาน เพื่อลดการใช้หน่วยความจำบน Railway
MAX_SLIP_BYTES = 8 * 1024 * 1024
MAX_SLIP_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SLIP_FILES = 4
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# ---------- ตรวจสลิปอัตโนมัติด้วย Slip2Go ----------
SLIP2GO_API_URL = os.getenv("SLIP2GO_API_URL", "https://connect.slip2go.com").rstrip("/")
SLIP2GO_SECRET = os.getenv("SLIP2GO_SECRET", "").strip()
RECEIVER_PHONE = re.sub(r"\D", "", os.getenv("RECEIVER_PHONE", ""))  # เลขล้วน ไม่มีขีด/เว้นวรรค
# ต้องมีทั้งคีย์และเบอร์ผู้รับ ถึงจะเปิดตรวจอัตโนมัติ (ไม่มีเบอร์ผู้รับ = สลิปโอนให้ใครก็ผ่าน จึงไม่เปิดให้)
AUTO_VERIFY = bool(SLIP2GO_SECRET and RECEIVER_PHONE)
SLIP_TIME_SLACK_MINUTES = 2     # สลิปต้องโอนหลังเปิด ticket (เผื่อเวลาบนสลิปที่ปัดเป็นนาที)
MAX_VERIFY_PER_TICKET = 5       # จำกัดจำนวนครั้งที่ยิง API ต่อ 1 ticket กันลูกค้าส่งรูปรัวจนโควตาหมด
SLIP_OK_CODES = {"200000", "200200"}
# รหัสที่แปลว่า "รูปมีปัญหา" ให้ลูกค้าส่งใหม่ ไม่ต้องรบกวนทีมงาน
SLIP_RETRY = {
    "400001": "อ่าน QR บนสลิปไม่ได้ กรุณาส่งรูปสลิปเต็มใบที่เห็น QR ชัดเจน (ไม่ครอป ไม่เบลอ)",
    "400002": "ไฟล์รูปไม่ถูกต้อง กรุณาส่งใหม่เป็นรูปสลิปจากแอปธนาคาร",
    "400005": "ไฟล์รูปไม่ถูกต้อง กรุณาส่งใหม่เป็นรูปสลิปจากแอปธนาคาร",
}
# รหัสที่แปลว่า "สลิปไม่ผ่านเงื่อนไข" ต้องแจ้งทีมงานด้วย
SLIP_REJECT = {
    "200401": "สลิปนี้ไม่ได้โอนเข้าบัญชีของร้าน",
    "200403": "วันเวลาโอนไม่ถูกต้อง (ต้องโอนหลังเปิด ticket นี้)",
    "200404": "ไม่พบรายการนี้ในระบบธนาคาร",
    "200500": "สลิปไม่ผ่านการตรวจสอบ กรุณาติดต่อทีมงาน",
    "200501": "สลิปนี้ถูกใช้งานไปแล้ว",
}
verify_attempts: dict[int, int] = {}
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
review_counts: dict[int, int] = {}
TOPIC_SYNC_COOLDOWN_SECONDS = 360  # Discord กลับมาอนุญาต PATCH หลัง retry_after พร้อม buffer
review_topic_pending_at: dict[int, float] = {}
review_topic_tasks: dict[int, asyncio.Task] = {}


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


# ---------- สถานะเวลาเปิด-ปิดร้าน ----------
def load_shop_configs():
    """โหลดเวลาเปิด-ปิดและห้องแจ้งเตือนจากไฟล์ เพื่อให้คงอยู่หลังรีสตาร์ต"""
    global SHOP_CONFIGS
    try:
        with open(SHOP_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        SHOP_CONFIGS = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        SHOP_CONFIGS = {}
    except (OSError, json.JSONDecodeError) as ex:
        print("shop config load error:", repr(ex))
        SHOP_CONFIGS = {}


def save_shop_configs():
    temp_path = SHOP_CONFIG_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(SHOP_CONFIGS, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, SHOP_CONFIG_PATH)


load_shop_configs()


def shop_is_open(config: dict, now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(SHOP_TIMEZONE)).astimezone(SHOP_TIMEZONE)
    open_at = dt.datetime.strptime(config["open_time"], "%H:%M").time()
    close_at = dt.datetime.strptime(config["close_time"], "%H:%M").time()
    current = now.time().replace(tzinfo=None)
    if open_at < close_at:
        return open_at <= current < close_at
    # รองรับเวลาทำการข้ามเที่ยงคืน เช่น 20:00 ถึง 02:00
    return current >= open_at or current < close_at


def shop_status_message(config: dict, is_open: bool) -> str:
    if is_open:
        return (f"🟢 **ร้านเปิดให้บริการแล้ว**\n"
                f"เวลาเปิด `{config['open_time']}` น. · ปิด `{config['close_time']}` น.")
    return (f"🔴 **ร้านปิดให้บริการแล้ว**\n"
            f"เวลาเปิด `{config['open_time']}` น. · ปิด `{config['close_time']}` น.")


LAST_SHOP_PRESENCE_STATUS: str | None = None


async def update_shop_presence(config: dict, force: bool = False):
    """ตั้ง activity ของบอทตามเวลาเปิด-ปิด โดยใช้เวลาประเทศไทย"""
    global LAST_SHOP_PRESENCE_STATUS
    is_open = shop_is_open(config)
    status = "open" if is_open else "closed"
    if not force and LAST_SHOP_PRESENCE_STATUS == status:
        return

    label = "เปิดให้บริการ🟢" if is_open else "ปิดให้บริการ🔴"
    await client.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name=label),
    )
    LAST_SHOP_PRESENCE_STATUS = status


@tasks.loop(seconds=30)
async def shop_status_watcher():
    """ตรวจสถานะทุก 30 วินาที อัปเดต activity และแจ้งเมื่อร้านเปลี่ยนสถานะ"""
    presence_config = SHOP_CONFIGS.get(str(GUILD_ID))
    if presence_config:
        try:
            await update_shop_presence(presence_config)
        except Exception as ex:
            print("shop presence update error:", repr(ex))

    changed = False
    for guild_id, config in list(SHOP_CONFIGS.items()):
        try:
            channel_id = int(config["channel_id"])
            is_open = shop_is_open(config)
            status = "open" if is_open else "closed"
            if config.get("last_status") == status:
                continue

            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            await channel.send(shop_status_message(config, is_open))
            config["last_status"] = status
            changed = True
        except discord.NotFound:
            print(f"shop status channel not found: guild={guild_id}")
        except discord.Forbidden:
            print(f"shop status send forbidden: guild={guild_id}")
        except Exception as ex:
            print(f"shop status error (guild={guild_id}):", repr(ex))
    if changed:
        save_shop_configs()


@tree.command(name="settime", description="ตั้งเวลาเปิด-ปิดร้านและห้องแจ้งเตือน",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    open_time="เวลาเปิดร้าน รูปแบบ 24 ชั่วโมง เช่น 09:00",
    close_time="เวลาปิดร้าน รูปแบบ 24 ชั่วโมง เช่น 18:30",
    channel="ห้องที่จะประกาศสถานะร้าน",
)
@app_commands.default_permissions(administrator=True)
async def settime(interaction: discord.Interaction, open_time: str, close_time: str,
                  channel: discord.TextChannel):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", open_time.strip()):
        return await interaction.response.send_message(
            "❌ เวลาเปิดไม่ถูกต้อง กรุณาใช้รูปแบบ `HH:MM` เช่น `09:00`", ephemeral=True)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", close_time.strip()):
        return await interaction.response.send_message(
            "❌ เวลาปิดไม่ถูกต้อง กรุณาใช้รูปแบบ `HH:MM` เช่น `18:30`", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("❌ ใช้คำสั่งนี้ในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)

    open_time, close_time = open_time.strip(), close_time.strip()
    if open_time == close_time:
        return await interaction.response.send_message(
            "❌ เวลาเปิดและเวลาปิดต้องไม่ตรงกัน", ephemeral=True)
    bot_member = interaction.guild.me
    if bot_member is None or not channel.permissions_for(bot_member).send_messages:
        return await interaction.response.send_message(
            f"❌ บอทไม่มีสิทธิ์ส่งข้อความใน {channel.mention}", ephemeral=True)

    config = {
        "open_time": open_time,
        "close_time": close_time,
        "channel_id": channel.id,
    }
    status_now = shop_is_open(config)
    config["last_status"] = "open" if status_now else "closed"
    SHOP_CONFIGS[str(interaction.guild.id)] = config
    await interaction.response.defer(ephemeral=True)
    try:
        save_shop_configs()
        await channel.send(shop_status_message(config, status_now))
        if str(interaction.guild.id) == str(GUILD_ID):
            try:
                await update_shop_presence(config, force=True)
            except Exception as presence_ex:
                print("shop presence update error:", repr(presence_ex))
    except (OSError, discord.HTTPException) as ex:
        print("settime error:", repr(ex))
        return await interaction.followup.send(
            "❌ บันทึกการตั้งค่าหรือส่งประกาศไม่สำเร็จ ตรวจสอบสิทธิ์บอทและลองอีกครั้ง", ephemeral=True)

    await interaction.followup.send(
        f"✅ ตั้งเวลาแล้ว (เวลาไทย): เปิด `{open_time}` · ปิด `{close_time}`\n"
        f"จะแจ้งสถานะใน {channel.mention} เมื่อร้านเปิดหรือปิด", ephemeral=True)


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
                    value="สแกน QR ด้านล่างเพื่อโอน แล้วแนบรูปสลิปเป็นไฟล์รูปในห้องนี้ บอทจะส่งต่อให้ทีมงานตรวจสอบ", inline=False)
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



class SlipReviewButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"tk:slip:(?P<action>approve|reject):(?P<ticket>\d+)"):
    """ปุ่มให้ทีมงานยืนยัน/ปฏิเสธสลิป โดยเก็บ ID ห้อง ticket ใน custom_id"""
    def __init__(self, action: str, ticket_id: int):
        self.action, self.ticket_id = action, ticket_id
        if action == "approve":
            label, style, emoji = "ยืนยันรับชำระแล้ว", discord.ButtonStyle.success, "✅"
        else:
            label, style, emoji = "ขอส่งสลิปใหม่", discord.ButtonStyle.danger, "❌"
        super().__init__(discord.ui.Button(
            label=label, style=style, emoji=emoji,
            custom_id=f"tk:slip:{action}:{ticket_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction):
        ticket = interaction.guild.get_channel(self.ticket_id) if interaction.guild else None
        info = parse_topic(ticket) if ticket else None
        if not info or info.get("kind") != "order":
            return await interaction.response.send_message(
                "❌ ห้อง ticket ไม่พบหรือปิดไปแล้ว", ephemeral=True)
        if not is_staff(interaction.user, info["role"]):
            return await interaction.response.send_message(
                "❌ เฉพาะทีมงานเท่านั้น", ephemeral=True)

        if not interaction.message.embeds:
            return await interaction.response.send_message(
                "❌ ไม่พบข้อมูลสลิปในข้อความนี้", ephemeral=True)
        e = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
        old_footer = e.footer.text or ""
        if old_footer.startswith("ผลตรวจ:"):
            return await interaction.response.send_message(
                "⚠️ สลิปนี้มีผลตรวจแล้ว", ephemeral=True)

        if self.action == "approve":
            status = f"ทีมงานยืนยันรับชำระแล้ว โดย {interaction.user.display_name}"
            ticket_text = f"✅ ทีมงาน {interaction.user.mention} ตรวจสลิปและยืนยันว่าได้รับชำระแล้ว"
        else:
            status = f"ทีมงานขอให้ส่งสลิปใหม่ โดย {interaction.user.display_name}"
            ticket_text = f"❌ ทีมงาน {interaction.user.mention} ขอให้ตรวจสอบและส่งสลิปใหม่อีกครั้ง"
        e.set_footer(text=f"ผลตรวจ: {status}")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="ยืนยันรับชำระแล้ว", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"tk:slip:approve:{self.ticket_id}", disabled=True))
        view.add_item(discord.ui.Button(
            label="ขอส่งสลิปใหม่", style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"tk:slip:reject:{self.ticket_id}", disabled=True))
        await interaction.response.edit_message(embed=e, view=view)
        try:
            await ticket.send(ticket_text)
        except discord.HTTPException as ex:
            print("slip decision ticket notification error:", repr(ex))


class SlipReviewView(discord.ui.View):
    def __init__(self, ticket_id: int):
        super().__init__(timeout=None)
        self.add_item(SlipReviewButton("approve", ticket_id))
        self.add_item(SlipReviewButton("reject", ticket_id))


REVIEW_TOPIC_RE = re.compile(r"เครดิต\s*[:：]\s*(\d+)")
review_topic_lock = asyncio.Lock()


def format_review_topic(topic: str, count: int) -> str:
    """แทน/เพิ่มตัวนับเครดิตใน Topic โดยคงข้อความอื่นและไม่เกิน 1024 ตัวอักษร"""
    marker = f"เครดิต: {count}"
    match = REVIEW_TOPIC_RE.search(topic)
    if match:
        return topic[:match.start()] + marker + topic[match.end():]
    separator = " | " if topic.strip() else ""
    room = max(0, 1024 - len(separator) - len(marker))
    return topic[:room].rstrip() + separator + marker


async def ensure_review_topic_counter(channel: discord.TextChannel) -> int:
    async with review_topic_lock:
        topic = channel.topic or ""
        match = REVIEW_TOPIC_RE.search(topic)
        if match:
            count = int(match.group(1))
            review_counts[channel.id] = count
            return count
        await channel.edit(topic=format_review_topic(topic, 0),
                           reason="ตั้งตัวนับจำนวนเครดิตรีวิว")
        review_counts[channel.id] = 0
        return 0


async def count_review_posts(channel: discord.TextChannel) -> int:
    """นับรีวิวจากข้อความที่บอทส่งไว้ ใช้กู้จำนวนหลัง Railway รีสตาร์ต"""
    if not client.user:
        return 0
    total = 0
    async for message in channel.history(limit=None, oldest_first=True):
        if message.author.id == client.user.id and any(
            embed.title == "⭐ รีวิวใหม่" for embed in message.embeds
        ):
            total += 1
    return total


def queue_review_topic_sync(channel: discord.TextChannel):
    """รวมการแก้ Topic ให้ไม่เกินหนึ่งครั้งต่อช่วง cooldown"""
    loop = asyncio.get_running_loop()
    channel_id = channel.id
    review_topic_pending_at[channel_id] = loop.time()
    task = review_topic_tasks.get(channel_id)
    if task is None or task.done():
        review_topic_tasks[channel_id] = asyncio.create_task(_delayed_review_topic_sync(channel))


async def _delayed_review_topic_sync(channel: discord.TextChannel):
    channel_id = channel.id
    try:
        while channel_id in review_topic_pending_at:
            loop = asyncio.get_running_loop()
            requested_at = review_topic_pending_at[channel_id]
            await asyncio.sleep(max(0, requested_at + TOPIC_SYNC_COOLDOWN_SECONDS - loop.time()))
            if review_topic_pending_at.get(channel_id) != requested_at:
                continue  # มีรีวิวใหม่ ให้รอครบ cooldown นับจากครั้งล่าสุด
            try:
                count = await count_review_posts(channel)
                topic = channel.topic or ""
                match = REVIEW_TOPIC_RE.search(topic)
                old_count = int(match.group(1)) if match else None
                if old_count != count:
                    await channel.edit(
                        topic=format_review_topic(topic, count),
                        reason="อัปเดตจำนวนรีวิวแบบจำกัดความถี่")
                review_counts[channel_id] = count
                print(f"[review] topic synced: channel={channel_id}, count={count}")
            except Exception as ex:
                print(f"review topic sync error (channel={channel_id}):", repr(ex))
                # ถ้า Discord ยังจำกัดอยู่ ให้รออีกหนึ่งรอบ cooldown ก่อนลองใหม่
                review_topic_pending_at[channel_id] = loop.time()
                continue
            if review_topic_pending_at.get(channel_id) == requested_at:
                review_topic_pending_at.pop(channel_id, None)
                return
    finally:
        if review_topic_tasks.get(channel_id) is asyncio.current_task():
            review_topic_tasks.pop(channel_id, None)


async def increment_review_topic_counter(channel: discord.TextChannel) -> int:
    async with review_topic_lock:
        count = review_counts.get(channel.id)
        if count is None:
            # Topic อาจยังไม่ทัน sync ก่อน Railway รีสตาร์ต; recount จากรีวิวจริง
            count = await count_review_posts(channel)
        else:
            count += 1
        review_counts[channel.id] = count
        queue_review_topic_sync(channel)
        return count



class ReviewModal(discord.ui.Modal):
    def __init__(self, destination_id: int):
        super().__init__(title="เขียนรีวิว")
        self.destination_id = destination_id
        self.review_text = discord.ui.TextInput(
            label="ข้อความรีวิว", placeholder="พิมพ์รีวิวของคุณที่นี่",
            style=discord.TextStyle.paragraph, required=True, min_length=2,
            max_length=1000)
        self.add_item(self.review_text)

    async def on_submit(self, interaction: discord.Interaction):
        # ตอบรับ interaction ทันที ป้องกัน Discord timeout ระหว่างส่งรีวิว/แก้ Topic
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.guild.get_channel(self.destination_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send(
                "❌ ไม่พบห้องสำหรับส่งรีวิว กรุณาแจ้งแอดมิน", ephemeral=True)

        e = discord.Embed(
            title="⭐ รีวิวใหม่",
            description=self.review_text.value.strip(),
            color=0xF1C40F, timestamp=dt.datetime.now(dt.timezone.utc))
        e.add_field(name="ผู้รีวิว", value=f"{interaction.user.mention} (`{interaction.user.id}`)",
                    inline=False)
        e.set_author(name=interaction.user.display_name,
                     icon_url=interaction.user.display_avatar.url)
        try:
            await channel.send(embed=e)
        except Exception as ex:
            print("review send error:", repr(ex))
            return await interaction.followup.send(
                "❌ ส่งรีวิวไม่สำเร็จ กรุณาลองใหม่หรือติดต่อแอดมิน", ephemeral=True)

        try:
            count = await increment_review_topic_counter(channel)
        except Exception as ex:
            print(f"review topic counter update error (channel={channel.id}):", repr(ex))
            return await interaction.followup.send(
                "✅ รีวิวถูกส่งแล้ว แต่แก้ Channel Topic ไม่สำเร็จ "
                "กรุณาตรวจสิทธิ์ Manage Channels ของบอทในห้องรีวิว", ephemeral=True)
        print(f"[review] updated topic counter: channel={channel.id}, count={count}")
        await interaction.followup.send(
            f"✅ ส่งรีวิวเรียบร้อย ขอบคุณมากครับ/ค่ะ (เครดิตรีวิว: {count})", ephemeral=True)



class ReviewPanelButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"review:submit:(?P<destination>\d+)"):
    """ปุ่มเปิดฟอร์มรีวิว โดยเก็บ ID ห้องปลายทางใน custom_id"""
    def __init__(self, destination_id: int):
        self.destination_id = destination_id
        super().__init__(discord.ui.Button(
            label="เขียนรีวิว", style=discord.ButtonStyle.primary, emoji="⭐",
            custom_id=f"review:submit:{destination_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["destination"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReviewModal(self.destination_id))


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



@tree.command(name="review_panel", description="สร้างแผงให้สมาชิกส่งรีวิว", guild=G)
@app_commands.describe(
    panel_channel="ห้องที่จะวางแผงรีวิว",
    review_channel="ห้องที่จะรับข้อความรีวิว",
    title="หัวข้อบนแผง (ไม่ใส่ = ใช้ค่าเริ่มต้น)",
    description="คำอธิบายบนแผง (ไม่ใส่ = ใช้ค่าเริ่มต้น)")
@app_commands.default_permissions(administrator=True)
async def review_panel(interaction: discord.Interaction,
                       panel_channel: discord.TextChannel,
                       review_channel: discord.TextChannel,
                       title: str | None = None,
                       description: str | None = None):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ เฉพาะแอดมินเท่านั้น", ephemeral=True)
    bot_member = interaction.guild.me
    if not review_channel.permissions_for(bot_member).manage_channels:
        return await interaction.response.send_message(
            f"❌ กรุณาให้บอทมีสิทธิ์ Manage Channels ใน {review_channel.mention} "
            "เพื่อเขียนจำนวนรีวิวใน Channel Topic", ephemeral=True)
    e = discord.Embed(
        title=title or "⭐ รีวิวจากลูกค้า",
        description=description or "กดปุ่มด้านล่างเพื่อเขียนรีวิว ข้อความจะถูกส่งไปยังห้องรีวิว",
        color=0x5865F2)
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewPanelButton(review_channel.id))
    try:
        await panel_channel.send(embed=e, view=view)
    except discord.Forbidden:
        return await interaction.response.send_message(
            f"❌ บอทส่งข้อความใน {panel_channel.mention} ไม่ได้", ephemeral=True)
    except discord.HTTPException as ex:
        print("review panel error:", repr(ex))
        return await interaction.response.send_message("❌ สร้างแผงรีวิวไม่สำเร็จ", ephemeral=True)
    try:
        count = await ensure_review_topic_counter(review_channel)
    except discord.HTTPException as ex:
        print("review topic initialization error:", repr(ex))
        return await interaction.response.send_message(
            f"✅ สร้างแผงแล้ว แต่ตั้ง Topic ใน {review_channel.mention} ไม่สำเร็จ",
            ephemeral=True)
    await interaction.response.send_message(
        f"✅ สร้างแผงใน {panel_channel.mention} แล้ว; รีวิวจะส่งไป {review_channel.mention} "
        f"(เครดิตรีวิวปัจจุบัน: {count})", ephemeral=True)


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



# ---------- ตรวจสลิปอัตโนมัติ (Slip2Go) ----------
def receiver_conditions() -> list[dict]:
    """บัญชีที่ยอมรับ: พร้อมเพย์เบอร์โทร หรือ TrueMoney Wallet เบอร์เดียวกัน (ตรงอย่างใดอย่างหนึ่งถือว่าผ่าน)"""
    return [{"accountType": "02001", "accountNumber": RECEIVER_PHONE},
            {"accountType": "04000", "accountNumber": RECEIVER_PHONE}]


def guess_mime(att: discord.Attachment) -> str:
    ct = (att.content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    ext = os.path.splitext(att.filename.lower())[1]
    return {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")


async def verify_slip_image(data: bytes, mime: str, not_before: dt.datetime) -> dict:
    """ส่งรูปสลิปไปตรวจที่ Slip2Go (base64) พร้อมเงื่อนไข: ไม่ซ้ำ + ผู้รับตรง + โอนหลังเปิด ticket
    คืน dict ที่มี key 'code' เสมอ; ถ้าเรียก API ไม่ได้คืน code='ERROR'"""
    nb = not_before.astimezone(dt.timezone.utc) - dt.timedelta(minutes=SLIP_TIME_SLACK_MINUTES)
    body = {"payload": {
        "imageBase64": f"data:{mime};base64,{base64.b64encode(data).decode()}",
        "checkCondition": {
            "checkDuplicate": True,
            "checkReceiver": receiver_conditions(),
            "checkDate": {"type": "gte", "date": nb.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
        },
    }}
    try:
        async with session.post(
                f"{SLIP2GO_API_URL}/api/verify-slip/qr-base64/info", json=body,
                headers={"Authorization": f"Bearer {SLIP2GO_SECRET}"},
                timeout=aiohttp.ClientTimeout(total=30)) as r:
            try:
                js = await r.json(content_type=None)
            except Exception:
                js = None
            if not isinstance(js, dict) or "code" not in js:
                return {"code": "ERROR", "message": f"HTTP {r.status}"}
            return js
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        return {"code": "ERROR", "message": repr(ex)}


async def check_one_slip(channel: discord.TextChannel, att: discord.Attachment, data: bytes):
    """คืน (kind, js, note) โดย kind = ok | retry | reject | manual"""
    if not AUTO_VERIFY:
        return "manual", None, "ยังไม่ได้ตั้งค่าตรวจอัตโนมัติ (SLIP2GO_SECRET / RECEIVER_PHONE)"
    used = verify_attempts.get(channel.id, 0)
    if used >= MAX_VERIFY_PER_TICKET:
        return "manual", None, f"ตรวจอัตโนมัติครบ {MAX_VERIFY_PER_TICKET} ครั้งต่อ ticket แล้ว โปรดตรวจเอง"
    verify_attempts[channel.id] = used + 1

    js = await verify_slip_image(data, guess_mime(att), channel.created_at)
    code = str(js.get("code"))
    if code in SLIP_OK_CODES:
        return "ok", js, None
    if code in SLIP_RETRY:
        return "retry", js, SLIP_RETRY[code]
    if code in SLIP_REJECT:
        return "reject", js, SLIP_REJECT[code]
    print(f"[slip2go] ตรวจไม่สำเร็จ code={code} message={js.get('message')}")
    note = f"ตรวจอัตโนมัติไม่สำเร็จ ({code}: {js.get('message', '')}) โปรดตรวจเอง"
    if code in ("401004", "401005", "401006"):
        note += " | ⚠️ แพ็กเกจ/โทเคน Slip2Go หมด ต้องเติมก่อน"
    return "manual", js, note


def fmt_slip_time(iso: str | None) -> str:
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return t.astimezone(TZ_TH).strftime("%d/%m/%Y %H:%M") + " น."
    except Exception:
        return str(iso or "-")


def slip_embed(message: discord.Message, kind: str, js: dict | None, note: str | None) -> discord.Embed:
    titles = {"ok": ("✅ ตรวจสลิปอัตโนมัติผ่านแล้ว", 0x2ECC71),
              "reject": ("🚫 สลิปไม่ผ่านการตรวจอัตโนมัติ", 0xE74C3C),
              "manual": ("💳 ได้รับรูปสลิป (ต้องตรวจเอง)", 0xF1C40F)}
    title, color = titles[kind]
    e = discord.Embed(
        title=title, color=color, timestamp=dt.datetime.now(dt.timezone.utc),
        description=f"ผู้ส่ง: {message.author.mention} (`{message.author.id}`)\n"
                    f"Ticket: {message.channel.mention}")
    if note:
        e.add_field(name="หมายเหตุ", value=note[:1000], inline=False)
    d = (js or {}).get("data")
    if isinstance(d, dict) and d.get("amount") is not None:
        try:
            e.add_field(name="💰 ยอดโอน", value=f"**{float(d['amount']):,.2f}** บาท", inline=True)
        except (TypeError, ValueError):
            pass
        e.add_field(name="🕒 เวลาโอน", value=fmt_slip_time(d.get("dateTime")), inline=True)
        e.add_field(name="🔖 เลขอ้างอิง", value=f"`{d.get('transRef', '-')}`", inline=False)
        snd = (d.get("sender") or {})
        rcv = (d.get("receiver") or {})
        e.add_field(name="ผู้โอน",
                    value=f"{(snd.get('account') or {}).get('name', '-')}\n"
                          f"{(snd.get('bank') or {}).get('name', '')}", inline=True)
        e.add_field(name="ผู้รับ",
                    value=f"{(rcv.get('account') or {}).get('name', '-')}\n"
                          f"{(rcv.get('bank') or {}).get('name', '')}", inline=True)
    if js and js.get("code"):
        e.add_field(name="รหัสผลตรวจ", value=f"`{js['code']}` {js.get('message', '')}"[:200], inline=False)
    foot = {"ok": "ตรวจกับระบบธนาคารผ่าน Slip2Go แล้ว · ทีมงานเทียบยอดกับออเดอร์ แล้วกดยืนยัน",
            "reject": "สลิปไม่ผ่านเงื่อนไข จึงไม่มีปุ่มยืนยัน",
            "manual": "ระบบไม่ได้ตรวจอัตโนมัติ · โปรดตรวจกับแอปธนาคารเอง"}[kind]
    e.set_footer(text=foot)
    return e


@client.event
async def on_message(message: discord.Message):
    """รับรูปจากเจ้าของ order ticket → ตรวจสลิปอัตโนมัติ → ส่งผลให้ทีมงานกดยืนยัน"""
    if message.author.bot or not isinstance(message.channel, discord.TextChannel):
        return
    info = parse_topic(message.channel)
    if not info or info.get("kind") != "order" or message.author.id != info["opener"]:
        return

    attachments = [a for a in message.attachments
                   if (a.content_type or "").lower().startswith("image/")
                   or a.filename.lower().endswith(IMAGE_EXTENSIONS)]
    if not attachments:
        return
    if any(a.size > MAX_SLIP_BYTES for a in attachments):
        return await message.channel.send(
            f"❌ รูปสลิปต้องมีขนาดไม่เกิน {MAX_SLIP_BYTES // (1024 * 1024)} MB ต่อรูป กรุณาส่งใหม่")
    if len(attachments) > MAX_SLIP_FILES or sum(a.size for a in attachments) > MAX_SLIP_TOTAL_BYTES:
        return await message.channel.send(
            f"❌ ส่งได้ไม่เกิน {MAX_SLIP_FILES} รูป และรวมกันไม่เกิน {MAX_SLIP_TOTAL_BYTES // (1024 * 1024)} MB ต่อครั้ง")

    log_ch = message.guild.get_channel(info["log"]) if message.guild else None
    if log_ch is None and message.guild:
        try:
            log_ch = await message.guild.fetch_channel(info["log"])
        except discord.HTTPException:
            log_ch = None
    if not isinstance(log_ch, discord.TextChannel):
        return await message.channel.send("❌ ไม่พบห้องทีมงานสำหรับตรวจสลิป กรุณาแจ้งแอดมิน")

    lines: list[str] = []
    had_reject = False
    try:
        async with message.channel.typing():
            for i, att in enumerate(attachments, 1):
                label = f"สลิปที่ {i}" if len(attachments) > 1 else "สลิป"
                try:
                    data = await att.read()
                except discord.HTTPException as ex:
                    print("slip download error:", repr(ex))
                    lines.append(f"❌ {label}: ดาวน์โหลดรูปไม่สำเร็จ กรุณาส่งใหม่")
                    continue

                kind, js, note = await check_one_slip(message.channel, att, data)

                if kind == "retry":                      # รูปมีปัญหา: บอกลูกค้าอย่างเดียว
                    lines.append(f"⚠️ {label}: {note}")
                    continue

                e = slip_embed(message, kind, js, note)
                view = SlipReviewView(message.channel.id) if kind in ("ok", "manual") else None
                f = discord.File(io.BytesIO(data), filename=att.filename)
                try:
                    kwargs = {"view": view} if view else {}
                    await log_ch.send(
                        content=f"<@&{info['role']}>", embed=e, file=f,
                        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                        **kwargs)
                except discord.HTTPException as ex:
                    print("slip forwarding error:", repr(ex))
                    lines.append(f"❌ {label}: ส่งให้ทีมงานไม่สำเร็จ กรุณาแจ้งทีมงานใน ticket นี้")
                    continue

                if kind == "ok":
                    amt = ""
                    d = (js or {}).get("data") or {}
                    try:
                        amt = f" ยอด **{float(d['amount']):,.2f}** บาท"
                    except (KeyError, TypeError, ValueError):
                        pass
                    lines.append(f"✅ {label}: ตรวจสำเร็จ{amt}")
                elif kind == "reject":
                    had_reject = True
                    lines.append(f"❌ {label}: {note}")
                else:
                    lines.append(f"⏳ {label}: ส่งให้ทีมงานตรวจสอบแล้ว")
    except Exception as ex:
        print("slip handler error:", repr(ex))
        return await message.channel.send("❌ เกิดข้อผิดพลาดระหว่างตรวจสลิป กรุณาแจ้งทีมงานใน ticket นี้")

    if any(l.startswith(("✅", "⏳")) for l in lines):
        lines.append("กรุณารอทีมงานยืนยันใน ticket นี้")
    elif had_reject:
        lines.append("หากคิดว่าระบบตรวจผิด แจ้งทีมงานใน ticket นี้ได้เลย")
    if lines:
        await message.channel.send(f"{message.author.mention}\n" + "\n".join(lines))


_ticket_ready = False


@client.event
async def on_ready():
    global session, _ticket_ready
    if not _ticket_ready:                      # ให้ปุ่มเก่ายังใช้ได้หลังบอทรีสตาร์ท
        client.add_dynamic_items(OpenTicketButton, SlipReviewButton, ReviewPanelButton)
        client.add_view(TicketControls())
        _ticket_ready = True
    if session is None:
        session = aiohttp.ClientSession()
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    if not watcher.is_running():
        watcher.start()
    if WATCH_EVENTS and not events_watcher.is_running():
        events_watcher.start()
    if not shop_status_watcher.is_running():
        shop_status_watcher.start()
    presence_config = SHOP_CONFIGS.get(str(GUILD_ID))
    if presence_config:
        try:
            await update_shop_presence(presence_config, force=True)
        except Exception as ex:
            print("shop presence update error:", repr(ex))
    # กู้ Topic ที่อาจค้างก่อน Railway รีสตาร์ต โดยยังเคารพ cooldown ของ Discord
    for guild in client.guilds:
        for ch in guild.text_channels:
            if REVIEW_TOPIC_RE.search(ch.topic or ""):
                queue_review_topic_sync(ch)
    if AUTO_VERIFY:
        print(f"[slip2go] เปิดตรวจสลิปอัตโนมัติ (เช็คผู้รับ ***{RECEIVER_PHONE[-4:]})")
    else:
        print("[slip2go] ปิดตรวจอัตโนมัติ: ต้องตั้งทั้ง SLIP2GO_SECRET และ RECEIVER_PHONE (ตอนนี้ส่งสลิปให้ทีมงานตรวจเอง)")
    if not intents.message_content:
        print("[ticket] เปิด Message Content Intent ใน Discord Developer Portal และตั้ง ENABLE_MESSAGE_CONTENT=1 เพื่อให้บอทตรวจพบไฟล์แนบใน ticket")
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    if not TOKEN or not SUPABASE_KEY:
        raise SystemExit("ตั้งค่า DISCORD_TOKEN และ SUPABASE_KEY ในไฟล์ .env ก่อน")
    client.run(TOKEN)
