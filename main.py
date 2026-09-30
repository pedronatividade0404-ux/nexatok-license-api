import base64
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


# ============================================================
# CONFIGURAÇÃO
# ============================================================

GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

BRANCH = os.getenv("GITHUB_BRANCH", "main")

RAW_ROOT = (
    f"https://api.github.com/repos/"
    f"{GITHUB_OWNER}/{GITHUB_REPO}/contents"
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
    version="0.2.0",
)


# ============================================================
# MODELOS
# ============================================================

class LicenseRequest(BaseModel):
    key: str
    hwid: str


class RedeemRequest(BaseModel):
    currentKey: str
    newKey: str
    hwid: str


# ============================================================
# GITHUB
# ============================================================

def headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_file(name: str):
    async with httpx.AsyncClient(timeout=20) as client:

        response = await client.get(
            f"{RAW_ROOT}/{name}?ref={BRANCH}",
            headers=headers(),
        )

        if response.status_code != 200:
            raise HTTPException(
                502,
                f"GitHub read failed: {response.status_code}",
            )

        data = response.json()

        try:
            content = base64.b64decode(
                data["content"]
            ).decode("utf-8")
        except Exception:
            raise HTTPException(
                502,
                f"Não foi possível ler {name}.",
            )

        return content, data["sha"]


async def put_file(
    name: str,
    content: str,
    sha: str,
    message: str,
):
    payload = {
        "message": message,
        "content": base64.b64encode(
            content.encode("utf-8")
        ).decode(),
        "sha": sha,
        "branch": BRANCH,
    }

    async with httpx.AsyncClient(timeout=20) as client:

        response = await client.put(
            f"{RAW_ROOT}/{name}",
            headers=headers(),
            json=payload,
        )

        if response.status_code not in (200, 201):
            raise HTTPException(
                502,
                f"GitHub write failed: {response.status_code}",
            )

        return response.json()


# ============================================================
# FUNÇÕES DAS CHAVES
# ============================================================

def parse_line(line: str):
    """
    Formato:

    KEY|STATUS|HWID|ACTIVATED_AT|EXPIRES_AT
    """

    parts = [
        part.strip()
        for part in line.split("|")
    ]

    if not parts:
        return None

    if not parts[0]:
        return None

    if parts[0].startswith("#"):
        return None

    parts += [""] * (5 - len(parts))

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


def find_key(content: str, key: str):
    lines = content.splitlines()

    for index, line in enumerate(lines):

        item = parse_line(line)

        if (
            item
            and item["key"].lower()
            == key.lower()
        ):
            return index, item

    return None, None


# ============================================================
# PREÇOS
# ============================================================

async def load_price(plan: str):
    try:

        content, _ = await get_file(
            "prices.txt"
        )

        for line in content.splitlines():

            if "=" not in line:
                continue

            if line.lstrip().startswith("#"):
                continue

            key, value = line.split("=", 1)

            if key.strip().lower() == plan.lower():

                return float(
                    value
                    .strip()
                    .replace(",", ".")
                )

    except Exception:
        pass

    return 0.0


# ============================================================
# CRIAR OBJETO DE LICENÇA
# ============================================================

def make_license(
    key,
    plan,
    hwid,
    activated,
    expires,
    price,
):
    return {
        "key": key,
        "plan": plan,
        "maxAccounts": PLANS[plan]["maxAccounts"],
        "activatedAt": activated,
        "expiresAt": expires,
        "hwid": hwid,
        "price": price,
    }


# ============================================================
# ATIVAÇÃO
# ============================================================

