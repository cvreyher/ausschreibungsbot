"""Telegram-Oberfläche (aiogram) – verbindet Chat-Nachrichten mit dem Delegate-Agent.

Threads:
- "chat:<chat_id>:<n>"  allgemeines Gespräch (/neu startet ein frisches)
- "tender:<id>"         Bewerbungsablauf für eine Ausschreibung

Fragt der Delegate etwas (LangGraph-Interrupt), wird die Frage gesendet und als "pending"
gespeichert. Die Antwort des Nutzers setzt den Graph mit Command(resume=...) fort.
"""

import asyncio
import html
import logging
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from langgraph.types import Command as Resume

from .agents.common import tender_brief
from .pipeline import scout_und_bewerten
from .profile import load_profile
from .services import Services

log = logging.getLogger(__name__)

TG_LIMIT = 4000

BOT_COMMANDS = [
    BotCommand(command="suchen", description="Jetzt nach neuen Ausschreibungen suchen"),
    BotCommand(command="liste", description="Bekannte Ausschreibungen anzeigen"),
    BotCommand(command="profil", description="Firmenprofil anzeigen"),
    BotCommand(command="neu", description="Neues Gespräch beginnen"),
    BotCommand(command="hilfe", description="Hilfe"),
]

HELP = (
    "Ich bin der Ausschreibungs-Bot von Decocity.\n\n"
    "• Ich durchsuche regelmäßig service.bund.de und melde passende Ausschreibungen.\n"
    "• Mit „📝 Bewerbung vorbereiten“ recherchiere ich die Unterlagen, stelle dir Rückfragen "
    "und schreibe einen Angebotsentwurf.\n"
    "• Abgegeben wird erst nach deiner Freigabe per Button.\n\n"
    "Du kannst mir auch einfach schreiben, z.B. „Such mal nach Markisen in Potsdam“.\n\n"
    "/suchen /liste /profil /neu"
)


ADMIN_HELP = (
    "Admin-Befehle:\n"
    "/nutzer – alle Nutzer und offenen Anfragen\n"
    "/freigeben <ID> – Nutzer freischalten\n"
    "/admin <ID> – Nutzer zum Admin machen\n"
    "/entziehen <ID> – Zugriff entziehen\n\n"
    "Neue Personen schicken dem Bot einfach /start – du bekommst dann eine Anfrage mit Buttons."
)


def chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    text = text or "(leer)"
    return [text[i : i + size] for i in range(0, len(text), size)]


