import asyncio, base64, html, io, logging, os, sqlite3, zipfile
from pathlib import PurePosixPath
from contextlib import closing
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
KEY = os.getenv("ENCRYPTION_KEY", "")
DB_PATH = os.getenv("DB_PATH", "./data/xcore_git.sqlite3")
MAX_ZIP_FILES = int(os.getenv("MAX_ZIP_FILES", "1000"))
MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", "52428800"))
MAX_TELEGRAM_FILE_BYTES = int(os.getenv("MAX_TELEGRAM_FILE_BYTES", "20971520"))
MAX_SINGLE_FILE_BYTES = int(os.getenv("MAX_SINGLE_FILE_BYTES", "5242880"))
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "").strip()
if not BOT_TOKEN or not KEY:
    raise RuntimeError("Set BOT_TOKEN and ENCRYPTION_KEY in .env")
fernet = Fernet(KEY.encode())
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
with closing(sqlite3.connect(DB_PATH)) as con:
    con.execute("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, login TEXT, email TEXT, token BLOB)")
    con.commit()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xcore_git")
API = "https://api.github.com"
HEADER = "◈──── 𝚇𝙲𝙾𝚁𝙴® ɢɪᴛ ────◈"
FOOTER = "\n\n╰─ 𝙳𝙴𝚅 𝙰𝚄𝚂𝚃𝙸𝙽"

class Flow(StatesGroup):
    token = State()
    repo_name = State()
    repo_desc = State()
    repo_visibility = State()
    repo_readme = State()
    repo_gitignore = State()
    zip_upload = State()
    single_upload = State()
    edit_desc = State()

# One active GitHub Device Flow per Telegram user.
oauth_tasks = {}

def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])

def main_kb():
    return keyboard([
        [("📁 MY REPOSITORIES", "repos")],
        [("➕ CREATE REPOSITORY", "create"), ("📤 UPLOAD PROJECT", "upload")],
        [("📥 DOWNLOAD REPOSITORY", "download")],
        [("👤 MY ACCOUNT", "account"), ("⚙️ SETTINGS", "settings")]
    ])

def db_get(uid):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        return con.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()

def db_save(uid, login, email, token):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("""INSERT INTO users VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET login=excluded.login,email=excluded.email,token=excluded.token""",
        (uid, login, email, fernet.encrypt(token.encode())))
        con.commit()

def db_delete(uid):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
        con.commit()

def token_for(uid):
    row = db_get(uid)
    if not row: return None
    try: return fernet.decrypt(row["token"]).decode()
    except InvalidToken:
        log.error("Credential decryption failed for Telegram user %s", uid)
        return None

