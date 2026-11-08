import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
HELP = "Назови животное. Например, спроси: «Как говорит собачка?» или скажи «Щенок»."


def normalize(text):
    return " ".join(re.findall(r"[а-я]+", text.casefold().replace("ё", "е")))


def read_animals(path):
    animals = {}
    aliases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        animal, name, phrases = (part.strip() for part in line.split("|"))
        single_words, *multiword_phrases = phrases.split(";")
        animals[animal] = name
        for phrase in [name, *single_words.split(), *multiword_phrases]:
            alias = normalize(phrase)
            if aliases.setdefault(alias, animal) != animal:
                raise ValueError(f"В словаре конфликт для «{phrase}»")
    return animals, aliases


ANIMALS, ALIASES = read_animals(ROOT / "animals.txt")
# Longer phrases take priority at the same position, e.g. «морская свинка».
ANIMAL_PATTERN = re.compile(r"\b(?:" + "|".join(sorted(ALIASES, key=len, reverse=True)) + r")\b")


def reply(text, *, tts=None, end=False, last=None):
    response = {"text": text, "end_session": end}
    if tts is not None:
        response["tts"] = tts
    result = {"version": "1.0", "response": response}
    if last and not end:
        result["session_state"] = {"last_animal": last}
    return result


def answer(event, config):
    session = event.get("session", {})
    command = normalize(event.get("request", {}).get("command", ""))
    last = event.get("state", {}).get("session", {}).get("last_animal")
    if command in {"стоп", "хватит", "выход", "выйти", "до свидания"}:
        return reply("До встречи!", end=True)
    if not command or command in {"помощь", "что ты умеешь", "как играть"}:
        return reply(HELP, last=last)

    if command in {"еще раз", "повтори", "повтори пожалуйста", "можно еще раз", "снова"}:
        if last not in ANIMALS:
            return reply("Сначала назови животное. Например, «Кошечка».")
        animal = last
    else:
        matches = {ALIASES[match.group()] for match in ANIMAL_PATTERN.finditer(command)}
        if not matches:
            return reply("Какое животное включить? Можно сказать просто «Собачка».", last=last)
        if len(matches) > 1:
            return reply("Назови одно животное, и я включу его запись.", last=last)
        animal = matches.pop()

    sound_id = config["sounds"].get(animal)
    if not sound_id:
        return reply(f"Запись «{ANIMALS[animal]}» пока недоступна.", last=last)
    audio = f"<speaker audio='dialogs-upload/{config['skill_id']}/{sound_id}.opus'>"
    one_shot = session.get("new") is True or command.startswith(("включи один раз ", "включить один раз "))
    return reply(ANIMALS[animal], tts=audio, end=one_shot, last=animal)


def handler(event, context):
    config = json.loads((ROOT / "sounds.json").read_text(encoding="utf-8"))
    return answer(event, config)
