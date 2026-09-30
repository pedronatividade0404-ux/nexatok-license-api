class RedeemRequest(BaseModel):
    currentKey: str
    newKey: str
    hwid: str


@app.post("/v1/redeem")
async def redeem(req: RedeemRequest):
    current_key = req.currentKey.strip()
    new_key = req.newKey.strip()
    hwid = req.hwid.strip()

    if not current_key or not new_key or not hwid:
        raise HTTPException(400, "Dados de renovação inválidos.")

    if current_key.lower() == new_key.lower():
        raise HTTPException(400, "A nova chave deve ser diferente da chave atual.")

    # ---------------------------------------------------------
    # 1. Localizar e validar a licença atual
    # ---------------------------------------------------------
    current_plan = None
    current_item = None

    for plan, cfg in PLANS.items():
        content, _ = await get_file(cfg["file"])
        _, item = find_key(content, current_key)

        if item:
            current_plan = plan
            current_item = item
            break

    if current_item is None:
        raise HTTPException(404, "Licença atual não encontrada.")

    if current_item["status"].upper() != "USED":
        raise HTTPException(409, "A licença atual ainda não foi ativada.")

    if current_item["hwid"] != hwid:
        raise HTTPException(
            403,
            "A licença atual pertence a outro computador."
        )

    try:
        current_expires = datetime.fromisoformat(
            current_item["expires"].replace("Z", "+00:00")
        )
    except Exception:
        raise HTTPException(500, "Data de expiração da licença atual inválida.")

    # ---------------------------------------------------------
    # 2. Procurar a NOVA chave
    # ---------------------------------------------------------
    new_plan = None
    new_cfg = None
    new_content = None
    new_sha = None
    new_idx = None
    new_item = None

    for plan, cfg in PLANS.items():
        content, sha = await get_file(cfg["file"])
        idx, item = find_key(content, new_key)

        if item:
            new_plan = plan
            new_cfg = cfg
            new_content = content
            new_sha = sha
            new_idx = idx
            new_item = item
            break

    if new_item is None:
        raise HTTPException(404, "Nova chave inválida.")

    if new_item["status"].upper() == "USED":
        raise HTTPException(409, "Esta nova chave já foi utilizada.")

    # ---------------------------------------------------------
    # 3. Calcular nova expiração
    # ---------------------------------------------------------
    now = datetime.now(timezone.utc)

    # Se a licença ainda estiver ativa, soma a partir da
    # expiração atual. Se já expirou, começa a partir de agora.
    base_date = current_expires if current_expires > now else now

    new_expires = base_date + timedelta(days=new_cfg["days"])

    activated = now.isoformat().replace("+00:00", "Z")
    expires = new_expires.isoformat().replace("+00:00", "Z")

    # ---------------------------------------------------------
    # 4. Marcar a nova chave como usada
    # ---------------------------------------------------------
    new_item.update({
        "status": "USED",
        "hwid": hwid,
        "activated": activated,
        "expires": expires,
    })

    lines = new_content.splitlines()
    lines[new_idx] = render(new_item)

    await put_file(
        new_cfg["file"],
        "\n".join(lines) + "\n",
        new_sha,
        f"NexaTok: redeem {new_plan} key"
    )

    # ---------------------------------------------------------
    # 5. Retornar a NOVA licença
    # ---------------------------------------------------------
    return {
        "ok": True,
        "license": make_license(
            new_key,
            new_plan,
            hwid,
            activated,
            expires,
            await load_price(new_plan)
        ),
        "renewal": {
            "previousKey": current_key,
            "addedDays": new_cfg["days"],
            "previousPlan": current_plan,
            "newPlan": new_plan,
        }
    }
