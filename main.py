import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import AliasChoices, BaseModel, Field


# CONFIGURAÇÃO

GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
BRANCH = os.getenv("GITHUB_BRANCH", "main")

REPO_URL = (
    f"https://api.github.com/repos/"
    f"{GITHUB_OWNER}/{GITHUB_REPO}"
)

PLANS = {
    "basic": {
        "file": "basic.txt",
        "days": 3,
        "maxAccounts": 1,
    },
    "pro": {
        "file": "pro.txt",
        "days": 7,
        "maxAccounts": 3,
    },
    "ultimate": {
        "file": "ultimate.txt",
        "days": 12,
        "maxAccounts": 5,
    },
}

app = FastAPI(
    title="NexaTok License API",
    version="0.3.0",
)

redeem_lock = asyncio.Lock()


# MODELOS

class LicenseRequest(BaseModel):
    key: str
    hwid: str


class RedeemRequest(BaseModel):
    currentKey: str
    newKey: str = Field(
        validation_alias=AliasChoices("newKey", "key")
    )
    hwid: str


# ACESSO AO GITHUB

def headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def github(method, path, payload=None, params=None):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                REPO_URL + path,
                headers=headers(),
                json=payload,
                params=params,
            )
    except httpx.HTTPError:
        raise HTTPException(
            502,
            "Não foi possível conectar ao GitHub.",
        )

    if response.status_code not in (200, 201):
        if response.status_code in (409, 422):
            raise HTTPException(
                409,
                "O repositório mudou durante a operação. "
                "Tente novamente.",
            )
        raise HTTPException(
            502,
            f"Falha no GitHub: HTTP {response.status_code}.",
        )

    try:
        return response.json()
    except ValueError:
        raise HTTPException(
            502,
            "O GitHub retornou uma resposta inválida.",
        )


async def get_file(name, ref=None):
    data = await github(
        "GET",
        f"/contents/{name}",
        params={"ref": ref or BRANCH},
    )

    try:
        content = base64.b64decode(
            data["content"]
        ).decode("utf-8")
        return content, data["sha"]
    except (KeyError, ValueError, UnicodeDecodeError):
        raise HTTPException(
            502,
            f"Não foi possível ler {name}.",
        )


async def put_file(name, content, sha, message):
    return await github(
        "PUT",
        f"/contents/{name}",
        payload={
            "message": message,
            "content": base64.b64encode(
                content.encode("utf-8")
            ).decode("ascii"),
            "sha": sha,
            "branch": BRANCH,
        },
    )


async def atomic_files(files, parent):
    """
    Atualiza a chave antiga e a nova em um único commit.
    Evita uma renovação parcialmente gravada.
    """
    commit = await github(
        "GET",
        f"/git/commits/{parent}",
    )

    tree = await github(
        "POST",
        "/git/trees",
        payload={
            "base_tree": commit["tree"]["sha"],
            "tree": [
                {
                    "path": name,
                    "mode": "100644",
                    "type": "blob",
                    "content": content,
                }
                for name, content in files.items()
            ],
        },
    )

    new_commit = await github(
        "POST",
        "/git/commits",
        payload={
            "message": "NexaTok: renovar licença",
            "tree": tree["sha"],
            "parents": [parent],
        },
    )

    await github(
        "PATCH",
        f"/git/refs/heads/{quote(BRANCH, safe='/')}",
        payload={
            "sha": new_commit["sha"],
            "force": False,
        },
    )


# FUNÇÕES AUXILIARES

def parse_line(line):
    # KEY|STATUS|HWID|ACTIVATED_AT|EXPIRES_AT
    parts = [value.strip() for value in line.split("|")]

    if not parts[0] or parts[0].startswith("#"):
        return None

    parts += [""] * max(0, 5 - len(parts))

    return {
        "key": parts[0],
        "status": parts[1] or "AVAILABLE",
        "hwid": parts[2],
        "activated": parts[3],
        "expires": parts[4],
    }


def render(item):
    return "|".join([
        item["key"],
        item["status"],
        item["hwid"],
        item["activated"],
        item["expires"],
    ])


def find_key(content, key):
    for index, line in enumerate(content.splitlines()):
        item = parse_line(line)
        if item and item["key"].lower() == key.lower():
            return index, item

    return None, None


def parse_date(value):
    try:
        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            409,
            "Registro de licença com data inválida.",
        )


def utc_string(value):
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


async def load_price(plan):
    try:
        content, _ = await get_file("prices.txt")

        for line in content.splitlines():
            if line.lstrip().startswith("#") or "=" not in line:
                continue

            name, value = line.split("=", 1)
            if name.strip().lower() == plan:
                return float(value.strip().replace(",", "."))

    except (HTTPException, ValueError):
        pass

    return 0.0


async def make_license(plan, item):
    return {
        "key": item["key"],
        "plan": plan,
        "maxAccounts": PLANS[plan]["maxAccounts"],
        "activatedAt": item["activated"],
        "expiresAt": item["expires"],
        "hwid": item["hwid"],
        "price": await load_price(plan),
    }


def request_values(req):
    key = req.key.strip()
    hwid = req.hwid.strip()

    if len(key) < 8 or not hwid:
        raise HTTPException(
            400,
            "Chave ou HWID inválido.",
        )

    return key, hwid


