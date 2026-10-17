"""Хранилище кланов, шифрованные бэкапы, синхронизация с сайтом."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import config
from config import GUILD_ID

log = logging.getLogger("clans")

COLORS = {"red": 0xC5162D, "dark": 0x151012, "green": 0x169B62, "grey": 0x626262}
ROLE_KEYS = ["leader", "deputy", "officer", "fighter", "recruit"]
ROLE_LABELS = {"leader": "Лидер", "deputy": "Заместитель", "officer": "Офицер", "fighter": "Боец", "recruit": "Рекрут"}
IMAGE_URL = re.compile(r"^https://(?:cdn\.discordapp\.com|media\.discordapp\.net)/attachments/\d{17,20}/\d{17,20}/[A-Za-z0-9_.\-]{1,200}(?:\?[^\s]{1,300})?$")
INVITE_URL = re.compile(r"^https://(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9\-]{2,64}$")
CLAN_LOGO_MAX_BYTES = 25 * 1024 * 1024
ROLE_ICON_MAX_BYTES = 256 * 1024
CLAN_LOGO_MIMES = {"image/png", "image/jpeg", "image/webp"}
BACKUP_FORMAT = "wardogs-clans-backup"
BACKUP_FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
STATE_FILE = DATA_DIR / "clan_state.json"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DATA_DIR / "backups"))).resolve()

CLAN_SYNC_URL = config.CLAN_SYNC_URL
CLAN_SYNC_SECRET = config.CLAN_SYNC_SECRET
CLAN_BACKUP_SECRET = config.CLAN_BACKUP_SECRET
CLAN_BACKUP_INTERVAL_HOURS = config.CLAN_BACKUP_INTERVAL_HOURS
CLAN_BACKUP_RETENTION = config.CLAN_BACKUP_RETENTION
CLAN_ADMIN_ROLE_ID = config.CLAN_ADMIN_ROLE_ID
CLAN_BACKUP_CHANNEL_ID = config.CLAN_BACKUP_CHANNEL_ID


def clean(value, max_len=2000):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).replace("  ", " ").strip()[:max_len]


def slug(value, max_len=80):
    result = clean(value, max_len).lower()
    result = re.sub(r"[^\w\-]+", "-", result, flags=re.UNICODE)
    result = result.strip("-")[:max_len]
    return result or "clan"


def detect_image_mime(data: bytes):
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_timestamp(dt: datetime | None = None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def blank_state():
    return {
        "version": 1,
        "setup": {},
        "clans": {},
        "drafts": {},
        "applications": {},
        "joinApplications": {},
        "temporaryVoices": {},
        "backup": {},
        "updatedAt": datetime.fromtimestamp(0, tz=timezone.utc).isoformat(),
    }


def validate_state(state):
    if not state or state.get("version") != 1:
        raise ValueError("Unsupported state schema")
    state.setdefault("joinApplications", {})
    for key in ["setup", "clans", "drafts", "applications", "joinApplications", "temporaryVoices"]:
        if not isinstance(state.get(key), dict) or isinstance(state[key], list):
            raise ValueError(f"Invalid state field: {key}")
    base = blank_state()
    for k, v in state.items():
        if k in base:
            base[k] = v
    return base


class ClanStore:
    def __init__(self):
        self._state = blank_state()
        self._lock = asyncio.Lock()

    @property
    def state(self):
        return self._state

    async def load(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            text = STATE_FILE.read_text(encoding="utf-8")
            self._state = validate_state(json.loads(text))
        except FileNotFoundError:
            await self._write_atomic()
        return self._state

    def snapshot(self):
        return json.loads(json.dumps(self._state))

    def find_clan_by_member_ids(self, role_ids: set) -> dict | None:
        for clan in self._state["clans"].values():
            clan_roles = set(clan.get("roleIds", {}).values())
            if clan_roles & role_ids:
                return clan
        return None

    def find_clan_by_leader(self, user_id: int) -> dict | None:
        for clan in self._state["clans"].values():
            if clan.get("leaderId") == user_id:
                return clan
        return None

    async def update(self, mutator):
        async with self._lock:
            mutator(self._state)
            self._state["updatedAt"] = iso_now()
            await self._write_atomic()
        return self._state

    async def save(self):
        async with self._lock:
            await self._write_atomic()

    async def replace(self, next_state):
        validated = validate_state(next_state)
        async with self._lock:
            self._state = validated
            self._state["updatedAt"] = iso_now()
            await self._write_atomic()
        return self._state

    async def _write_atomic(self):
        temp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(STATE_FILE)


class BackupManager:
    def __init__(self, store: ClanStore):
        self.store = store
        self._timer: asyncio.Task | None = None
        self._running = False

    @property
    def enabled(self):
        return bool(CLAN_BACKUP_SECRET) and len(CLAN_BACKUP_SECRET) >= 32

    def _derive_key(self, secret: str, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
        return kdf.derive(secret.encode("utf-8"))

    def _encrypt(self, plain: bytes) -> dict:
        salt = secrets.token_bytes(16)
        iv = secrets.token_bytes(12)
        key = self._derive_key(CLAN_BACKUP_SECRET, salt)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(iv, plain, None)
        return {
            "format": BACKUP_FORMAT,
            "version": BACKUP_FORMAT_VERSION,
            "cipher": "aes-256-gcm",
            "kdf": "scrypt",
            "salt": base64.b64encode(salt).decode(),
            "iv": base64.b64encode(iv).decode(),
            "data": base64.b64encode(ciphertext).decode(),
            "checksum": hashlib.sha256(plain).hexdigest(),
        }

    def _decrypt(self, envelope: dict) -> bytes:
        salt = base64.b64decode(envelope["salt"])
        iv = base64.b64decode(envelope["iv"])
        tag_ciphertext = base64.b64decode(envelope["data"])
        key = self._derive_key(CLAN_BACKUP_SECRET, salt)
        aesgcm = AESGCM(key)
        plain = aesgcm.decrypt(iv, tag_ciphertext, None)
        checksum = hashlib.sha256(plain).hexdigest()
        if checksum != envelope.get("checksum"):
            raise ValueError("Backup checksum mismatch")
        return plain

    async def _resolve_channel(self, guild: discord.Guild):
        candidates = [CLAN_BACKUP_CHANNEL_ID, self.store.state["setup"].get("backupChannelId", 0)]
        for cid in candidates:
            if not cid:
                continue
            try:
                ch = await guild.fetch_channel(cid)
            except Exception:
                continue
            if self._is_safe_backup_channel(ch, guild):
                return ch
        for ch in guild.text_channels:
            if "резервные-копии" in ch.name and self._is_safe_backup_channel(ch, guild):
                return ch
        return None

    @staticmethod
    def _is_safe_backup_channel(channel, guild):
        if not isinstance(channel, discord.TextChannel) or channel.is_thread():
            return False
        everyone = channel.permissions_for(guild.default_role)
        bot = channel.permissions_for(guild.me)
        return (not everyone.view_channel) and bot.view_channel and bot.send_messages and bot.attach_files and bot.read_message_history

    async def create(self, guild: discord.Guild, reason: str = "manual"):
        if not self.enabled:
            raise RuntimeError("CLAN_BACKUP_SECRET is not configured")
        if self._running:
            raise RuntimeError("Backup operation is already running")
        self._running = True
        try:
            created_at = iso_now()
            state = self.store.snapshot()
            import gzip
            plain = json.dumps({
                "format": BACKUP_FORMAT,
                "version": BACKUP_FORMAT_VERSION,
                "createdAt": created_at,
                "reason": reason,
                "guild": {"id": guild.id, "name": guild.name},
                "state": state,
            }, ensure_ascii=False).encode("utf-8")
            compressed = gzip.compress(plain, 9)
            envelope = self._encrypt(compressed)
            envelope["format"] = BACKUP_FORMAT
            envelope["version"] = BACKUP_FORMAT_VERSION
            envelope["cipher"] = "aes-256-gcm"
            envelope["kdf"] = "scrypt"
            envelope["createdAt"] = created_at
            envelope["guildId"] = guild.id
            contents = json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(contents) > MAX_BACKUP_BYTES:
                raise RuntimeError("Encrypted backup exceeds the 8 MB safety limit")
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"wardogs-clans-{safe_timestamp()}.wdbak"
            (BACKUP_DIR / filename).write_bytes(contents)
            channel = await self._resolve_channel(guild)
            if not channel:
                raise RuntimeError("Private backup channel is missing; run /clan-setup")
            import gzip as _gz
            plain_check = _gz.decompress(self._decrypt(envelope))
            payload = json.loads(plain_check)
            await channel.send(
                content=(
                    "🔐 **Зашифрованная резервная копия клановой системы**\n"
                    f"Причина: `{reason}` · кланов: **{len(state['clans'])}** · "
                    f"заявок на клан: **{len(state['applications'])}** · "
                    f"заявок на вступление: **{len(state.get('joinApplications', {}))}**\n"
                    f"Создано: <t:{int(time.time())}:F> · формат: `{BACKUP_FORMAT}/{BACKUP_FORMAT_VERSION}`"
                ),
                file=discord.File(io.BytesIO(contents), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.store.update(lambda s: s.__setitem__("backup", {
                "lastAt": created_at, "lastFilename": filename, "lastError": None,
            }))
            await self._prune_local()
            log.info("Backup created: %s", filename)
            return {"filename": filename, "createdAt": created_at, "clanCount": len(state["clans"])}
        except Exception as e:
            await self.store.update(lambda s: s.get("backup", {}).update({"lastError": str(e), "lastAttemptAt": iso_now()}))
            raise
        finally:
            self._running = False

    async def load_latest(self, guild: discord.Guild):
        if not self.enabled:
            raise RuntimeError("CLAN_BACKUP_SECRET is not configured")
        channel = await self._resolve_channel(guild)
        if not channel:
            raise RuntimeError("Private backup channel was not found")
        async for message in channel.history(limit=100):
            if message.author != guild.me:
                continue
            for attachment in message.attachments:
                if attachment.filename.endswith(".wdbak"):
                    if attachment.size > MAX_BACKUP_BYTES:
                        raise RuntimeError("Backup attachment is too large")
                    data = await attachment.read()
                    envelope = json.loads(data)
                    import gzip
                    plain = gzip.decompress(self._decrypt(envelope))
                    payload = json.loads(plain)
                    return {"payload": payload, "messageId": message.id, "filename": attachment.filename}
        raise RuntimeError("No backup attachment found in the private channel")

    def start(self, guild: discord.Guild):
        self.stop()
        if not self.enabled:
            log.error("Automatic backups disabled: CLAN_BACKUP_SECRET is not configured")
            return
        self._timer = asyncio.create_task(self._loop(guild))

    def stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def _loop(self, guild: discord.Guild):
        interval = CLAN_BACKUP_INTERVAL_HOURS * 3600
        while True:
            await asyncio.sleep(interval)
            state = self.store.state
            if not state.get("setup"):
                log.error("Automatic backup skipped: clan setup state is empty")
                continue
            last_at = state.get("backup", {}).get("lastAt")
            if last_at:
                try:
                    last_ts = datetime.fromisoformat(last_at).timestamp()
                    if time.time() - last_ts < interval * 0.9:
                        continue
                except Exception:
                    pass
            try:
                await self.create(guild, "automatic")
            except Exception as e:
                log.error("Automatic backup failed: %s", e)

    async def _prune_local(self):
        if not BACKUP_DIR.exists():
            return
        files = sorted(
            [f.name for f in BACKUP_DIR.iterdir() if f.name.startswith("wardogs-clans-") and f.name.endswith(".wdbak")],
            reverse=True,
        )
        for name in files[CLAN_BACKUP_RETENTION:]:
            try:
                (BACKUP_DIR / name).unlink()
            except Exception:
                pass


async def inspect_discord_image(session: aiohttp.ClientSession, url: str, max_bytes: int, label: str) -> dict:
    url = clean(url, 500)
    if not IMAGE_URL.match(url):
        raise ValueError(f"{label} должен быть прямой ссылкой на изображение, загруженное в Discord.")
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as resp:
        if resp.status != 200:
            raise ValueError(f"Discord не отдал изображение ({resp.status}). Скопируйте ссылку заново.")
        clen = resp.headers.get("content-length", "0")
        try:
            declared = int(clen)
        except ValueError:
            declared = 0
        if declared > max_bytes:
            raise ValueError(f"Размер файла «{label}» превышает {max_bytes // 1024} КБ.")
        data = await resp.read()
    if not data or len(data) > max_bytes:
        raise ValueError(f"Размер файла «{label}» должен быть от 1 байта до {max_bytes // 1024} КБ.")
    mime = detect_image_mime(data)
    if mime not in CLAN_LOGO_MIMES:
        raise ValueError("Разрешены только PNG, JPG/JPEG и WebP.")
    return {"url": url, "hash": hashlib.sha256(data).hexdigest(), "mime": mime, "size": len(data), "bytes": data}


async def sync_site(clans: list[dict]):
    if not CLAN_SYNC_URL or not CLAN_SYNC_SECRET:
        return {"skipped": True}
    def public_clan(c):
        return {
            "id": c.get("id"),
            "name": c.get("name"),
            "tag": c.get("tag"),
            "leader_name": c.get("leaderName"),
            "declared_members": c.get("declaredMembers"),
            "server_members": c.get("serverMembers", 0),
            "discord_url": c.get("discordUrl"),
            "logo_source_url": c.get("logoUrl") or None,
            "logo_sha256": c.get("logoHash") or None,
            "logo_mime": c.get("logoMime") or None,
            "description": c.get("description"),
            "goals": c.get("goals"),
            "recruitment_status": "open" if c.get("recruitmentStatus") == "open" else "closed",
            "recruitment_url": c.get("recruitmentUrl") or None,
            "created_at": c.get("createdAt"),
            "updated_at": c.get("updatedAt"),
        }
    body = json.dumps({
        "version": 1,
        "generated_at": iso_now(),
        "source": {"guild_id": str(GUILD_ID)},
        "clans": [public_clan(c) for c in clans[:100]],
    }, ensure_ascii=False)
    ts = str(int(time.time()))
    sig = hmac.new(CLAN_SYNC_SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            CLAN_SYNC_URL,
            data=body.encode("utf-8"),
            headers={
                "content-type": "application/json",
                "user-agent": "WARDOGS-RU-Clans-Bot/1.0",
                "x-wardogs-timestamp": ts,
                "x-wardogs-signature": sig,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Site sync failed ({resp.status}): {text[:200]}")
            return {"skipped": False, "status": resp.status}