class GH:
    def __init__(self, token):
        self.c = httpx.AsyncClient(base_url=API, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "XCORE-GIT"
        }, timeout=60)
    async def close(self): await self.c.aclose()
    async def req(self, method, path, **kwargs):
        r = await self.c.request(method, path, **kwargs)
        if r.status_code >= 400:
            try: detail = r.json().get("message", r.text)
            except Exception: detail = r.text
            raise RuntimeError(f"GitHub {r.status_code}: {detail}")
        return None if r.status_code == 204 or not r.content else r.json()
    async def me(self): return await self.req("GET", "/user")
    async def email(self):
        try:
            e = await self.req("GET", "/user/emails")
            return next((x["email"] for x in e if x.get("primary")), None)
        except Exception: return None
    async def repos(self):
        return await self.req("GET", "/user/repos", params={"visibility":"all","affiliation":"owner,collaborator,organization_member","sort":"updated","per_page":100})
    async def repo(self, owner, name): return await self.req("GET", f"/repos/{owner}/{name}")
    async def create(self, name, desc, private, readme, gitignore):
        p = {"name":name,"description":desc,"private":private,"auto_init":readme}
        if gitignore: p["gitignore_template"] = gitignore
        return await self.req("POST", "/user/repos", json=p)
    async def update(self, owner, name, **p): return await self.req("PATCH", f"/repos/{owner}/{name}", json=p)
    async def delete_repo(self, owner, name): return await self.req("DELETE", f"/repos/{owner}/{name}")
    async def upload_tree(self, owner, name, branch, files, message):
        b = await self.req("GET", f"/repos/{owner}/{name}/branches/{branch}")
        parent = b["commit"]["sha"]
        base_tree = b["commit"]["commit"]["tree"]["sha"]
        entries = []
        for path, data in files.items():
            blob = await self.req("POST", f"/repos/{owner}/{name}/git/blobs",
                json={"content":base64.b64encode(data).decode(),"encoding":"base64"})
            entries.append({"path":path,"mode":"100644","type":"blob","sha":blob["sha"]})
        tree = await self.req("POST", f"/repos/{owner}/{name}/git/trees",
                              json={"base_tree":base_tree,"tree":entries})
        commit = await self.req("POST", f"/repos/{owner}/{name}/git/commits",
                                json={"message":message,"tree":tree["sha"],"parents":[parent]})
        await self.req("PATCH", f"/repos/{owner}/{name}/git/refs/heads/{branch}", json={"sha":commit["sha"]})
        return commit
    async def put_file(self, owner, name, path, data, branch):
        old = None
        try: old = await self.req("GET", f"/repos/{owner}/{name}/contents/{path}", params={"ref":branch})
        except RuntimeError as e:
            if "404" not in str(e): raise
        p = {"message":f"Upload {path} via XCORE GIT","content":base64.b64encode(data).decode(),"branch":branch}
        if old: p["sha"] = old["sha"]
        return await self.req("PUT", f"/repos/{owner}/{name}/contents/{path}", json=p)
    async def tree(self, owner, name, branch):
        b = await self.req("GET", f"/repos/{owner}/{name}/branches/{branch}")
        sha = b["commit"]["commit"]["tree"]["sha"]
        return await self.req("GET", f"/repos/{owner}/{name}/git/trees/{sha}", params={"recursive":"1"})
    async def download(self, owner, name, branch):
        r = await self.c.get(f"/repos/{owner}/{name}/zipball/{branch}", follow_redirects=True)
        if r.status_code >= 400: raise RuntimeError("GitHub could not create the archive.")
        return r.content
    async def delete_file(self, owner, name, path, sha, branch):
        return await self.req("DELETE", f"/repos/{owner}/{name}/contents/{path}",
            json={"message":f"Delete {path} via XCORE GIT","sha":sha,"branch":branch})

def github(uid):
    t = token_for(uid)
    return GH(t) if t else None

SECRET = {".env", ".env.local", ".env.production", ".env.development", "id_rsa", "id_ed25519", "credentials.json", "service-account.json"}
def parse_zip(raw):
    try: z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile: raise ValueError("This is not a valid ZIP file.")
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_FILES: raise ValueError(f"Too many files. Maximum: {MAX_ZIP_FILES}.")
    if sum(i.file_size for i in infos) > MAX_ZIP_BYTES: raise ValueError("The extracted project exceeds the configured size limit.")
    out = {}
    for i in infos:
        p = PurePosixPath(i.filename.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0].endswith(":"):
            raise ValueError("Unsafe file path found in ZIP. Upload cancelled.")
        name = p.name.lower()
        if name in SECRET or name.endswith((".pem",".key",".p12",".pfx")): continue
        if name.startswith(".env.") and name not in (".env.example",".env.sample",".env.template"): continue
        out[str(p)] = z.read(i)
    # Remove a single wrapper directory if every file is nested inside it.
    roots = {PurePosixPath(p).parts[0] for p in out}
    if len(roots) == 1:
        root = next(iter(roots))
        if all(len(PurePosixPath(p).parts) > 1 for p in out):
            out = {str(PurePosixPath(p).relative_to(root)): v for p,v in out.items()}
    if not out: raise ValueError("No safe project files found. Secret files are excluded.")
    return out

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

async def edit(msg, text, markup=None):
    try: await msg.edit_text(text, reply_markup=markup)
    except Exception: await msg.answer(text, reply_markup=markup)

async def need_account(c):
    g = github(c.from_user.id)
    if not g:
        await c.answer("Connect GitHub first.", show_alert=True)
        await edit(c.message, f"{HEADER}\n\nYour GitHub account is not connected.{FOOTER}",
                   keyboard([[("🔐 CONNECT GITHUB","connect")], [("🏠 MAIN MENU","home")]]))
    return g

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    row = db_get(m.from_user.id)
    status = f"GitHub: <b>@{html.escape(row['login'])}</b>" if row else "GitHub: <b>Not connected</b>"
    await m.answer(f"{HEADER}\n\nWelcome to XCORE GIT.\n{status}\n\nYour GitHub. Your Files. Zero Stress.{FOOTER}",
                   main_kb() if row else keyboard([[("🔐 CONNECT GITHUB","connect")]]))

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    row = db_get(c.from_user.id)
    status = f"GitHub: <b>@{html.escape(row['login'])}</b>" if row else "GitHub: <b>Not connected</b>"
    await edit(c.message, f"{HEADER}\n\n{status}\n\nYour GitHub. Your Files. Zero Stress.{FOOTER}", main_kb())
    await c.answer()