async def process(req: LicenseRequest):

    key = req.key.strip()
    hwid = req.hwid.strip()

    if len(key) < 8 or not hwid:
        raise HTTPException(
            400,
            "Chave ou HWID inválido.",
        )

    for plan, cfg in PLANS.items():

        content, sha = await get_file(
            cfg["file"]
        )

        idx, item = find_key(
            content,
            key,
        )

        if item is None:
            continue

        now = datetime.now(
            timezone.utc
        )

        # ----------------------------------------------------
        # CHAVE JÁ UTILIZADA
        # ----------------------------------------------------

        if item["status"].upper() == "USED":

            if item["hwid"] != hwid:
                raise HTTPException(
                    409,
                    "Esta chave já está vinculada "
                    "a outro computador.",
                )

            try:

                expires = datetime.fromisoformat(
                    item["expires"].replace(
                        "Z",
                        "+00:00",
                    )
                )

                datetime.fromisoformat(
                    item["activated"].replace(
                        "Z",
                        "+00:00",
                    )
                )

            except Exception:

                raise HTTPException(
                    409,
                    "Registro de licença inválido.",
                )

            if expires <= now:
                raise HTTPException(
                    410,
                    "Esta licença expirou.",
                )

            return make_license(
                key,
                plan,
                hwid,
                item["activated"],
                item["expires"],
                await load_price(plan),
            )

        # ----------------------------------------------------
        # PRIMEIRA ATIVAÇÃO
        # ----------------------------------------------------

        activated = now

        expires = (
            now
            + timedelta(
                days=cfg["days"]
            )
        )

        item.update({
            "status": "USED",
            "hwid": hwid,
            "activated": (
                activated
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            ),
            "expires": (
                expires
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            ),
        })

        lines = content.splitlines()

        lines[idx] = render(item)

        await put_file(
            cfg["file"],
            "\n".join(lines) + "\n",
            sha,
            f"NexaTok: activate {plan} key",
        )

        return make_license(
            key,
            plan,
            hwid,
            item["activated"],
            item["expires"],
            await load_price(plan),
        )

    raise HTTPException(
        404,
        "Chave inválida.",
    )


# ============================================================
# POST /v1/activate
# ============================================================

@app.post("/v1/activate")
async def activate(req: LicenseRequest):

    license_data = await process(req)

    return {
        "ok": True,
        "license": license_data,
    }


# ============================================================
# POST /v1/validate
# ============================================================

@app.post("/v1/validate")
async def validate(req: LicenseRequest):

    key = req.key.strip()
    hwid = req.hwid.strip()

    if not key or not hwid:
        raise HTTPException(
            400,
            "Chave ou HWID inválido.",
        )

    for plan, cfg in PLANS.items():

        content, _ = await get_file(
            cfg["file"]
        )

        _, item = find_key(
            content,
            key,
        )

        if item is None:
            continue

        if item["status"].upper() != "USED":
            raise HTTPException(
                403,
                "Licença ainda não foi ativada.",
            )

        if item["hwid"] != hwid:
            raise HTTPException(
                403,
                "Licença não vinculada "
                "a este computador.",
            )

        try:

            expires = datetime.fromisoformat(
                item["expires"].replace(
                    "Z",
                    "+00:00",
                )
            )

        except Exception:

            raise HTTPException(
                500,
                "Data de expiração inválida.",
            )

        if expires <= datetime.now(
            timezone.utc
        ):
            raise HTTPException(
                410,
                "Licença expirada.",
            )

        return {
            "ok": True,
            "license": make_license(
                key,
                plan,
                hwid,
                item["activated"],
                item["expires"],
                await load_price(plan),
            ),
        }

    raise HTTPException(
        404,
        "Licença não encontrada.",
    )


# ============================================================
# POST /v1/redeem
# ============================================================

