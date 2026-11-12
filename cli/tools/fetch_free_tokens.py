"""Дозагрузить токены для монстров, у которых в Foundry нет картинки.

В дистрибутиве dnd5e у 25 существ ``img`` и ``prototypeToken.texture.src``
пустые (драконы-древние, дракон-черепаха, кракен, фиолетовый червь, призывы
вроде ``Arcane Hand`` и ``Unseen Servant``). Готовых файлов в репозитории
Foundry нет, поэтому берём свободные токены из проекта
``acodcha/dnd5e-companion`` (CC BY 4.0; автор репозитория отмечает, что
само artwork было найдено в открытом доступе).

Имена файлов выбираются так, чтобы ``shared/token_art.py`` находил их по
имени монстра: ``Dragon Turtle`` -> ``dragon/DragonTurtle.webp``,
``Ancient Black Dragon`` -> ``dragon/BlackDragonAncient.webp`` и т.д.

Запуск:
    python3 cli/tools/fetch_free_tokens.py            # только недостающие
    python3 cli/tools/fetch_free_tokens.py --force    # перезаписать
"""
import argparse
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

RAW_BASE = ("https://raw.githubusercontent.com/acodcha/"
            "dnd5e-companion/main/")

# куда положить -> что скачать
SOURCES = {
    "dragon/DragonTurtle.webp":
        "tokens/monsters/oceans/monsters/turtle_dragon.png",
    "dragon/BlackDragonAncient.webp":
        "tokens/monsters/swamps/dragons_black/dragon_black_ancient.png",
    "dragon/BrassDragonAncient.webp":
        "tokens/monsters/deserts/dragons_brass/dragon_brass_ancient.png",
    "dragon/BronzeDragonAncient.webp":
        "tokens/monsters/coastal/dragons_bronze/dragon_bronze_ancient.png",
    "dragon/CopperDragonAncient.webp":
        "tokens/monsters/hills/dragons_copper/dragon_copper_ancient.png",
    "dragon/GoldDragonAncient.webp":
        "tokens/monsters/plains/dragons_gold/dragon_gold_ancient.png",
    "dragon/SilverDragonAncient.webp":
        "tokens/monsters/mountains/dragons_silver/dragon_silver_ancient.png",
    "monstrosity/Kraken.webp":
        "tokens/monsters/oceans/monsters/kraken.png",
    "monstrosity/PurpleWorm.webp":
        "tokens/monsters/deserts/monsters/purple_worm.png",
    "plant/AwakenedTree.webp":
        "tokens/monsters/forests/monsters/awakened_plant_tree.png",
    "celestial/GuardianOfFaith.webp":
        "tokens/spells/guardian_of_faith_10ft.png",
    "construct/UnseenServant.webp":
        "tokens/spells/unseen_servant_5ft.png",
    "construct/ArcaneHand.webp":
        "tokens/spells/bigbys_hand_10ft.png",
    "construct/ArcaneEye.webp":
        "tokens/spells/summon_aberration_beholderkin_5ft.png",
    "construct/ArcaneSword.webp":
        "tokens/spells/spiritual_weapon_5ft_sword.png",
    "construct/MageHand.webp":
        "tokens/spells/bigbys_hand_10ft.png",
    "construct/TinyAnimatedObject.webp":
        "tokens/monsters/constructs/instinctive/animated_sword_flying.png",
    "construct/SmallAnimatedObject.webp":
        "tokens/monsters/constructs/instinctive/animated_sword_flying.png",
    "construct/MediumAnimatedObject.webp":
        "tokens/monsters/constructs/instinctive/animated_armor.png",
    "construct/LargeAnimatedObject.webp":
        "tokens/monsters/constructs/instinctive/animated_rug_of_smothering.png",
    "construct/HugeAnimatedObject.webp":
        "tokens/monsters/constructs/instinctive/animated_rug_of_smothering.png",
    "undead/AvatarOfDeath.webp":
        "tokens/monsters/undead/ethereal/wraith.png",
    "beast/GiantFly.webp":
        "tokens/monsters/swamps/monsters/stirge.png",
    "elemental/DancingLightsTiny.webp":
        "tokens/monsters/undead/ethereal/will_o_wisp.png",
    "elemental/DancingLightsMedium.webp":
        "tokens/monsters/undead/ethereal/will_o_wisp.png",
}


def download(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def to_webp(blob: bytes, target: str):
    from io import BytesIO

    image = Image.open(BytesIO(blob))
    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    image.save(target, "WEBP", quality=90, method=6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", default=os.path.join(
        ROOT, "library", "dnd5e"), help="куда класть webp")
    parser.add_argument("--force", action="store_true",
                        help="перезаписать уже существующие файлы")
    args = parser.parse_args()

    saved = skipped = failed = 0
    for relative, remote in SOURCES.items():
        target = os.path.join(args.library, *relative.split("/"))
        if os.path.isfile(target) and not args.force:
            skipped += 1
            continue
        try:
            blob = download(RAW_BASE + remote)
            to_webp(blob, target)
            saved += 1
            print(f"  + {relative}")
        except Exception as exc:  # сеть/формат — сообщаем и идём дальше
            failed += 1
            print(f"  ! {relative}: {exc}")

    print(f"Сохранено: {saved}, уже было: {skipped}, ошибок: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