@dp.callback_query(F.data == "connect")
async def connect(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c.message, f"{HEADER}\n\n🔐 <b>CONNECT GITHUB</b>\nChoose your preferred connection method.\n\n🌐 <b>Login with GitHub</b> — approve XCORE GIT using a short verification code. No manual token copying.\n\n🔑 <b>Personal Access Token</b> — connect with a token you create in GitHub settings.{FOOTER}",
                keyboard([[ ("🌐 LOGIN WITH GITHUB","connect:oauth") ],[("🔑 CONNECT WITH PAT","connect:pat")],[("↩️ CANCEL","home")]]))
    await c.answer()

@dp.callback_query(F.data == "connect:pat")
async def connect_pat(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.token)
    await edit(c.message, f"{HEADER}\n\n🔑 <b>CONNECT WITH PERSONAL ACCESS TOKEN</b>\nSend your GitHub Personal Access Token here in this private chat. Use a fine-grained token with Metadata read, Contents read/write, and Administration permissions for repository management. The bot attempts to delete the token message immediately.{FOOTER}",
                keyboard([[ ("↩️ CANCEL","connect") ]]))
    await c.answer()

async def github_device_login(uid: int, chat_id: int):
    """Run GitHub OAuth Device Flow and save the resulting token encrypted."""
    task = asyncio.current_task()
    try:
        async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json", "User-Agent": "XCORE-GIT"}) as client:
            start = await client.post("https://github.com/login/device/code", data={
                "client_id": GITHUB_CLIENT_ID, "scope": "repo user:email delete_repo"
            })
            start.raise_for_status()
            payload = start.json()
            device_code = payload["device_code"]
            user_code = payload["user_code"]
            verify_url = payload.get("verification_uri", "https://github.com/login/device")
            expires_in = int(payload.get("expires_in", 900))
            interval = max(5, int(payload.get("interval", 5)))
            await bot.send_message(chat_id,
                f"{HEADER}\n\n🌐 <b>GITHUB LOGIN</b>\n\n1. Open: {html.escape(verify_url)}\n2. Enter this one-time code:\n\n<code>{html.escape(user_code)}</code>\n\n3. Sign in to GitHub and approve XCORE GIT.\n\nI will detect approval automatically. This code expires in about 15 minutes. Do not share it with anyone.{FOOTER}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 OPEN GITHUB", url=verify_url)],
                    [InlineKeyboardButton(text="❌ CANCEL LOGIN", callback_data="oauth:cancel")]
                ]))
            deadline = asyncio.get_running_loop().time() + expires_in
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(interval)
                response = await client.post("https://github.com/login/oauth/access_token", data={
                    "client_id": GITHUB_CLIENT_ID, "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                })
                response.raise_for_status()
                result = response.json()
                if result.get("access_token"):
                    token = result["access_token"]
                    g = GH(token)
                    try:
                        me = await g.me()
                        email = await g.email()
                        repos = await g.repos()
                        db_save(uid, me["login"], email, token)
                        pub = sum(not r["private"] for r in repos)
                        prv = sum(r["private"] for r in repos)
                    finally:
                        await g.close()
                    await bot.send_message(chat_id,
                        f"{HEADER}\n\n✅ <b>GITHUB CONNECTED</b>\nLogin method: GitHub OAuth\nUser: @{html.escape(me['login'])}\nEmail: {html.escape(email or 'Not available')}\nTotal repositories: {len(repos)}\nPublic: {pub}\nPrivate: {prv}\nStatus: Connected{FOOTER}", reply_markup=main_kb())
                    return
                error = result.get("error", "")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error in ("expired_token", "token_expired"):
                    await bot.send_message(chat_id, "GitHub login code expired. Please start again with Login with GitHub.")
                    return
                if error == "access_denied":
                    await bot.send_message(chat_id, "GitHub authorization was declined. You can try again or use a Personal Access Token.")
                    return
                if error:
                    await bot.send_message(chat_id, f"GitHub login could not continue: {html.escape(error)}. You can try again or use PAT login.")
                    return
            await bot.send_message(chat_id, "GitHub login timed out. Please start again to get a new code.")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("GitHub OAuth device login failed")
        await bot.send_message(chat_id, f"GitHub login failed: {html.escape(str(e)[:500])}")
    finally:
        if oauth_tasks.get(uid) is task:
            oauth_tasks.pop(uid, None)

@dp.callback_query(F.data == "connect:oauth")
async def connect_oauth(c: CallbackQuery):
    if not GITHUB_CLIENT_ID:
        await c.answer("GitHub OAuth is not configured yet. Add GITHUB_CLIENT_ID to deployment variables.", show_alert=True)
        return
    existing = oauth_tasks.get(c.from_user.id)
    if existing and not existing.done():
        await c.answer("A GitHub login is already running. Check your messages.", show_alert=True)
        return
    task = asyncio.create_task(github_device_login(c.from_user.id, c.message.chat.id))
    oauth_tasks[c.from_user.id] = task
    await edit(c.message, f"{HEADER}\n\n⏳ Preparing your secure GitHub login...\nI will send your verification code in a moment.{FOOTER}")
    await c.answer("Starting GitHub login...")

@dp.callback_query(F.data == "oauth:cancel")
async def oauth_cancel(c: CallbackQuery):
    task = oauth_tasks.get(c.from_user.id)
    if task and not task.done():
        task.cancel()
    await edit(c.message, f"GitHub login cancelled.{FOOTER}", keyboard([[ ("🔐 CONNECT GITHUB","connect") ],[("🏠 MAIN MENU","home")]]))
    await c.answer("Login cancelled.")

@dp.message(Flow.token)
async def token_received(m: Message, state: FSMContext):
    token = (m.text or "").strip()
    try: await m.delete()
    except Exception: pass
    if len(token) < 20 or len(token) > 500 or any(ch.isspace() for ch in token):
        await m.answer("That doesn't look like a valid GitHub token. Send a valid token or /cancel.")
        return
    g = GH(token)
    try:
        me = await g.me()
        email = await g.email()
        db_save(m.from_user.id, me["login"], email, token)
        repos = await g.repos()
        pub = sum(not r["private"] for r in repos)
        prv = sum(r["private"] for r in repos)
        await state.clear()
        await m.answer(f"{HEADER}\n\n✅ <b>GITHUB CONNECTED</b>\nUser: @{html.escape(me['login'])}\nEmail: {html.escape(email or 'Not available')}\nTotal repositories: {len(repos)}\nPublic: {pub}\nPrivate: {prv}\nStatus: Connected{FOOTER}", main_kb())
    except Exception as e:
        await m.answer(f"GitHub couldn't validate the token. Check the token and its permissions.\n{html.escape(str(e))}")
    finally: await g.close()

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.", main_kb())

@dp.callback_query(F.data == "account")
async def account(c: CallbackQuery):
    g = await need_account(c)
    if not g: return
    try:
        me = await g.me(); email = await g.email(); repos = await g.repos()
        pub = sum(not r["private"] for r in repos); prv = sum(r["private"] for r in repos)
        await edit(c.message, f"◈──── 𝚇𝙲𝙾𝚁𝙴® ɢɪᴛ • ᴀᴄᴄᴏᴜɴᴛ ────◈\n\n╭─ ᴀᴄᴄᴏᴜɴᴛ sᴛᴀᴛᴜs\n├─ ᴜsᴇʀ : @{html.escape(me['login'])}\n├─ ᴇᴍᴀɪʟ : {html.escape(email or '—')}\n├─ ᴛᴏᴛᴀʟ ʀᴇᴘᴏs : {len(repos)}\n├─ ᴘᴜʙʟɪᴄ : {pub}\n├─ ᴘʀɪᴠᴀᴛᴇ : {prv}\n╰─ sᴛᴀᴛᴜs : ✅ ᴄᴏɴɴᴇᴄᴛᴇᴅ{FOOTER}",
                   keyboard([[("🔄 REFRESH","account")], [("🔌 DISCONNECT","disconnect")], [("🏠 MAIN MENU","home")]]))
        await c.answer()
    except Exception as e: await c.answer(str(e)[:190], show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data == "disconnect")
async def disconnect(c: CallbackQuery):
    await edit(c.message, f"Disconnect GitHub and erase the saved encrypted token?{FOOTER}",
        keyboard([[("⚠️ YES, DISCONNECT","disconnect:yes")], [("↩️ CANCEL","account")]]))
    await c.answer()

@dp.callback_query(F.data == "disconnect:yes")
async def disconnect_yes(c: CallbackQuery):
    db_delete(c.from_user.id)
    await edit(c.message, f"GitHub disconnected. The saved token has been deleted from this bot's database.{FOOTER}",
        keyboard([[("🔐 CONNECT GITHUB","connect")], [("🏠 MAIN MENU","home")]]))
    await c.answer()

@dp.callback_query(F.data == "repos")
async def repos(c: CallbackQuery):
    g = await need_account(c)
    if not g: return
    try:
        rs = await g.repos()
        rows = [[(("🔒 " if r["private"] else "🌍 ") + r["name"])[:60], f"repo:{r['owner']['login']}:{r['name']}"] for r in rs[:35]]
        rows = [[x] for x in rows]
        rows += [[("➕ CREATE REPOSITORY","create")], [("🔄 REFRESH","repos")], [("🏠 MAIN MENU","home")]]
        await edit(c.message, f"{HEADER}\n\n📁 <b>YOUR REPOSITORIES</b>\nSelect a repository.{FOOTER}", keyboard(rows))
        await c.answer()
    except Exception as e: await c.answer(str(e)[:190], show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("repo:"))
async def repo_open(c: CallbackQuery):
    g = await need_account(c)
    if not g: return
    _, owner, name = c.data.split(":",2)
    try:
        r = await g.repo(owner,name)
        await edit(c.message, f"{HEADER}\n\n📁 <b>{html.escape(r['full_name'])}</b>\nDescription: {html.escape(r.get('description') or '—')}\nVisibility: {'Private' if r['private'] else 'Public'}\nDefault branch: {html.escape(r['default_branch'])}\nFiles: use Browse Files below.\n{html.escape(r['html_url'])}{FOOTER}",
            keyboard([[("📤 UPLOAD ZIP PROJECT",f"uploadto:{owner}:{name}")],
                      [("📄 UPLOAD SINGLE FILE",f"singleto:{owner}:{name}")],
                      [("📂 BROWSE FILES",f"browse:{owner}:{name}")],
                      [("📥 DOWNLOAD ZIP",f"getzip:{owner}:{name}")],
                      [("🔗 COPY REPOSITORY URL",f"url:{owner}:{name}")],
                      [("✏️ EDIT DESCRIPTION",f"edit:{owner}:{name}")],
                      [("🗑️ DELETE REPOSITORY",f"del:{owner}:{name}")],
                      [("↩️ REPOSITORIES","repos"),("🏠 HOME","home")]]))
        await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data == "create")
async def create_start(c: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(Flow.repo_name)
    await edit(c.message, f"{HEADER}\n\n➕ <b>CREATE REPOSITORY</b>\nSend the repository name.{FOOTER}",
               keyboard([[("CANCEL","home")]])); await c.answer()

@dp.message(Flow.repo_name)
async def create_name(m: Message, state: FSMContext):
    name=(m.text or "").strip()
    if not name or len(name)>100 or any(ch in name for ch in ("/","\\"," ")):
        await m.answer("Enter a repository name (up to 100 characters, no spaces or slashes)."); return
    await state.update_data(name=name); await state.set_state(Flow.repo_desc)
    await m.answer("Add a description, or type /skip.")

@dp.message(Flow.repo_desc)
async def create_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if (m.text or "").strip().lower()=="/skip" else (m.text or "")[:350])
    await state.set_state(Flow.repo_visibility)
    await m.answer("Choose visibility:",reply_markup=keyboard([[("🌍 PUBLIC","vis:public"),("🔒 PRIVATE","vis:private")]]))

@dp.callback_query(F.data.startswith("vis:"),Flow.repo_visibility)
async def create_vis(c: CallbackQuery,state:FSMContext):
    await state.update_data(private=c.data.endswith("private")); await state.set_state(Flow.repo_readme)
    await edit(c.message,"Initialize README?",keyboard([[("✅ YES","readme:yes"),("❌ NO","readme:no")]])); await c.answer()

@dp.callback_query(F.data.startswith("readme:"),Flow.repo_readme)
async def create_readme(c:CallbackQuery,state:FSMContext):
    await state.update_data(readme=c.data.endswith("yes")); await state.set_state(Flow.repo_gitignore)
    await edit(c.message,"Add a .gitignore template? You can skip it.",keyboard([
        [("⏭️ SKIP","ignore:skip")],[("🐍 Python","ignore:Python"),("🟨 Node","ignore:Node")],
        [("☕ Java","ignore:Java"),("🌐 Go","ignore:Go")]])); await c.answer()

@dp.callback_query(F.data.startswith("ignore:"),Flow.repo_gitignore)
async def create_finish(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); ignore=c.data.split(":")[1]
    g=github(c.from_user.id)
    if not g: await c.answer("Connect GitHub first.",show_alert=True); return
    try:
        r=await g.create(d["name"],d.get("desc",""),d.get("private",True),d.get("readme",True),None if ignore=="skip" else ignore)
        await state.clear()
        await edit(c.message,f"✅ <b>REPOSITORY CREATED</b>\n\nName: {html.escape(r['name'])}\nVisibility: {'Private' if r['private'] else 'Public'}\nREADME: {'Yes' if d.get('readme') else 'No'}\nURL: {html.escape(r['html_url'])}{FOOTER}",
            keyboard([[("📤 UPLOAD PROJECT",f"uploadto:{r['owner']['login']}:{r['name']}")],[("📁 MY REPOSITORIES","repos")],[("🏠 MAIN MENU","home")]]))
        await c.answer("Repository created.")
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data == "upload")
async def upload_start(c:CallbackQuery,state:FSMContext):
    await state.clear()
    await edit(c.message,f"{HEADER}\n\nWhat would you like to upload?{FOOTER}",
        keyboard([[("📦 ZIP PROJECT","zip:start")],[("📄 SINGLE FILE","single:start")],[("↩️ MAIN MENU","home")]])); await c.answer()

