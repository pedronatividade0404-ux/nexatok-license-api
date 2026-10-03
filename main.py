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
        "version": "0.4.0",
    }
