import argparse
from collections import defaultdict
from functools import cache
import re
import time
from uuid import UUID

from skill import ALIASES, ANIMALS, ROOT, normalize

HOME = "https://yandex.ru/iot/"
QUASAR = "https://iot.quasar.yandex.ru"
VOICE = "scenario.trigger.voice"
TEXT_ACTION = "devices.capabilities.quasar.server_action"
PHRASES_PER_SCENARIO = 4


def catalog():
    scenarios = {}
    used_phrases = set()
    for line in (ROOT / "scenario_phrases.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        animal, target, extras, verbs = (field.strip() for field in line.split("|"))
        names = [ANIMALS[animal].lower(), *(name.strip().lower() for name in extras.split(";") if name.strip())]
        if animal in scenarios or any(ALIASES.get(normalize(name)) != animal for name in [target, *names]):
            raise ValueError(f"Проверь названия и падежи для {animal} в scenario_phrases.txt.")
        phrases = [f"как {verb} {name}" for name in names for verb in ("говорит", *verbs.split())]
        keys = {normalize(phrase) for phrase in phrases}
        if len(keys) != len(phrases) or keys & used_phrases:
            raise ValueError(f"Есть повторяющиеся фразы для {animal}.")
        used_phrases.update(keys)
        scenarios[animal] = (
            f"Звериный хор {ANIMALS[animal]}",
            f"Попроси Звериный хор включить один раз {target}",
            phrases,
        )
    if scenarios.keys() != ANIMALS.keys():
        raise ValueError("Нужны правила для каждого животного из animals.txt.")
    return scenarios


def payload(name, command, phrases, station_id):
    capability = {"type": TEXT_ACTION, "state": {"instance": "text_action", "value": command}}
    device = {"id": station_id, "item_type": "device", "capabilities": [capability]}
    item = {"id": station_id, "type": "step.action.item.device", "value": device}
    return {
        "name": name,
        "icon": "home",
        "triggers": [{"trigger": {"type": VOICE, "value": phrase}} for phrase in phrases],
        "steps": [{"type": "scenarios.steps.actions.v2", "parameters": {"items": [item]}}],
    }


def voice_phrases(scenario):
    for entry in scenario.get("triggers", []):
        trigger = entry.get("trigger", entry)
        if trigger.get("type") == VOICE and isinstance(trigger.get("value"), str):
            yield normalize(trigger["value"])


def enabled(scenario):
    return scenario.get("is_active") is not False and scenario.get("enabled") is not False


def has_action(scenario, station_id, command):
    steps = scenario.get("steps", [])
    if len(steps) != 1 or steps[0].get("type") != "scenarios.steps.actions.v2":
        return False
    items = steps[0].get("parameters", {}).get("items", [])
    if len(items) != 1 or items[0].get("type") != "step.action.item.device":
        return False
    device = items[0]["value"]
    capabilities = device.get("capabilities", [])
    if device.get("id") != station_id or len(capabilities) != 1:
        return False
    state = capabilities[0].get("state", {})
    value = state.get("value")
    return (
        capabilities[0].get("type") == TEXT_ACTION
        and state.get("instance") == "text_action"
        and isinstance(value, str)
        and normalize(value) == normalize(command)
    )


def scenario_groups(scenarios):
    for animal, (name, command, phrases) in scenarios.items():
        for offset in range(0, len(phrases), PHRASES_PER_SCENARIO):
            title = f"{name} {offset // PHRASES_PER_SCENARIO + 1}" if offset else name
            yield animal, title, command, phrases[offset:offset + PHRASES_PER_SCENARIO]


def plan_creation(scenarios, existing, details, station_id):
    occupied = defaultdict(list)
    titles = {scenario["name"] for scenario in existing}
    for scenario in existing:
        for phrase in voice_phrases(scenario):
            occupied[phrase].append(scenario)
    planned = []
    for _, name, command, phrases in scenario_groups(scenarios):
        missing = []
        for phrase in phrases:
            owners = occupied[normalize(phrase)]
            if not owners:
                missing.append(phrase)
            for owner in owners:
                current = details(owner["id"])
                if (
                    not enabled(owner) or not enabled(current) or current.get("effective_time")
                    or any(entry.get("filters") for entry in current.get("triggers", []))
                    or not has_action(current, station_id, command)
                ):
                    raise ValueError(f"Фраза «{phrase}» занята сценарием «{owner['name']}» с другой настройкой.")
        if missing:
            if name in titles:
                raise ValueError(f"Сценарий «{name}» уже есть, но часть фраз отсутствует. Проверь его в приложении.")
            planned.append(payload(name, command, missing, station_id))
    return planned


def apply_scenarios(scenarios):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise RuntimeError("Для применения установи Playwright по инструкции в README.md.") from None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(HOME, wait_until="domcontentloaded")
            input("Войди в нужный Яндекс ID в открывшемся Chrome, затем нажми Enter здесь. ")
            csrf = None

            def api(method, path, *, data=None):
                response = context.request.fetch(
                    QUASAR + path,
                    method=method,
                    data=data,
                    headers={"x-csrf-token": csrf} if method == "POST" else None,
                    max_redirects=0,
                    timeout=20_000,
                )
                if not response.ok:
                    raise RuntimeError(f"Quasar отклонил {method} {path} (HTTP {response.status}).")
                try:
                    result = response.json()
                except ValueError:
                    raise RuntimeError(f"Quasar вернул неожиданный ответ на {method} {path}.") from None
                if not isinstance(result, dict) or result.get("status") != "ok":
                    code = (result.get("error_code") or result.get("code")) if isinstance(result, dict) else None
                    reason = f" Код {code}." if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code) else ""
                    raise RuntimeError(f"Quasar не подтвердил {method} {path}.{reason}")
                return result

            households = api("GET", "/m/v3/user/devices")["households"]
            stations = {
                device["id"]: f"{house['name']} / {device['name']}"
                for house in households
                for device in house["all"]
                if device.get("quasar_info")
                and device["quasar_info"].get("platform") not in {"saturn", "mike", "cherry"}
                and device.get("capabilities")
                and not device.get("sharing_info")
            }
            if not stations:
                raise ValueError("В аккаунте нет доступных Станций. Проверь, под каким Яндекс ID ты вошёл.")
            for index, title in enumerate(stations.values(), 1):
                print(f"{index}. {title}")
            choice = int(input("Номер Станции: "))
            if not 1 <= choice <= len(stations):
                raise ValueError("Номер Станции вне списка.")
            station_id = list(stations)[choice - 1]
            existing = api("GET", "/m/user/scenarios")["scenarios"]

            @cache
            def details(scenario_id):
                return api("GET", f"/m/v4/user/scenarios/{UUID(scenario_id)}/edit")["scenario"]

            planned = plan_creation(scenarios, existing, details, station_id)
            if not planned:
                print("Все выбранные фразы уже настроены.")
                return
            for item in planned:
                print(f"{item['name']} — {len(item['triggers'])} новых фраз")
            if input(f"Добавить {len(planned)} сценариев на «{stations[station_id]}»? [да/нет] ").strip().lower() != "да":
                return
            home = context.request.get(HOME, max_redirects=0, timeout=20_000)
            match = re.search(r'"csrfToken2"\s*:\s*"([^"]+)"', home.text()) if home.ok else None
            if not match:
                raise RuntimeError("Не удалось получить CSRF-токен. Проверь вход на страницу Дома с Алисой.")
            csrf = match[1]
            commands = {name: command for _, name, command, _ in scenario_groups(scenarios)}
            for item in planned:
                name = item["name"]
                try:
                    result = api("POST", "/m/v4/user/scenarios/", data=item)
                    scenario = details(result["scenario_id"])
                except RuntimeError as error:
                    raise RuntimeError(f"Сценарий «{name}». {error}") from None
                if (
                    not enabled(scenario)
                    or set(voice_phrases(scenario)) != set(voice_phrases(item))
                    or not has_action(scenario, station_id, commands[name])
                ):
                    raise RuntimeError(f"Quasar сохранил «{name}» с отличиями. Проверь его в приложении.")
                print(f"Создан: {item['name']}", flush=True)
                time.sleep(0.35)
    except Error:
        raise RuntimeError(
            "Chrome или запрос Quasar прервался. Проверь окно входа и сеть. "
            "Повторный запуск сверит уже созданные сценарии."
        ) from None


def main():
    parser = argparse.ArgumentParser(description="Подготовить или добавить голосовые сценарии через Quasar.")
    parser.add_argument("--only", action="append", choices=ANIMALS, metavar="ID", help="Выбрать один звук. Можно повторять.")
    parser.add_argument("--apply", action="store_true", help="Добавить сценарии после входа и подтверждения.")
    args = parser.parse_args()
    try:
        scenarios = {key: value for key, value in catalog().items() if not args.only or key in args.only}
        if args.apply:
            apply_scenarios(scenarios)
        else:
            groups = list(scenario_groups(scenarios))
            print(f"{len(groups)} сценариев, {sum(len(entry[3]) for entry in groups)} фраз.")
            for key, name, command, phrases in groups:
                print(f"\n{name} [{key}]\n  Действие: {command}")
                for phrase in phrases:
                    print(f"  • {phrase}")
    except (ValueError, RuntimeError, EOFError) as error:
        parser.exit(1, f"{error or 'Ввод завершён.'}\n")


if __name__ == "__main__":
    main()
