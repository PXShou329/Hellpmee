"""
本地食物清單（v2.1.5：500 道菜版）
─────────────────────────────────────────────────────────────────
資料來源：core/data/taiwan_dishes.json

抽到的菜名會搭配「找附近店」按鈕，讓使用者一鍵接 /吃什麼 流程。
"""
import json
import os
import random


_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "taiwan_dishes.json")


def _load_foods() -> list[str]:
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[food_picker] 讀 JSON 失敗：{e}")
        return ["白飯配醬油"]   # 起碼有 1 個 fallback

    return [f.strip() for f in data.get("foods", []) if f.strip()]


ALL_FOODS: list[str] = _load_foods()


def total_food_count() -> int:
    return len(ALL_FOODS)


def pick_random_food() -> str:
    if not ALL_FOODS:
        return "白飯配醬油"
    return random.choice(ALL_FOODS)


# ════════════════════════════════════════════════════════════════
#  靜態理由模板
# ════════════════════════════════════════════════════════════════
_STATIC_REASONS = [
    "牢大，今天就吃「{food}」吧。本喵已經幫你決定了，不准反悔喵♡",
    "抽到「{food}」了喵！這選擇穩到不行，跟你放棄思考時的人生保底一樣可靠～",
    "嗯哼,「{food}」。本小姐覺得這對你的智商沒有挑戰，剛剛好♡",
    "牢大要吃「{food}」喔。要是不喜歡的話...就忍著吃下去喵♡",
    "「{food}」～本喵覺得很適合你今天這副沒睡飽的樣子。",
    "命運的齒輪轉到了「{food}」，這是本喵給牢大的指示喵♡",
    "選擇障礙直接被處刑:「{food}」。別問為什麼，吃就對了。",
    "今天的吉祥物是「{food}」，會幫你帶來一整天的好運氣喵～♡",
    "「{food}」！本喵的直覺告訴本喵，這就是牢大今天該攝取的能量補給。",
    "本喵抽到了「{food}」，理由很簡單：因為本喵說了算♡",
    "「{food}」，牢大不要再猶豫了。猶豫就會敗北喵！",
    "嗯～「{food}」聽起來就很適合配影集，牢大去吃吧♡",
    "牢大，去吃「{food}」吧。本小姐已經幫你省下五分鐘掙扎時間。",
    "「{food}」。理由是它夠穩，比你最近做的決定還可靠喵♡",
    "今天的指定餐點是「{food}」，本喵覺得你需要這個能量♡",
]


def pick_static_reason(food: str) -> str:
    return random.choice(_STATIC_REASONS).format(food=food)
