"""
本地飲料清單（v2.1.6：兩層抽樣 + 公平機率）
─────────────────────────────────────────────────────────────────
資料來源：core/data/drink_menus.json

機率設計（規格 IV.2 答案）：
    第一層：從 8 家店中等機率挑一家（每家 1/8 = 12.5%）
    第二層：從該店品項中等機率挑一杯
    顯示：用品項名查回所有有賣的店家

為什麼？JSON 裡清心福全 90 品項、CoCo 32 品項。直接抽品項層
清心福全會佔 25%。兩層抽樣 → 每家店都是 12.5%，公平。
"""
import json
import os
import random
from collections import OrderedDict


_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "drink_menus.json")


def _load_drinks():
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[drink_picker] 讀 JSON 失敗：{e}")
        return OrderedDict(), OrderedDict()

    store_map: "OrderedDict[str, list[str]]" = OrderedDict()
    drink_map: "OrderedDict[str, list[str]]" = OrderedDict()

    for store in data.get("stores", []):
        store_name = store.get("store_name", "未知店家")
        drinks_in_store = []
        for drink in store.get("drinks", []):
            drink = drink.strip()
            if not drink:
                continue
            drinks_in_store.append(drink)
            if drink not in drink_map:
                drink_map[drink] = []
            if store_name not in drink_map[drink]:
                drink_map[drink].append(store_name)
        if drinks_in_store:
            store_map[store_name] = drinks_in_store

    return store_map, drink_map


_STORE_MAP, _DRINK_MAP = _load_drinks()
_STORE_NAMES: list[str] = list(_STORE_MAP.keys())
_DRINK_NAMES: list[str] = list(_DRINK_MAP.keys())


def total_drink_count() -> int:
    return len(_DRINK_NAMES)


def total_store_count() -> int:
    return len(_STORE_NAMES)


def get_distribution() -> dict[str, int]:
    """每店品項數，給 debug 報告用"""
    return {s: len(drinks) for s, drinks in _STORE_MAP.items()}


def pick_random_drink() -> dict:
    """
    兩層抽樣：
    1. 等機率挑一家店
    2. 從該店品項挑一杯
    3. 用品項名查回所有有賣的店清單
    """
    if not _STORE_NAMES:
        return {"name": "白開水", "stores": []}
    store = random.choice(_STORE_NAMES)
    drink_name = random.choice(_STORE_MAP[store])
    all_stores = _DRINK_MAP.get(drink_name, [store])
    return {"name": drink_name, "stores": all_stores}


def format_drink(drink: dict) -> str:
    name   = drink.get("name", "未知飲料")
    stores = drink.get("stores", [])
    if not stores:
        return name
    return f"{name}（{'、'.join(stores)}）"


def primary_search_keyword(drink: dict) -> tuple[str, str]:
    """
    給 /巴豆妖 喝什麼 的「找附近」流程用。
    回傳 (places_query, maps_query)：

    1 家店有 → 品牌獨家 → 兩個 query 都用品牌名（剝括號）
        例：「熟成紅茶（可不可熟成紅茶(KEBUKE)）」
        → ("可不可熟成紅茶", "可不可熟成紅茶")

    多家店都有 → 通用飲料 → 用飲料名 + 上下文字
        例：「珍珠奶茶（五十嵐、清心福全、CoCo 都可）」
        → ("珍珠奶茶 飲料店", "珍珠奶茶 手搖飲")

    沒店家 → fallback
    """
    name   = drink.get("name", "飲料")
    stores = drink.get("stores") or []

    if len(stores) == 1:
        brand = stores[0]
        if "(" in brand:
            brand = brand.split("(")[0].strip()
        return (brand, brand)

    if len(stores) >= 2:
        return (f"{name} 飲料店", f"{name} 手搖飲")

    return (f"{name} 飲料店", f"{name} 飲料")


_STATIC_REASONS = [
    "牢大，本小姐幫你抽到：「{drink}」。糖分跟快樂都給你，今天先別裝健康。",
    "今天喝：「{drink}」。理由：名字聽起來很高級，至少比你剛剛選半天高級。",
    "「{drink}」喵♡ 本小姐覺得這杯穩穩的，喝下去就對了。",
    "抽到「{drink}」了。理由：別問，問就是命運，喵♡",
    "牢大，去買「{drink}」。理由：本小姐昨晚做夢就夢到這杯了。",
    "今天的指定飲品是「{drink}」喵♡ 你今天值得，明天再戒糖。",
    "「{drink}」。理由：這杯能喝到的人都是有品味的牢大♡",
    "本喵幫牢大抽到:「{drink}」喵♡ 喝完今天的續命就靠這杯了。",
    "「{drink}」。本小姐保證 99% 的牢大喝完都會說好喝（剩 1% 是冰太多）。",
    "今天喝「{drink}」。理由：因為你還沒喝過今天的快樂♡",
    "去點「{drink}」，別猶豫。猶豫的話下一杯本喵就幫你抽超難喝的。",
    "「{drink}」喵～本小姐覺得這杯跟你今天的氣質剛好契合。",
]


def pick_static_reason(drink_display: str) -> str:
    return random.choice(_STATIC_REASONS).format(drink=drink_display)