@dp.callback_query(F.data == "zip:start")
async def zip_start(c:CallbackQuery,state:FSMContext):
    await state.set_state(Flow.zip_upload); await state.update_data(destination=None)
    await edit(c.message,"Send your project ZIP. The bot preserves its folders and filenames and filters common secret files."); await c.answer()

@dp.callback_query(F.data.startswith("uploadto:"))
async def zip_to_repo(c:CallbackQuery,state:FSMContext):
    _,owner,name=c.data.split(":",2)
    await state.set_state(Flow.zip_upload); await state.update_data(destination=(owner,name))
    await edit(c.message,f"Send the ZIP project for <b>{html.escape(owner)}/{html.escape(name)}</b>."); await c.answer()

@dp.message(Flow.zip_upload,F.document)
async def zip_received(m:Message,state:FSMContext):
    doc=m.document
    if not (doc.file_name or "").lower().endswith(".zip"): await m.answer("Please send a .zip file."); return
    if doc.file_size and doc.file_size>MAX_TELEGRAM_FILE_BYTES: await m.answer("ZIP exceeds the configured Telegram download limit."); return
    f=await bot.get_file(doc.file_id); stream=await bot.download_file(f.file_path); raw=stream.read()
    try: files=parse_zip(raw)
    except Exception as e: await m.answer(f"Upload cancelled: {html.escape(str(e))}"); return
    await state.update_data(files={p:b.hex() for p,b in files.items()})
    d=await state.get_data()
    if d.get("destination"):
        await upload_zip_to(m,state,*d["destination"]); return
    g=github(m.from_user.id)
    if not g: await state.clear(); await m.answer("Connect GitHub first."); return
    try:
        rs=await g.repos()
        rows=[[(("🔒 " if r["private"] else "🌍 ")+r["name"])[:55],f"zipdest:{r['owner']['login']}:{r['name']}"] for r in rs[:35]]
        rows=[[x] for x in rows]+[[("➕ CREATE NEW REPOSITORY","create")],[("❌ CANCEL","home")]]
        await m.answer(f"Found {len(files)} safe files. Choose the destination repository:",reply_markup=keyboard(rows))
    finally: await g.close()