def check_used(item, hwid):
    if item["status"].upper() != "USED":
        raise HTTPException(
            403,
            "Licença não ativada ou já substituída.",
        )

    if item["hwid"] != hwid:
        raise HTTPException(
            403,
            "Licença não vinculada a este computador.",
        )

    parse_date(item["activated"])
    expires = parse_date(item["expires"])

    if expires <= datetime.now(timezone.utc):
        raise HTTPException(
            410,
            "Esta licença expirou.",
        )


# ATIVAÇÃO

@app.post("/v1/activate")
async def activate(req: LicenseRequest):
    key, hwid = request_values(req)

    for plan, cfg in PLANS.items():
        content, sha = await get_file(cfg["file"])
        index, item = find_key(content, key)

        if item is None:
            continue

        status = item["status"].upper()

        if status == "USED":
            check_used(item, hwid)
            return {
                "ok": True,
                "license": await make_license(plan, item),
            }

        if status != "AVAILABLE":
            raise HTTPException(
                409,
                "Esta chave não está disponível para ativação.",
            )

        now = datetime.now(timezone.utc)

        item.update({
            "status": "USED",
            "hwid": hwid,
            "activated": utc_string(now),
            "expires": utc_string(
                now + timedelta(days=cfg["days"])
            ),
        })

        lines = content.splitlines()
        lines[index] = render(item)

        await put_file(
            cfg["file"],
            "\n".join(lines) + "\n",
            sha,
            f"NexaTok: ativar licença {plan}",
        )

        return {
            "ok": True,
            "license": await make_license(plan, item),
        }

    raise HTTPException(404, "Chave inválida.")


# VALIDAÇÃO

@app.post("/v1/validate")
async def validate(req: LicenseRequest):
    key, hwid = request_values(req)

    for plan, cfg in PLANS.items():
        content, _ = await get_file(cfg["file"])
        _, item = find_key(content, key)

        if item is None:
            continue

        check_used(item, hwid)

        return {
            "ok": True,
            "license": await make_license(plan, item),
        }

    raise HTTPException(
        404,
        "Licença não encontrada.",
    )


# RESGATE / RENOVAÇÃO

@app.post("/v1/redeem")
async def redeem(req: RedeemRequest):
    current_key = req.currentKey.strip()
    new_key = req.newKey.strip()
    hwid = req.hwid.strip()

    if not current_key or len(new_key) < 8 or not hwid:
        raise HTTPException(
            400,
            "Dados de renovação inválidos.",
        )

    if current_key.lower() == new_key.lower():
        raise HTTPException(
            400,
            "A nova chave deve ser diferente da chave atual.",
        )

    async with redeem_lock:
        ref = await github(
            "GET",
            f"/git/ref/heads/{quote(BRANCH, safe='/')}",
        )
        parent = ref["object"]["sha"]

        snapshots = {}
        current_found = None
        new_found = None

        # Todos os arquivos são lidos do mesmo commit.
        for plan, cfg in PLANS.items():
            content, _ = await get_file(
                cfg["file"],
                ref=parent,
            )
            snapshots[cfg["file"]] = content.splitlines()

            old_index, old_item = find_key(content, current_key)
            new_index, new_item = find_key(content, new_key)

            if old_item is not None:
                current_found = (
                    plan, cfg, old_index, old_item
                )

            if new_item is not None:
                new_found = (
                    plan, cfg, new_index, new_item
                )

        if current_found is None:
            raise HTTPException(
                404,
                "Licença atual não encontrada.",
            )

        old_plan, old_cfg, old_index, old_item = current_found

        if old_item["status"].upper() != "USED":
            raise HTTPException(
                409,
                "A licença atual não está ativada "
                "ou já foi substituída.",
            )

        if old_item["hwid"] != hwid:
            raise HTTPException(
                403,
                "A licença atual pertence a outro computador.",
            )

        if new_found is None:
            raise HTTPException(
                404,
                "Nova chave inválida.",
            )

        new_plan, new_cfg, new_index, new_item = new_found

        if new_item["status"].upper() != "AVAILABLE":
            raise HTTPException(
                409,
                "Esta nova chave já foi utilizada "
                "ou não está disponível.",
            )

        now = datetime.now(timezone.utc)
        previous_expires = parse_date(old_item["expires"])

        # Preserva o tempo restante; se expirou, começa agora.
        base_date = max(previous_expires, now)
        expires = base_date + timedelta(days=new_cfg["days"])

        new_item.update({
            "status": "USED",
            "hwid": hwid,
            "activated": utc_string(now),
            "expires": utc_string(expires),
        })

        # O tempo da chave antiga foi transferido para a nova.
        old_item["status"] = "REDEEMED"

        snapshots[old_cfg["file"]][old_index] = render(old_item)
        snapshots[new_cfg["file"]][new_index] = render(new_item)

        changed_files = {
            name: "\n".join(snapshots[name]) + "\n"
            for name in {
                old_cfg["file"],
                new_cfg["file"],
            }
        }

        await atomic_files(changed_files, parent)

        return {
            "ok": True,
            "license": await make_license(new_plan, new_item),
            "renewal": {
                "previousKey": old_item["key"],
                "previousPlan": old_plan,
                "newKey": new_item["key"],
                "newPlan": new_plan,
                "addedDays": new_cfg["days"],
                "previousExpiresAt": utc_string(previous_expires),
                "expiresAt": new_item["expires"],
            },
        }


# HEALTH CHECK

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "NexaTok License API",
        "version": "0.3.0",
    }
