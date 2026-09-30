
import base64, os, re
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
BRANCH = os.getenv("GITHUB_BRANCH", "main")
RAW_ROOT = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents"
PLANS = {
    "basic": {"file": "basic.txt", "days": 3, "maxAccounts": 1},
    "pro": {"file": "pro.txt", "days": 7, "maxAccounts": 3},
    "ultimate": {"file": "ultimate.txt", "days": 12, "maxAccounts": 5},
}

app = FastAPI(title="NexaTok License API")

def headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def get_file(name: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{RAW_ROOT}/{name}?ref={BRANCH}", headers=headers())
        if r.status_code != 200:
            raise HTTPException(502, f"GitHub read failed: {r.status_code}")
        j = r.json()
        content = base64.b64decode(j["content"]).decode()
        return content, j["sha"]

async def put_file(name: str, content: str, sha: str, message: str):
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
        "branch": BRANCH,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.put(f"{RAW_ROOT}/{name}", headers=headers(), json=payload)
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"GitHub write failed: {r.status_code}")
        return r.json()

def parse_line(line: str):
    # KEY|STATUS|HWID|ACTIVATED_AT|EXPIRES_AT
    parts = [x.strip() for x in line.split("|")]
    if not parts or not parts[0] or parts[0].startswith("#"):
        return None
    parts += [""] * (5-len(parts))
    return {"key": parts[0], "status": parts[1] or "AVAILABLE", "hwid": parts[2],
            "activated": parts[3], "expires": parts[4]}

def render(item):
    return "|".join([item["key"], item["status"], item["hwid"], item["activated"], item["expires"]])

def find_key(content: str, key: str):
    lines = content.splitlines()
    for i, line in enumerate(lines):
        item = parse_line(line)
        if item and item["key"].lower() == key.lower():
            return i, item
    return None, None

def price_for(plan: str):
    # prices.txt format: basic=19.90 / pro=39.90 / ultimate=69.90
    # It stays in the same private repository and can be edited without rebuilding the app.
    return 0.0

async def load_price(plan: str):
    try:
        content, _ = await get_file("prices.txt")
        for line in content.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k,v=line.split("=",1)
                if k.strip().lower()==plan:
                    return float(v.strip().replace(",", "."))
    except Exception:
        pass
    return 0.0

class LicenseRequest(BaseModel):
    key: str
    hwid: str

def make_license(key, plan, hwid, activated, expires, price):
    return {
        "key": key, "plan": plan, "maxAccounts": PLANS[plan]["maxAccounts"],
        "activatedAt": activated, "expiresAt": expires, "hwid": hwid, "price": price,
    }

async def process(req: LicenseRequest):
    key = req.key.strip()
    hwid = req.hwid.strip()
    if len(key) < 8 or not hwid:
        raise HTTPException(400, "Chave ou HWID inválido.")

    for plan, cfg in PLANS.items():
        content, sha = await get_file(cfg["file"])
        idx, item = find_key(content, key)
        if item is None:
            continue

        now = datetime.now(timezone.utc)
        if item["status"].upper() == "USED":
            if item["hwid"] != hwid:
                raise HTTPException(409, "Esta chave já está vinculada a outro computador.")
            try:
                expires = datetime.fromisoformat(item["expires"].replace("Z","+00:00"))
                activated = datetime.fromisoformat(item["activated"].replace("Z","+00:00"))
            except Exception:
                raise HTTPException(409, "Registro de licença inválido.")
            if expires <= now:
                raise HTTPException(410, "Esta licença expirou.")
            return make_license(key, plan, hwid, item["activated"], item["expires"], await load_price(plan))

        activated = now
        expires = now + timedelta(days=cfg["days"])
        item.update({
            "status":"USED", "hwid":hwid,
            "activated":activated.isoformat().replace("+00:00","Z"),
            "expires":expires.isoformat().replace("+00:00","Z"),
        })
        lines = content.splitlines()
        lines[idx] = render(item)
        await put_file(cfg["file"], "\n".join(lines)+"\n", sha, f"NexaTok: activate {plan} key")
        return make_license(key, plan, hwid, item["activated"], item["expires"], await load_price(plan))

    raise HTTPException(404, "Chave inválida.")

@app.post("/v1/activate")
async def activate(req: LicenseRequest):
    return {"ok": True, "license": await process(req)}

@app.post("/v1/validate")
async def validate(req: LicenseRequest):
    # Validation does not rebind a key. It confirms the same HWID and non-expired record.
    key = req.key.strip()
    for plan, cfg in PLANS.items():
        content, _ = await get_file(cfg["file"])
        _, item = find_key(content, key)
        if item:
            if item["status"].upper() != "USED" or item["hwid"] != req.hwid:
                raise HTTPException(403, "Licença não vinculada a este computador.")
            try:
                expires = datetime.fromisoformat(item["expires"].replace("Z","+00:00"))
            except Exception:
                raise HTTPException(500, "Data de expiração inválida.")
            if expires <= datetime.now(timezone.utc):
                raise HTTPException(410, "Licença expirada.")
            activated = item["activated"]
            return {"ok":True,"license":make_license(key,plan,req.hwid,activated,item["expires"],await load_price(plan))}
    raise HTTPException(404, "Licença não encontrada.")

@app.get("/health")
async def health():
    return {"ok": True, "service": "NexaTok License API"}