async def upload_zip_to(m,state,owner,name):
    d=await state.get_data(); files={p:bytes.fromhex(v) for p,v in d.get("files",{}).items()}
    if not files: await m.answer("No project files are waiting."); await state.clear(); return
    status=await m.answer(f"⏳ Uploading {len(files)} files to {owner}/{name}...")
    g=github(m.from_user.id)
    if not g: await status.edit_text("Connect GitHub first."); await state.clear(); return
    try:
        r=await g.repo(owner,name); branch=r["default_branch"]
        await g.upload_tree(owner,name,branch,files,"Upload project via XCORE GIT")
        await status.edit_text(f"✅ <b>PROJECT UPLOADED</b>\n\nRepository: {html.escape(owner)}/{html.escape(name)}\nFiles: {len(files)}\nBranch: {html.escape(branch)}\nURL: https://github.com/{owner}/{name}{FOOTER}",
            reply_markup=keyboard([[("📂 OPEN REPOSITORY",f"repo:{owner}:{name}")],[("🏠 MAIN MENU","home")]]))
        await state.clear()
    except Exception as e: await status.edit_text(f"❌ Upload failed: {html.escape(str(e))}")
    finally: await g.close()

@dp.callback_query(F.data.startswith("zipdest:"))
async def zip_dest(c:CallbackQuery,state:FSMContext):
    _,owner,name=c.data.split(":",2); await c.answer("Uploading...")
    await upload_zip_to(c.message,state,owner,name)

