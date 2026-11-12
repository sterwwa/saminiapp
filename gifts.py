"""Gift economy for the Telegram Mini App. PostgreSQL-backed; no real-money operations."""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone

from aiohttp import web

MARKET_FEE_PERCENT = 3
MAX_PINNED_GIFTS = 5
CURRENCY = "газявы"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gift_wallets (
    user_id BIGINT PRIMARY KEY,
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS gift_wallet_transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    balance_after BIGINT NOT NULL CHECK (balance_after >= 0),
    type TEXT NOT NULL,
    reference_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS gift_wallet_transactions_user_idx
    ON gift_wallet_transactions(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS gift_catalog (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    animation_url TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    gift_type TEXT NOT NULL CHECK (gift_type IN ('regular', 'collectible')),
    price BIGINT NOT NULL CHECK (price >= 0),
    total_supply BIGINT,
    minted_count BIGINT NOT NULL DEFAULT 0,
    rarity_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    attributes_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((gift_type = 'regular' AND total_supply IS NULL) OR
           (gift_type = 'collectible' AND total_supply > 0))
);
CREATE TABLE IF NOT EXISTS gift_instances (
    id BIGSERIAL PRIMARY KEY,
    gift_id BIGINT NOT NULL REFERENCES gift_catalog(id),
    owner_id BIGINT NOT NULL,
    serial_number BIGINT,
    rarity TEXT NOT NULL DEFAULT 'common',
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    anonymous BOOLEAN NOT NULL DEFAULT FALSE,
    message TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((serial_number IS NULL) OR serial_number > 0),
    UNIQUE(gift_id, serial_number)
);
CREATE INDEX IF NOT EXISTS gift_instances_owner_idx
    ON gift_instances(owner_id, received_at DESC);
CREATE TABLE IF NOT EXISTS gift_ownership_history (
    id BIGSERIAL PRIMARY KEY,
    gift_instance_id BIGINT NOT NULL REFERENCES gift_instances(id),
    from_user_id BIGINT,
    to_user_id BIGINT NOT NULL,
    operation TEXT NOT NULL,
    price BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS gift_ownership_history_instance_idx
    ON gift_ownership_history(gift_instance_id, created_at DESC);
CREATE TABLE IF NOT EXISTS gift_market_listings (
    id BIGSERIAL PRIMARY KEY,
    gift_instance_id BIGINT NOT NULL REFERENCES gift_instances(id),
    seller_id BIGINT NOT NULL,
    price BIGINT NOT NULL CHECK (price > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'sold', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sold_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS gift_market_active_idx
    ON gift_market_listings(status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS gift_market_one_active_listing_idx
    ON gift_market_listings(gift_instance_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS gift_idempotency (
    user_id BIGINT NOT NULL,
    operation TEXT NOT NULL,
    request_key TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(user_id, operation, request_key)
);
CREATE TABLE IF NOT EXISTS gift_audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS gift_system_wallet (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0)
);
INSERT INTO gift_system_wallet(id, balance) VALUES (1, 0) ON CONFLICT(id) DO NOTHING;
"""

def _json_obj(value, fallback=None):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else (fallback or {})
        except ValueError:
            return fallback or {}
    return fallback or {}


def _uid(request: web.Request) -> int:
    return int((request.get("user") or {}).get("id", 0))

def _pool(request: web.Request):
    pool = request.app.get("gift_pool")
    if pool is None:
        raise web.HTTPServiceUnavailable(text="Система подарков временно не настроена: отсутствует DATABASE_URL")
    return pool

async def initialize(app: web.Application) -> None:
    url = os.getenv("DATABASE_URL", "").strip()
    app["gift_pool"] = None
    if not url:
        return
    import asyncpg
    app["gift_pool"] = await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=15)
    async with app["gift_pool"].acquire() as conn:
        await conn.execute(SCHEMA)

async def close_pool(app: web.Application) -> None:
    pool = app.get("gift_pool")
    if pool is not None:
        await pool.close()

async def _body(request: web.Request) -> dict:
    try:
        value = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Ожидается JSON")
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="Некорректный JSON")
    return value

def _key(request: web.Request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise web.HTTPBadRequest(text="Передайте уникальный Idempotency-Key (8–128 символов)")
    return key

async def _wallet(conn, uid: int) -> int:
    await conn.execute("INSERT INTO gift_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    row = await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1", uid)
    return int(row["balance"])

async def _lock_idempotency(conn, uid: int, op: str, key: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{uid}:{op}:{key}")


async def _cached(conn, uid: int, op: str, key: str):
    row = await conn.fetchrow(
        "SELECT response FROM gift_idempotency WHERE user_id=$1 AND operation=$2 AND request_key=$3",
        uid, op, key)
    return _json_obj(row["response"]) if row else None

async def _remember(conn, uid: int, op: str, key: str, response: dict) -> None:
    await conn.execute(
        "INSERT INTO gift_idempotency(user_id,operation,request_key,response) VALUES($1,$2,$3,$4::jsonb)",
        uid, op, key, json.dumps(response, ensure_ascii=False))

async def api_catalog(request: web.Request) -> web.Response:
    sort=request.query.get("sort","price_asc")
    order={"price_asc":"price ASC,id ASC","price_desc":"price DESC,id ASC","rarity":"(rarity_config->>'default') ASC,price ASC,id ASC"}.get(sort,"price ASC,id ASC")
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("""SELECT id,name,description,image_url,animation_url,category,gift_type,
            price,total_supply,minted_count, GREATEST(COALESCE(total_supply,0)-minted_count,0) AS remaining
            FROM gift_catalog WHERE is_active=TRUE ORDER BY """+order)
    return web.json_response({"gifts":[dict(r) for r in rows], "currency":CURRENCY})

async def api_gift_detail(request: web.Request) -> web.Response:
    try: gift_id = int(request.match_info["gift_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID")
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow("""SELECT id,name,description,image_url,animation_url,category,gift_type,
            price,total_supply,minted_count, GREATEST(COALESCE(total_supply,0)-minted_count,0) AS remaining
            FROM gift_catalog WHERE id=$1 AND is_active=TRUE""", gift_id)
    if not row: raise web.HTTPNotFound(text="Подарок не найден")
    return web.json_response(dict(row))

async def api_wallet(request: web.Request) -> web.Response:
    uid = _uid(request)
    async with _pool(request).acquire() as conn:
        balance = await _wallet(conn, uid)
    return web.json_response({"balance":balance,"currency":CURRENCY})

async def api_wallet_transactions(request: web.Request) -> web.Response:
    uid = _uid(request)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("""SELECT id,amount,balance_after,type,reference_id,created_at
            FROM gift_wallet_transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 50""", uid)
    return web.json_response({"transactions":[dict(r) for r in rows]})

async def api_user_gifts(request: web.Request) -> web.Response:
    try: target = int(request.match_info["user_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID пользователя")
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("""SELECT i.id,i.gift_id,i.owner_id,i.serial_number,i.rarity,i.attributes,
            i.is_hidden,i.is_pinned,i.anonymous,i.message,i.received_at,
            g.name,g.description,g.image_url,g.animation_url,g.gift_type,g.category,l.id AS listing_id
            FROM gift_instances i JOIN gift_catalog g ON g.id=i.gift_id
            LEFT JOIN gift_market_listings l ON l.gift_instance_id=i.id AND l.status='active'
            WHERE i.owner_id=$1 AND (i.is_hidden=FALSE OR $2=TRUE)
            ORDER BY i.is_pinned DESC,i.received_at DESC""", target, _uid(request)==target)
    return web.json_response({"gifts":[dict(r) for r in rows]})

async def api_purchase(request: web.Request) -> web.Response:
    uid, key = _uid(request), _key(request)
    body = await _body(request)
    gift_id, recipient = body.get("gift_id"), body.get("recipient_id", uid)
    anonymous, message = body.get("anonymous", False), body.get("message", "")
    if type(gift_id) is not int or type(recipient) is not int or recipient <= 0:
        raise web.HTTPBadRequest(text="Некорректный подарок или получатель")
    if type(anonymous) is not bool or not isinstance(message, str) or len(message)>300:
        raise web.HTTPBadRequest(text="Некорректная подпись")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            cached = await _cached(conn,uid,"purchase",key)
            if cached: return web.json_response(cached)
            gift = await conn.fetchrow("SELECT * FROM gift_catalog WHERE id=$1 AND is_active=TRUE FOR UPDATE",gift_id)
            if not gift: raise web.HTTPNotFound(text="Подарок не найден")
            if gift["gift_type"]=="collectible" and gift["minted_count"]>=gift["total_supply"]:
                raise web.HTTPConflict(text="Тираж подарка закончился")
            price=int(gift["price"])
            await conn.execute("INSERT INTO gift_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING",uid)
            wallet=await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1 FOR UPDATE",uid)
            if wallet["balance"]<price: raise web.HTTPConflict(text="Недостаточно газяв")
            balance=wallet["balance"]-price
            await conn.execute("UPDATE gift_wallets SET balance=$2,updated_at=NOW() WHERE user_id=$1",uid,balance)
            await conn.execute("""INSERT INTO gift_wallet_transactions(user_id,amount,balance_after,type)
                VALUES($1,$2,$3,'gift_purchase')""",uid,-price,balance)
            serial=None; rarity="common"; attrs={}
            if gift["gift_type"]=="collectible":
                serial=int(gift["minted_count"])+1
                rarity_config=_json_obj(gift["rarity_config"])
                rarity_weights=rarity_config.get("weights") if isinstance(rarity_config,dict) else None
                if isinstance(rarity_weights,dict) and rarity_weights:
                    choices=[(str(name),max(0,int(weight))) for name,weight in rarity_weights.items()]
                    total=sum(weight for _,weight in choices)
                    pick=secrets.randbelow(total) if total else 0
                    rarity="common"
                    if total:
                        for name,weight in choices:
                            if pick < weight:
                                rarity=name[:40]
                                break
                            pick-=weight
                else:
                    rarity=str(rarity_config.get("default","common"))[:40] if isinstance(rarity_config,dict) else "common"
                attr_config=_json_obj(gift["attributes_config"])
                attrs={k:(secrets.choice(v) if isinstance(v,list) and v else v) for k,v in attr_config.items()}
                await conn.execute("UPDATE gift_catalog SET minted_count=minted_count+1 WHERE id=$1",gift_id)
            instance=await conn.fetchrow("""INSERT INTO gift_instances
                (gift_id,owner_id,serial_number,rarity,attributes,anonymous,message)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7) RETURNING id""",
                gift_id,recipient,serial,rarity,json.dumps(attrs),anonymous,message)
            instance_id=int(instance["id"])
            await conn.execute("""INSERT INTO gift_ownership_history
                (gift_instance_id,from_user_id,to_user_id,operation,price)
                VALUES($1,NULL,$2,'purchase',$3)""",instance_id,recipient,price)
            await conn.execute("""INSERT INTO gift_audit_log(actor_id,action,target_id,details)
                VALUES($1,'gift_purchase',$2,$3::jsonb)""",uid,instance_id,json.dumps({"recipient_id":recipient,"price":price}))
            response={"success":True,"gift_instance_id":instance_id,"price":price,"currency":CURRENCY,"balance":balance}
            await _remember(conn,uid,"purchase",key,response)
    return web.json_response(response)

async def api_send_gift(request: web.Request) -> web.Response:
    uid,key=_uid(request),_key(request); body=await _body(request)
    instance_id,recipient=body.get("gift_instance_id"),body.get("recipient_id")
    anonymous,message=body.get("anonymous",False),body.get("message","")
    if type(instance_id) is not int or type(recipient) is not int or recipient<=0 or type(anonymous) is not bool or not isinstance(message,str) or len(message)>300:
        raise web.HTTPBadRequest(text="Некорректные данные передачи")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await _lock_idempotency(conn,uid,"send",key)
            cached=await _cached(conn,uid,"send",key)
            if cached:return web.json_response(cached)
            gift=await conn.fetchrow("SELECT * FROM gift_instances WHERE id=$1 FOR UPDATE",instance_id)
            if not gift or gift["owner_id"]!=uid:raise web.HTTPNotFound(text="Подарок не найден у вас")
            if await conn.fetchval("SELECT 1 FROM gift_market_listings WHERE gift_instance_id=$1 AND status='active'",instance_id):
                raise web.HTTPConflict(text="Сначала снимите подарок с маркета")
            await conn.execute("UPDATE gift_instances SET owner_id=$2,anonymous=$3,message=$4,is_pinned=FALSE WHERE id=$1",instance_id,recipient,anonymous,message)
            await conn.execute("""INSERT INTO gift_ownership_history(gift_instance_id,from_user_id,to_user_id,operation)
                VALUES($1,$2,$3,'transfer')""",instance_id,uid,recipient)
            await conn.execute("""INSERT INTO gift_audit_log(actor_id,action,target_id,details)
                VALUES($1,'gift_transfer',$2,$3::jsonb)""",uid,instance_id,json.dumps({"recipient_id":recipient,"anonymous":anonymous}))
            response={"success":True,"gift_instance_id":instance_id,"owner_id":recipient}
            await _remember(conn,uid,"send",key,response)
    return web.json_response(response)

async def api_gift_visibility(request: web.Request) -> web.Response:
    uid=_uid(request); body=await _body(request); instance_id=body.get("gift_instance_id"); field=request.match_info["field"]
    if type(instance_id) is not int or type(body.get("value")) is not bool:raise web.HTTPBadRequest(text="Некорректные данные")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow("SELECT id FROM gift_instances WHERE id=$1 AND owner_id=$2 FOR UPDATE",instance_id,uid)
            if not row:raise web.HTTPNotFound(text="Подарок не найден")
            if field=="pin" and body["value"]:
                await conn.execute("SELECT pg_advisory_xact_lock($1)",uid)
                count=await conn.fetchval("SELECT COUNT(*) FROM gift_instances WHERE owner_id=$1 AND is_pinned=TRUE",uid)
                if count>=MAX_PINNED_GIFTS:raise web.HTTPConflict(text=f"Можно закрепить не более {MAX_PINNED_GIFTS} подарков")
            column="is_pinned" if field=="pin" else "is_hidden"
            await conn.execute(f"UPDATE gift_instances SET {column}=$2 WHERE id=$1",instance_id,body["value"])
    return web.json_response({"success":True,"value":body["value"]})

async def api_gift_history(request: web.Request) -> web.Response:
    try: instance_id=int(request.match_info["instance_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID подарка")
    uid=_uid(request)
    async with _pool(request).acquire() as conn:
        owner=await conn.fetchval("SELECT owner_id FROM gift_instances WHERE id=$1",instance_id)
        if owner is None: raise web.HTTPNotFound(text="Подарок не найден")
        if int(owner)!=uid and uid not in request.app["deps"].admin_ids:
            raise web.HTTPForbidden(text="История доступна владельцу подарка")
        rows=await conn.fetch("""SELECT from_user_id,to_user_id,operation,price,created_at
            FROM gift_ownership_history WHERE gift_instance_id=$1 ORDER BY id DESC LIMIT 100""",instance_id)
    return web.json_response({"history":[dict(row) for row in rows]})


async def api_market(request: web.Request) -> web.Response:
    async with _pool(request).acquire() as conn:
        rows=await conn.fetch("""SELECT l.id,l.gift_instance_id,l.seller_id,l.price,l.created_at,
            i.serial_number,i.rarity,i.attributes,g.name,g.image_url,g.animation_url,g.gift_type
            FROM gift_market_listings l JOIN gift_instances i ON i.id=l.gift_instance_id
            JOIN gift_catalog g ON g.id=i.gift_id WHERE l.status='active'
            ORDER BY l.created_at DESC LIMIT 100""")
    return web.json_response({"listings":[dict(r) for r in rows],"fee_percent":MARKET_FEE_PERCENT,"currency":CURRENCY})

async def api_market_list(request: web.Request) -> web.Response:
    uid,key=_uid(request),_key(request);body=await _body(request)
    instance_id,price=body.get("gift_instance_id"),body.get("price")
    if type(instance_id) is not int or type(price) is not int or price<=0:raise web.HTTPBadRequest(text="Укажите подарок и цену")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await _lock_idempotency(conn,uid,"market_list",key)
            cached=await _cached(conn,uid,"market_list",key)
            if cached:return web.json_response(cached)
            gift=await conn.fetchrow("""SELECT i.id,i.owner_id,g.gift_type FROM gift_instances i
                JOIN gift_catalog g ON g.id=i.gift_id WHERE i.id=$1 FOR UPDATE OF i""",instance_id)
            if not gift or gift["owner_id"]!=uid:raise web.HTTPNotFound(text="Подарок не найден")
            if gift["gift_type"]!="collectible":raise web.HTTPBadRequest(text="На маркет можно выставлять только коллекционные подарки")
            listing=await conn.fetchrow("""INSERT INTO gift_market_listings(gift_instance_id,seller_id,price)
                VALUES($1,$2,$3) RETURNING id""",instance_id,uid,price)
            await conn.execute("UPDATE gift_instances SET is_pinned=FALSE WHERE id=$1",instance_id)
            await conn.execute("""INSERT INTO gift_audit_log(actor_id,action,target_id,details)
                VALUES($1,'market_list',$2,$3::jsonb)""",uid,instance_id,json.dumps({"price":price,"listing_id":int(listing["id"])}))
            response={"success":True,"listing_id":int(listing["id"])}
            await _remember(conn,uid,"market_list",key,response)
    return web.json_response(response)

async def api_market_buy(request: web.Request) -> web.Response:
    uid,key=_uid(request),_key(request);body=await _body(request);listing_id=body.get("listing_id")
    if type(listing_id) is not int:raise web.HTTPBadRequest(text="Некорректный лот")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await _lock_idempotency(conn,uid,"market_buy",key)
            cached=await _cached(conn,uid,"market_buy",key)
            if cached:return web.json_response(cached)
            listing=await conn.fetchrow("SELECT * FROM gift_market_listings WHERE id=$1 FOR UPDATE",listing_id)
            if not listing or listing["status"]!="active":raise web.HTTPConflict(text="Лот уже недоступен")
            if listing["seller_id"]==uid:raise web.HTTPBadRequest(text="Нельзя купить собственный подарок")
            price=int(listing["price"])
            seller_id=int(listing["seller_id"])
            for wallet_id in sorted({uid,seller_id}):
                await conn.execute("INSERT INTO gift_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING",wallet_id)
                await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1 FOR UPDATE",wallet_id)
            buyer=await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1",uid)
            seller=await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1",seller_id)
            if buyer["balance"]<price:raise web.HTTPConflict(text="Недостаточно газяв")
            fee=price*MARKET_FEE_PERCENT//100; payout=price-fee
            buyer_balance=int(buyer["balance"])-price;seller_balance=int(seller["balance"])+payout
            await conn.execute("UPDATE gift_wallets SET balance=$2,updated_at=NOW() WHERE user_id=$1",uid,buyer_balance)
            await conn.execute("UPDATE gift_wallets SET balance=$2,updated_at=NOW() WHERE user_id=$1",seller_id,seller_balance)
            await conn.execute("UPDATE gift_system_wallet SET balance=balance+$1 WHERE id=1",fee)
            await conn.execute("""INSERT INTO gift_wallet_transactions(user_id,amount,balance_after,type,reference_id)
                VALUES($1,$2,$3,'market_purchase',$4),($5,$6,$7,'market_sale',$4)""",
                uid,-price,buyer_balance,listing_id,seller_id,payout,seller_balance)
            await conn.execute("UPDATE gift_market_listings SET status='sold',sold_at=NOW() WHERE id=$1",listing_id)
            await conn.execute("UPDATE gift_instances SET owner_id=$2,is_pinned=FALSE WHERE id=$1",listing["gift_instance_id"],uid)
            await conn.execute("""INSERT INTO gift_ownership_history(gift_instance_id,from_user_id,to_user_id,operation,price)
                VALUES($1,$2,$3,'market_sale',$4)""",listing["gift_instance_id"],seller_id,uid,price)
            await conn.execute("""INSERT INTO gift_audit_log(actor_id,action,target_id,details)
                VALUES($1,'market_sale',$2,$3::jsonb)""",uid,listing["gift_instance_id"],json.dumps({"seller_id":seller_id,"price":price,"fee":fee}))
            response={"success":True,"listing_id":listing_id,"price":price,"fee":fee,"seller_received":payout,"balance":buyer_balance}
            await _remember(conn,uid,"market_buy",key,response)
    return web.json_response(response)

async def api_market_cancel(request: web.Request) -> web.Response:
    uid=_uid(request);body=await _body(request);listing_id=body.get("listing_id")
    if type(listing_id) is not int:raise web.HTTPBadRequest(text="Некорректный лот")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow("SELECT * FROM gift_market_listings WHERE id=$1 FOR UPDATE",listing_id)
            if not row or row["seller_id"]!=uid or row["status"]!="active":raise web.HTTPNotFound(text="Активный лот не найден")
            await conn.execute("UPDATE gift_market_listings SET status='cancelled' WHERE id=$1",listing_id)
    return web.json_response({"success":True})

async def api_admin_wallet_credit(request: web.Request) -> web.Response:
    d=request.app["deps"];uid=_uid(request)
    if uid not in d.admin_ids:return web.json_response({"error":"forbidden"},status=403)
    key=_key(request);body=await _body(request);target,amount=body.get("user_id"),body.get("amount")
    if type(target) is not int or target<=0 or type(amount) is not int or not 1<=amount<=1000000:
        raise web.HTTPBadRequest(text="Укажите пользователя и сумму от 1 до 1000000")
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await _lock_idempotency(conn,uid,"admin_credit",key)
            cached=await _cached(conn,uid,"admin_credit",key)
            if cached:return web.json_response(cached)
            await conn.execute("INSERT INTO gift_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING",target)
            row=await conn.fetchrow("SELECT balance FROM gift_wallets WHERE user_id=$1 FOR UPDATE",target)
            balance=int(row["balance"])+amount
            await conn.execute("UPDATE gift_wallets SET balance=$2,updated_at=NOW() WHERE user_id=$1",target,balance)
            await conn.execute("""INSERT INTO gift_wallet_transactions(user_id,amount,balance_after,type)
                VALUES($1,$2,$3,'admin_credit')""",target,amount,balance)
            await conn.execute("""INSERT INTO gift_audit_log(actor_id,action,target_id,details)
                VALUES($1,'wallet_credit',$2,$3::jsonb)""",uid,target,json.dumps({"amount":amount}))
            response={"success":True,"user_id":target,"amount":amount,"balance":balance,"currency":CURRENCY}
            await _remember(conn,uid,"admin_credit",key,response)
    return web.json_response(response)

async def api_admin_create_gift(request: web.Request) -> web.Response:
    d=request.app["deps"]
    if _uid(request) not in d.admin_ids:return web.json_response({"error":"forbidden"},status=403)
    body=await _body(request)
    name,kind,price=body.get("name"),body.get("gift_type"),body.get("price")
    if not isinstance(name,str) or not name.strip() or len(name)>100 or kind not in {"regular","collectible"} or type(price) is not int or price<0:
        raise web.HTTPBadRequest(text="Некорректные название, тип или цена")
    supply=body.get("total_supply")
    rarity_config=body.get("rarity_config", {})
    attributes_config=body.get("attributes_config", {})
    if not isinstance(rarity_config,dict) or not isinstance(attributes_config,dict):
        raise web.HTTPBadRequest(text="Некорректные настройки редкости или атрибутов")
    if kind=="collectible" and (type(supply) is not int or not 1<=supply<=1000000):
        raise web.HTTPBadRequest(text="Для коллекционного подарка укажите тираж")
    if kind=="regular":supply=None
    async with _pool(request).acquire() as conn:
        row=await conn.fetchrow("""INSERT INTO gift_catalog(name,description,image_url,animation_url,category,gift_type,price,total_supply)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            name.strip(),str(body.get("description",""))[:500],str(body.get("image_url",""))[:1000],
            str(body.get("animation_url",""))[:1000],str(body.get("category","other"))[:40],kind,price,supply)
        await conn.execute("UPDATE gift_catalog SET rarity_config=$2::jsonb,attributes_config=$3::jsonb WHERE id=$1",
            row["id"],json.dumps(rarity_config),json.dumps(attributes_config))
    return web.json_response({"success":True,"gift_id":int(row["id"])},status=201)