class TelegramUI:
    def __init__(self, services: Services, delegate, bot: Bot):
        self.s = services.settings
        self.db = services.db
        self.services = services
        self.delegate = delegate
        self.bot = bot
        self.router = Router()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()
        self._register()

    # ------------------------------------------------------------------ Zugriff

    async def init_users(self) -> None:
        """Übernimmt Admins aus TELEGRAM_ALLOWED_CHAT_IDS und den alten Besitzer-Eintrag."""
        admins = set(self.s.allowed_chat_ids)
        if owner := await self.db.kv_get("owner_chat_id"):
            admins.add(int(owner))
        for chat_id in admins:
            await self.db.upsert_user(chat_id, None, None, status="approved", role="admin")

    async def recipients(self) -> set[int]:
        return {u["chat_id"] for u in await self.db.list_users(status="approved")}

    async def admins(self) -> set[int]:
        return {u["chat_id"] for u in await self.db.list_users(status="approved", role="admin")}

    async def allowed(self, chat_id: int) -> bool:
        u = await self.db.get_user(chat_id)
        return bool(u and u["status"] == "approved")

    async def is_admin(self, chat_id: int) -> bool:
        u = await self.db.get_user(chat_id)
        return bool(u and u["status"] == "approved" and u["role"] == "admin")

    @staticmethod
    def user_label(u: dict) -> str:
        name = u.get("name") or "?"
        handle = f" (@{u['username']})" if u.get("username") else ""
        return f"{name}{handle} – ID {u['chat_id']}"

    async def request_access(self, m: Message) -> None:
        user = await self.db.upsert_user(m.chat.id, m.from_user.full_name, m.from_user.username)
        if user["status"] == "blocked":
            await m.answer("⛔ Kein Zugriff.")
            return
        await m.answer("🔐 Zugriff angefragt. Ein Admin von Decocity muss dich freischalten.")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Freigeben", callback_data=f"usr:ok:{m.chat.id}"),
                    InlineKeyboardButton(text="❌ Ablehnen", callback_data=f"usr:no:{m.chat.id}"),
                ]
            ]
        )
        for admin in await self.admins():
            await self.bot.send_message(admin, f"🙋 Zugriffsanfrage\n{self.user_label(user)}", reply_markup=kb)

    async def approve(self, chat_id: int, by: int, role: str = "user") -> dict:
        user = await self.db.set_user(chat_id, status="approved", role=role, approved_by=by)
        try:
            await self.bot.send_message(chat_id, "✅ Du wurdest freigeschaltet.\n\n" + HELP)
        except Exception as e:
            log.warning("Konnte %s nicht benachrichtigen: %s", chat_id, e)
        return user

    # ------------------------------------------------------------------ Senden

    async def send(self, chat_id: int, text: str, **kw) -> Message:
        msg = None
        for part in chunks(text):
            msg = await self.bot.send_message(chat_id, part, **kw)
        return msg

    async def notify_tenders(self, tenders: list[dict]) -> None:
        for chat_id in await self.recipients():
            for t in tenders:
                await self.send_card(chat_id, t)

    async def send_card(self, chat_id: int, t: dict) -> None:
        e = html.escape
        text = (
            f"🔔 <b>{e(t['title'])}</b>\n\n"
            f"🏛 {e(t.get('authority') or '-')}\n"
            f"📍 {e(t.get('place') or '-')}\n"
            f"⏰ Frist: {e(t.get('deadline') or '-')}\n"
            f"🎯 Passung: {t.get('score') if t.get('score') is not None else '?'}/100\n\n"
            f"{e((t.get('summary') or '')[:1500])}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📝 Bewerbung vorbereiten", callback_data=f"bid:{t['id']}")],
                [
                    InlineKeyboardButton(text="🔎 Details", callback_data=f"det:{t['id']}"),
                    InlineKeyboardButton(text="🙈 Ignorieren", callback_data=f"ign:{t['id']}"),
                ],
                [InlineKeyboardButton(text="🌐 Öffnen", url=t.get("notice_url") or t["url"])],
            ]
        )
        await self.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # ------------------------------------------------------------------ Agent-Läufe

    def start_run(self, chat_id: int, thread_id: str, payload: str | Resume) -> None:
        task = asyncio.create_task(self._run(chat_id, thread_id, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _typing(self, chat_id: int) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4.5)

    async def _run(self, chat_id: int, thread_id: str, payload: str | Resume) -> None:
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 150}
        async with self._locks[thread_id]:
            # Hängt der Thread noch an einer Rückfrage, wird neuer Text als Antwort gewertet.
            if isinstance(payload, str):
                snap = await self.delegate.aget_state(config)
                if snap.interrupts:
                    intr = snap.interrupts[0]
                    value = payload if intr.value.get("art") == "frage" else f"aenderung: {payload}"
                    payload = Resume(resume={intr.id: value})

            await self.db.clear_pending(thread_id)
            typing = asyncio.create_task(self._typing(chat_id))
            try:
                inp = payload if isinstance(payload, Resume) else {"messages": [{"role": "user", "content": payload}]}
                await self.delegate.ainvoke(inp, config)
            except Exception as e:
                log.exception("Agent-Lauf %s fehlgeschlagen", thread_id)
                await self.send(chat_id, f"⚠️ Fehler im Agent ({thread_id}): {e}")
                return
            finally:
                typing.cancel()

            snap = await self.delegate.aget_state(config)
            if snap.interrupts:
                for intr in snap.interrupts:
                    await self._ask(chat_id, thread_id, intr)
                return
            last = snap.values["messages"][-1]
            await self.send(chat_id, last.text or "✅ Erledigt.")

    async def _ask(self, chat_id: int, thread_id: str, intr) -> None:
        v = intr.value
        tender_id = v.get("tender_id")
        header = ""
        if tender_id and (t := await self.db.get_tender(tender_id)):
            header = f"📌 #{tender_id} {t['title']}\n\n"

        if v["art"] == "frage":
            msg = await self.send(chat_id, f"❓ {header}{v['frage']}\n\n↩️ Antworte einfach auf diese Nachricht.")
            await self.db.add_pending(chat_id, thread_id, intr.id, "frage", tender_id, msg.message_id)
            return

        # Freigabe: Entwurf als Datei + Buttons. Freigeben dürfen nur Admins – arbeitet ein
        # normaler Nutzer am Angebot, geht die Anfrage zusätzlich an alle Admins.
        entwurf = self.s.bids_dir / str(tender_id) / "angebot_entwurf.md"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Freigeben", callback_data=f"fg:ok|{thread_id}"),
                    InlineKeyboardButton(text="✏️ Ändern", callback_data=f"fg:edit|{thread_id}"),
                    InlineKeyboardButton(text="❌ Verwerfen", callback_data=f"fg:no|{thread_id}"),
                ]
            ]
        )
        targets = [chat_id] + sorted((await self.admins()) - {chat_id})
        for target in targets:
            if entwurf.exists():
                await self.bot.send_document(target, FSInputFile(entwurf, filename=f"angebot_{tender_id}.md"))
            note = "" if await self.is_admin(target) else "\n\n(Freigeben kann nur ein Admin – die Anfrage ging auch an die Admins.)"
            msg = await self.bot.send_message(
                target, f"🧾 Freigabe nötig\n{header}{v['zusammenfassung']}"[: TG_LIMIT - 120] + note, reply_markup=kb
            )
            await self.db.add_pending(target, thread_id, intr.id, "freigabe", tender_id, msg.message_id)

    async def chat_thread(self, chat_id: int) -> str:
        n = await self.db.kv_get(f"chat_thread:{chat_id}") or "0"
        return f"chat:{chat_id}:{n}"

    # ------------------------------------------------------------------ Handler

    def _register(self) -> None:
        r = self.router

        @r.message(CommandStart())
        async def start(m: Message):
            if not await self.admins():
                await self.db.upsert_user(
                    m.chat.id, m.from_user.full_name, m.from_user.username, status="approved", role="admin"
                )
                await m.answer(f"👋 Du bist jetzt Admin dieses Bots (Chat-ID {m.chat.id}).")
            if not await self.allowed(m.chat.id):
                await self.request_access(m)
                return
            await m.answer(HELP + ("\n\n" + ADMIN_HELP if await self.is_admin(m.chat.id) else ""))

        # --- Nutzerverwaltung (nur Admins) ---

        async def _target(m: Message, command: CommandObject) -> int | None:
            if not await self.is_admin(m.chat.id):
                return None
            if not command.args or not command.args.strip().lstrip("-").isdigit():
                await m.answer(f"Nutzung: /{command.command} <Chat-ID>  (IDs siehe /nutzer)")
                return None
            return int(command.args.strip())

        @r.message(Command("nutzer"))
        async def nutzer(m: Message):
            if not await self.is_admin(m.chat.id):
                return
            users = await self.db.list_users()
            icons = {"approved": "✅", "pending": "⏳", "blocked": "⛔"}
            lines = [
                f"{icons.get(u['status'], '?')} {'👑 ' if u['role'] == 'admin' else ''}{self.user_label(u)}"
                for u in users
            ]
            await self.send(m.chat.id, "Nutzer:\n" + ("\n".join(lines) or "(keine)") + "\n\n" + ADMIN_HELP)

        @r.message(Command("freigeben"))
        async def freigeben(m: Message, command: CommandObject):
            if (cid := await _target(m, command)) is not None:
                u = await self.approve(cid, by=m.chat.id)
                await m.answer(f"✅ Freigeschaltet: {self.user_label(u)}")

        @r.message(Command("admin"))
        async def admin(m: Message, command: CommandObject):
            if (cid := await _target(m, command)) is not None:
                u = await self.approve(cid, by=m.chat.id, role="admin")
                await m.answer(f"👑 Admin: {self.user_label(u)}")

        @r.message(Command("entziehen"))
        async def entziehen(m: Message, command: CommandObject):
            if (cid := await _target(m, command)) is None:
                return
            if cid == m.chat.id:
                await m.answer("Du kannst dir nicht selbst den Zugriff entziehen.")
                return
            u = await self.db.set_user(cid, status="blocked", role="user")
            await self.db.clear_all_pending(cid)
            await m.answer(f"⛔ Zugriff entzogen: {self.user_label(u)}")

        @r.callback_query(F.data.startswith("usr:"))
        async def user_action(c: CallbackQuery):
            if not await self.is_admin(c.message.chat.id):
                await c.answer("Nur Admins.")
                return
            _, action, cid = c.data.split(":")
            if action == "ok":
                u = await self.approve(int(cid), by=c.message.chat.id)
                text = f"✅ Freigeschaltet: {self.user_label(u)}"
            else:
                u = await self.db.set_user(int(cid), status="blocked")
                text = f"❌ Abgelehnt: {self.user_label(u)}"
            await c.answer()
            await c.message.edit_text(text)

        @r.message(Command("hilfe"))
        async def hilfe(m: Message):
            await m.answer(HELP)

        @r.message(Command("suchen"))
        async def suchen(m: Message):
            if not await self.allowed(m.chat.id):
                return
            await m.answer("🔍 Suche läuft …")
            new = await scout_und_bewerten(self.services)
            relevant = sum(t["status"] == "gemeldet" for t in new)
            await m.answer(f"Fertig: {len(new)} neue Ausschreibungen, davon {relevant} relevant.")

        @r.message(Command("liste"))
        async def liste(m: Message):
            if not await self.allowed(m.chat.id):
                return
            rows = await self.db.list_tenders()
            if not rows:
                await m.answer("Noch keine relevanten Ausschreibungen bekannt. /suchen")
                return
            lines = [f"#{t['id']} [{t['status']}] {t['title']} – Frist {t['deadline']}" for t in rows]
            await self.send(m.chat.id, "\n".join(lines))

        @r.message(Command("profil"))
        async def profil(m: Message):
            if await self.allowed(m.chat.id):
                await self.send(m.chat.id, load_profile(self.s))

        @r.message(Command("neu"))
        async def neu(m: Message):
            if not await self.allowed(m.chat.id):
                return
            n = int(await self.db.kv_get(f"chat_thread:{m.chat.id}") or "0") + 1
            await self.db.kv_set(f"chat_thread:{m.chat.id}", str(n))
            await m.answer("🧹 Neues Gespräch gestartet.")

        @r.message(F.text)
        async def text(m: Message):
            if not await self.allowed(m.chat.id):
                await m.answer("🔐 Du bist noch nicht freigeschaltet. Schick /start, um Zugriff anzufragen.")
                return
            pending = None
            if m.reply_to_message:
                pending = await self.db.pending_by_message(m.chat.id, m.reply_to_message.message_id)
            pending = pending or await self.db.latest_pending(m.chat.id, ("frage", "aenderung"))
            if pending:
                value = m.text if pending["kind"] == "frage" else f"aenderung: {m.text}"
                self.start_run(m.chat.id, pending["thread_id"], Resume(resume={pending["interrupt_id"]: value}))
                return
            self.start_run(m.chat.id, await self.chat_thread(m.chat.id), m.text)

        @r.callback_query(F.data.startswith(("bid:", "det:", "ign:")))
        async def tender_action(c: CallbackQuery):
            if not await self.allowed(c.message.chat.id):
                return
            action, tid = c.data.split(":")
            t = await self.db.get_tender(int(tid))
            if not t:
                await c.answer("Unbekannt")
                return
            if action == "det":
                await c.answer()
                await self.send(c.message.chat.id, tender_brief(t))
            elif action == "ign":
                await self.db.update_tender(t["id"], status="ignoriert")
                await c.answer("Ignoriert")
                await c.message.edit_reply_markup(reply_markup=None)
            else:
                await c.answer("Los geht's")
                await c.message.edit_reply_markup(reply_markup=None)
                await self.db.update_tender(t["id"], status="in_bearbeitung")
                await self.send(c.message.chat.id, f"📝 Ich bereite die Bewerbung für #{t['id']} vor. Das dauert ein paar Minuten …")
                self.start_run(
                    c.message.chat.id,
                    f"tender:{t['id']}",
                    f"Bereite die Bewerbung für Ausschreibung #{t['id']} vor.\n\n{tender_brief(t)}",
                )

        @r.callback_query(F.data.startswith("fg:"))
        async def freigabe(c: CallbackQuery):
            chat_id = c.message.chat.id
            if not await self.allowed(chat_id):
                return
            action, thread_id = c.data[3:].split("|", 1)
            pending = await self.db.pending_by_message(chat_id, c.message.message_id)
            if not pending or pending["thread_id"] != thread_id:
                await c.answer("Diese Freigabe ist nicht mehr aktuell.")
                return
            if action == "ok" and not await self.is_admin(chat_id):
                await c.answer("Nur Admins dürfen Angebote freigeben.", show_alert=True)
                return
            await c.message.edit_reply_markup(reply_markup=None)
            tid = pending["tender_id"]
            if action == "edit":
                await self.db.set_pending_kind(pending["id"], "aenderung")
                await c.answer()
                await c.message.reply("✏️ Was soll geändert werden? Antworte auf diese Nachricht.")
                return
            # Die Freigabe wird hier im Code gesetzt – nur so darf abgabe_durchfuehren laufen.
            if tid:
                await self.db.update_tender(tid, status="freigegeben" if action == "ok" else "verworfen")
            await c.answer("Freigegeben" if action == "ok" else "Verworfen")
            value = "freigegeben" if action == "ok" else "verworfen"
            # Weiter im Chat, in dem die Bewerbung läuft – auch wenn ein Admin aus seinem Chat freigibt.
            home = await self.db.home_chat(thread_id) or chat_id
            if home != chat_id:
                who = c.from_user.full_name
                await self.bot.send_message(home, f"{'✅ Freigegeben' if action == 'ok' else '❌ Verworfen'} von {who}.")
            self.start_run(home, thread_id, Resume(resume={pending["interrupt_id"]: value}))