@dp.callback_query(F.data == "single:start")
async def single_start(c:CallbackQuery,state:FSMContext):
    await state.set_state(Flow.single_upload); await state.update_data(destination=None)
    await edit(c.message,"Send one file as a Telegram document."); await c.answer()

@dp.callback_query(F.data.startswith("singleto:"))
async def single_to(c:CallbackQuery,state:FSMContext):
    _,owner,name=c.data.split(":",2); await state.set_state(Flow.single_upload); await state.update_data(destination=(owner,name))
    await edit(c.message,f"Send one file for <b>{html.escape(owner)}/{html.escape(name)}</b>."); await c.answer()

@dp.message(Flow.single_upload,F.document)
async def single_received(m:Message,state:FSMContext):
    doc=m.document
    if doc.file_size and doc.file_size>MAX_SINGLE_FILE_BYTES: await m.answer("File is too large for single-file upload."); return
    f=await bot.get_file(doc.file_id); stream=await bot.download_file(f.file_path); data=stream.read()
    d=await state.get_data()
    if d.get("destination"): owner,name=d["destination"]; await single_upload_do(m,state,owner,name,doc.file_name,data)
    else:
        await state.update_data(single_name=doc.file_name,single_data=data.hex())
        g=github(m.from_user.id)
        if not g: await m.answer("Connect GitHub first."); return
        try:
            rs=await g.repos()
            rows=[[(r["name"][:55],f"singledest:{r['owner']['login']}:{r['name']}")] for r in rs[:35]]
            await m.answer("Choose destination repository:",reply_markup=keyboard(rows))
        finally: await g.close()

