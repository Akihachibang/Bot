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
import json
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
                await notify(make_embed("🗑️ ลบคีย์", 0x95A5A6, old))

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
        await interaction.followup.send(f"📦 คีย์ที่ยังไม่ถูกขายเหลือ **{n}** คีย์ (จากทั้งหมด {len(rows)})")
    except Exception as ex:
        print("stock error:", repr(ex))
        await interaction.followup.send("❌ ดึงข้อมูลสต็อกไม่สำเร็จ")


@tree.command(name="downdownload", description="รับลิงก์ดาวน์โหลด",
              guild=discord.Object(id=GUILD_ID))
async def downdownload(interaction: discord.Interaction):
    await interaction.response.send_message(f"⬇️ ดาวน์โหลดได้ที่นี่\n{DOWNLOAD_URL}", ephemeral=True)


@client.event
async def on_ready():
    global session
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