@app.post("/v1/redeem")
async def redeem(req: RedeemRequest):

    current_key = req.currentKey.strip()
    new_key = req.newKey.strip()
    hwid = req.hwid.strip()

    # --------------------------------------------------------
    # VALIDAÇÃO INICIAL
    # --------------------------------------------------------

    if not current_key:
        raise HTTPException(
            400,
            "Chave atual não informada.",
        )

    if not new_key:
        raise HTTPException(
            400,
            "Nova chave não informada.",
        )

    if not hwid:
        raise HTTPException(
            400,
            "HWID não informado.",
        )

    if current_key.lower() == new_key.lower():
        raise HTTPException(
            400,
            "A nova chave deve ser diferente "
            "da chave atual.",
        )

    # --------------------------------------------------------
    # 1. PROCURAR LICENÇA ATUAL
    # --------------------------------------------------------

    current_plan = None
    current_item = None

    for plan, cfg in PLANS.items():

        content, _ = await get_file(
            cfg["file"]
        )

        _, item = find_key(
            content,
            current_key,
        )

        if item is not None:

            current_plan = plan
            current_item = item

            break

    if current_item is None:
        raise HTTPException(
            404,
            "Licença atual não encontrada.",
        )

    # --------------------------------------------------------
    # 2. VERIFICAR LICENÇA ATUAL
    # --------------------------------------------------------

    if (
        current_item["status"].upper()
        != "USED"
    ):
        raise HTTPException(
            409,
            "A licença atual ainda "
            "não foi ativada.",
        )

    if current_item["hwid"] != hwid:
        raise HTTPException(
            403,
            "A licença atual pertence "
            "a outro computador.",
        )

    try:

        current_expires = (
            datetime.fromisoformat(
                current_item[
                    "expires"
                ].replace(
                    "Z",
                    "+00:00",
                )
            )
        )

    except Exception:

        raise HTTPException(
            500,
            "Data de expiração da "
            "licença atual inválida.",
        )

    # --------------------------------------------------------
    # 3. PROCURAR NOVA CHAVE
    # --------------------------------------------------------

    new_plan = None
    new_cfg = None
    new_content = None
    new_sha = None
    new_idx = None
    new_item = None

    for plan, cfg in PLANS.items():

        content, sha = await get_file(
            cfg["file"]
        )

        idx, item = find_key(
            content,
            new_key,
        )

        if item is not None:

            new_plan = plan
            new_cfg = cfg
            new_content = content
            new_sha = sha
            new_idx = idx
            new_item = item

            break

    if new_item is None:
        raise HTTPException(
            404,
            "Nova chave inválida.",
        )

    # --------------------------------------------------------
    # 4. VERIFICAR SE NOVA CHAVE JÁ FOI USADA
    # --------------------------------------------------------

    if (
        new_item["status"].upper()
        == "USED"
    ):
        raise HTTPException(
            409,
            "Esta nova chave já foi utilizada.",
        )

    # --------------------------------------------------------
    # 5. CALCULAR NOVA DATA
    # --------------------------------------------------------

    now = datetime.now(
        timezone.utc
    )

    # Se ainda existe tempo restante,
    # preservamos esse tempo.
    #
    # Se já expirou, começa de agora.

    if current_expires > now:
        base_date = current_expires
    else:
        base_date = now

    added_days = new_cfg["days"]

    new_expires = (
        base_date
        + timedelta(
            days=added_days
        )
    )

    activated_string = (
        now
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    expires_string = (
        new_expires
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    # --------------------------------------------------------
    # 6. MARCAR NOVA CHAVE COMO USED
    # --------------------------------------------------------

    new_item.update({
        "status": "USED",
        "hwid": hwid,
        "activated": activated_string,
        "expires": expires_string,
    })

    lines = new_content.splitlines()

    lines[new_idx] = render(
        new_item
    )

    await put_file(
        new_cfg["file"],
        "\n".join(lines) + "\n",
        new_sha,
        f"NexaTok: redeem {new_plan} key",
    )

    # --------------------------------------------------------
    # 7. RETORNAR NOVA LICENÇA
    # --------------------------------------------------------

    return {
        "ok": True,

        "license": make_license(
            new_key,
            new_plan,
            hwid,
            activated_string,
            expires_string,
            await load_price(
                new_plan
            ),
        ),

        "renewal": {
            "previousKey": current_key,
            "previousPlan": current_plan,
            "newKey": new_key,
            "newPlan": new_plan,
            "addedDays": added_days,
            "previousExpiresAt": (
                current_item["expires"]
            ),
            "expiresAt": expires_string,
        },
    }


# ============================================================
# GET /health
# ============================================================

@app.get("/health")
async def health():

    return {
        "ok": True,
        "service": "NexaTok License API",
        "version": "0.2.0",
    }