async def single_upload_do(m,state,owner,name,filename,data):
    if not filename or "/" in filename or "\\" in filename: await m.answer("Invalid filename."); return
    g=github(m.from_user.id)
    try:
        r=await g.repo(owner,name)
        await g.put_file(owner,name,filename,data,r["default_branch"])
        await m.answer(f"✅ Uploaded <code>{html.escape(filename)}</code> to {html.escape(owner)}/{html.escape(name)}.{FOOTER}",main_kb())
        await state.clear()
    except Exception as e: await m.answer(html.escape(str(e)))
    finally: await g.close()

@dp.callback_query(F.data.startswith("singledest:"))
async def single_dest(c:CallbackQuery,state:FSMContext):
    _,owner,name=c.data.split(":",2); d=await state.get_data()
    await c.answer("Uploading...")
    await single_upload_do(c.message,state,owner,name,d["single_name"],bytes.fromhex(d["single_data"]))

@dp.callback_query(F.data == "download")
async def download_list(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    try:
        rs=await g.repos(); rows=[[(("📦 "+r["name"])[:55],f"getzip:{r['owner']['login']}:{r['name']}")] for r in rs[:35]]
        await edit(c.message,"Choose a repository to download as ZIP.",keyboard(rows+[[("🏠 MAIN MENU","home")]])); await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("getzip:"))
async def download_repo(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    _,owner,name=c.data.split(":",2)
    try:
        r=await g.repo(owner,name); data=await g.download(owner,name,r["default_branch"])
        if len(data)>MAX_TELEGRAM_FILE_BYTES: await c.answer("ZIP is too large to send through this bot.",show_alert=True); return
        await c.message.answer_document(BufferedInputFile(data,filename=f"{name}.zip"),caption=f"{owner}/{name}{FOOTER}")
        await c.answer("Archive ready.")
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("browse:"))
async def browse(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    _,owner,name=c.data.split(":",2)
    try:
        r=await g.repo(owner,name); tree=await g.tree(owner,name,r["default_branch"])
        items=[x for x in tree.get("tree",[]) if x["type"]=="blob"][:35]
        rows=[[(("📄 "+x["path"])[:55],f"filedel:{owner}:{name}:{x['sha']}")] for x in items]
        rows += [[("↩️ REPOSITORY",f"repo:{owner}:{name}")]]
        await edit(c.message,f"📂 <b>{html.escape(owner)}/{html.escape(name)}</b>\nSelect a file to delete it (confirmation required).{FOOTER}",keyboard(rows)); await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("filedel:"))
