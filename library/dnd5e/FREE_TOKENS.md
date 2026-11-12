# Токены вне дистрибутива Foundry

В официальном дистрибутиве dnd5e 6.0.6 у 25 монстров нет картинки токена
(в YAML `img: null` и `prototypeToken.texture.src: null`). Поэтому их арт
дозагружен из открытого набора:

* **Источник:** <https://github.com/acodcha/dnd5e-companion>
* **Лицензия собственных материалов:** CC BY 4.0
  (<https://creativecommons.org/licenses/by/4.0/>)
* **Загрузка:** `python3 cli/tools/fetch_free_tokens.py`

Автор набора прямо указывает, что не владеет artwork: изображения были
найдены в открытом доступе, обрезаны и приведены к форме токена. Если вы
публикуете проект, проверьте права на конкретные иллюстрации; для личной
игры в локальной сети это не создаёт проблем.

## Что откуда взято

| Монстр | Файл | Источник в наборе |
|---|---|---|
| Dragon Turtle | `dragon/DragonTurtle.webp` | `tokens/monsters/oceans/monsters/turtle_dragon.png` |
| Ancient Black Dragon | `dragon/BlackDragonAncient.webp` | `tokens/monsters/swamps/dragons_black/dragon_black_ancient.png` |
| Ancient Brass Dragon | `dragon/BrassDragonAncient.webp` | `tokens/monsters/deserts/dragons_brass/dragon_brass_ancient.png` |
| Ancient Bronze Dragon | `dragon/BronzeDragonAncient.webp` | `tokens/monsters/coastal/dragons_bronze/dragon_bronze_ancient.png` |
| Ancient Copper Dragon | `dragon/CopperDragonAncient.webp` | `tokens/monsters/hills/dragons_copper/dragon_copper_ancient.png` |
| Ancient Gold Dragon | `dragon/GoldDragonAncient.webp` | `tokens/monsters/plains/dragons_gold/dragon_gold_ancient.png` |
| Ancient Silver Dragon | `dragon/SilverDragonAncient.webp` | `tokens/monsters/mountains/dragons_silver/dragon_silver_ancient.png` |
| Kraken | `monstrosity/Kraken.webp` | `tokens/monsters/oceans/monsters/kraken.png` |
| Purple Worm | `monstrosity/PurpleWorm.webp` | `tokens/monsters/deserts/monsters/purple_worm.png` |
| Awakened Tree | `plant/AwakenedTree.webp` | `tokens/monsters/forests/monsters/awakened_plant_tree.png` |
| Guardian of Faith | `celestial/GuardianOfFaith.webp` | `tokens/spells/guardian_of_faith_10ft.png` |
| Unseen Servant | `construct/UnseenServant.webp` | `tokens/spells/unseen_servant_5ft.png` |
| Arcane Hand | `construct/ArcaneHand.webp` | `tokens/spells/bigbys_hand_10ft.png` |
| Mage Hand | `construct/MageHand.webp` | `tokens/spells/bigbys_hand_10ft.png` |
| Arcane Eye | `construct/ArcaneEye.webp` | `tokens/spells/summon_aberration_beholderkin_5ft.png` |
| Arcane Sword | `construct/ArcaneSword.webp` | `tokens/spells/spiritual_weapon_5ft_sword.png` |
| Tiny Animated Object | `construct/TinyAnimatedObject.webp` | `tokens/monsters/constructs/instinctive/animated_sword_flying.png` |
| Small Animated Object | `construct/SmallAnimatedObject.webp` | `tokens/monsters/constructs/instinctive/animated_sword_flying.png` |
| Medium Animated Object | `construct/MediumAnimatedObject.webp` | `tokens/monsters/constructs/instinctive/animated_armor.png` |
| Large Animated Object | `construct/LargeAnimatedObject.webp` | `tokens/monsters/constructs/instinctive/animated_rug_of_smothering.png` |
| Huge Animated Object | `construct/HugeAnimatedObject.webp` | `tokens/monsters/constructs/instinctive/animated_rug_of_smothering.png` |
| Avatar of Death | `undead/AvatarOfDeath.webp` | `tokens/monsters/undead/ethereal/wraith.png` |
| Giant Fly | `beast/GiantFly.webp` | `tokens/monsters/swamps/monsters/stirge.png` |
| Dancing Lights (tiny) | `elemental/DancingLightsTiny.webp` | `tokens/monsters/undead/ethereal/will_o_wisp.png` |
| Dancing Lights (medium) | `elemental/DancingLightsMedium.webp` | `tokens/monsters/undead/ethereal/will_o_wisp.png` |

Для имён файлов сохранены правила Foundry: возраст дракона стоит в конце,
`of` не пишется, пояснение в скобках отбрасывается.