async def filedel_prompt(c:CallbackQuery):
    _,owner,name,sha=c.data.split(":",3)
    await edit(c.message,"Delete this file? This creates a Git commit.",keyboard([[("🗑️ CONFIRM DELETE",f"filedelconfirm:{owner}:{name}:{sha}")],[("↩️ CANCEL",f"browse:{owner}:{name}")]])); await c.answer()

@dp.callback_query(F.data.startswith("filedelconfirm:"))
async def filedel(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    _,owner,name,sha=c.data.split(":",3)
    try:
        r=await g.repo(owner,name); tree=await g.tree(owner,name,r["default_branch"])
        item=next((x for x in tree.get("tree",[]) if x["sha"]==sha and x["type"]=="blob"),None)
        if not item: await c.answer("File not found.",show_alert=True); return
        await g.delete_file(owner,name,item["path"],sha,r["default_branch"])
        await edit(c.message,f"✅ Deleted <code>{html.escape(item['path'])}</code>.{FOOTER}",keyboard([[("📂 BROWSE FILES",f"browse:{owner}:{name}")],[("🏠 HOME","home")]])); await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("url:"))
async def url(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    _,owner,name=c.data.split(":",2)
    try:
        r=await g.repo(owner,name)
        await edit(c.message,f"🔗 Repository URL\n<code>{html.escape(r['html_url'])}</code>{FOOTER}",keyboard([[("↩️ REPOSITORY",f"repo:{owner}:{name}")],[("🏠 HOME","home")]])); await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data.startswith("edit:"))
async def edit_start(c:CallbackQuery,state:FSMContext):
    _,owner,name=c.data.split(":",2); await state.set_state(Flow.edit_desc); await state.update_data(owner=owner,name=name)
    await edit(c.message,"Send the new repository description, or /skip."); await c.answer()

@dp.message(Flow.edit_desc)
async def edit_desc(m:Message,state:FSMContext):
    d=await state.get_data(); desc=(m.text or "").strip()
    if desc.lower()=="/skip": await state.clear(); await m.answer("No changes made.",main_kb()); return
    g=github(m.from_user.id)
    try:
        await g.update(d["owner"],d["name"],description=desc[:350])
        await state.clear(); await m.answer("✅ Repository description updated.",main_kb())
    except Exception as e: await m.answer(html.escape(str(e)))
    finally: await g.close()

@dp.callback_query(F.data.startswith("del:"))
async def delete_prompt(c:CallbackQuery):
    _,owner,name=c.data.split(":",2)
    await edit(c.message,f"⚠️ Permanently delete <b>{html.escape(owner)}/{html.escape(name)}</b>?{FOOTER}",
        keyboard([[("🗑️ YES, DELETE",f"delconfirm:{owner}:{name}")],[("↩️ CANCEL",f"repo:{owner}:{name}")]])); await c.answer()

@dp.callback_query(F.data.startswith("delconfirm:"))
async def delete_repo(c:CallbackQuery):
    g=await need_account(c)
    if not g:return
    _,owner,name=c.data.split(":",2)
    try:
        await g.delete_repo(owner,name)
        await edit(c.message,f"✅ Repository deleted.{FOOTER}",keyboard([[("📁 MY REPOSITORIES","repos")],[("🏠 HOME","home")]])); await c.answer()
    except Exception as e: await c.answer(str(e)[:190],show_alert=True)
    finally: await g.close()

@dp.callback_query(F.data == "settings")
async def settings(c:CallbackQuery):
    await edit(c.message,f"{HEADER}\n\n⚙️ SETTINGS\nAccount connection and security.{FOOTER}",
        keyboard([[("👤 ACCOUNT","account")],[("🔌 DISCONNECT GITHUB","disconnect")],[("🏠 MAIN MENU","home")]])); await c.answer()

async def main():
    log.info("Starting XCORE GIT bot")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
