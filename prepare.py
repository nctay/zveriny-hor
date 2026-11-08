import argparse
from getpass import getpass
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from skill import ANIMALS, ROOT

API = "https://dialogs.yandex.net/api/v1"
RETRY_DELAYS = (1, 2, 4)


class TransientApiError(RuntimeError):
    pass


def get_audio(directory):
    paths = {path.stem: path for path in directory.glob("*.wav")}
    missing = ANIMALS.keys() - paths.keys()
    if missing:
        raise ValueError("Не найдены WAV: " + ", ".join(sorted(missing)))
    audio = {}
    for animal in ANIMALS:
        wav = paths[animal].read_bytes()
        if not 0 < len(wav) <= 5_000_000:
            raise ValueError(f"Некорректный размер {paths[animal].name}")
        digest = sha256(wav).hexdigest()[:16]
        audio[animal] = (f"{animal}-{digest}.wav", wav)
    return audio


def api_request(url, token, upload=None):
    headers = {}
    body = None
    if upload is not None:
        filename, audio = upload
        boundary = uuid4().hex
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        )
        body = prefix.encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    request = Request(url, data=body, headers=headers)
    request.add_unredirected_header("Authorization", f"OAuth {token}")
    delays = (0, *RETRY_DELAYS) if upload is None else (0,)
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            message = f"Яндекс Диалоги вернули HTTP {error.code}."
            if error.code not in {408, 429} and not 500 <= error.code < 600:
                raise RuntimeError(message) from None
        except (OSError, HTTPException, json.JSONDecodeError) as error:
            message = f"Не удалось получить ответ от Яндекс Диалогов: {error}"
    raise TransientApiError(message)


def list_sounds(url, token):
    existing = {}
    for item in api_request(url, token)["sounds"]:
        if not item.get("error"):
            name = item["originalName"]
            if name not in existing or item["isProcessed"]:
                existing[name] = item
    return existing


def upload_sounds(skill_id, token, audio):
    url = f"{API}/skills/{skill_id}/sounds"
    existing = list_sounds(url, token)

    ready = {}
    pending = {}
    for animal, upload in audio.items():
        name, _ = upload
        item = existing.get(name)
        if item is None:
            print(f"Загружаю {animal}.wav", flush=True)
            try:
                item = api_request(url, token, upload)["sound"]
            except TransientApiError as error:
                print(f"Проверяю, приняли ли Диалоги {animal}.wav после обрыва.", flush=True)
                for delay in RETRY_DELAYS:
                    time.sleep(delay)
                    item = list_sounds(url, token).get(name)
                    if item is not None:
                        break
                if item is None:
                    raise RuntimeError(
                        f"{error}\nНе удалось подтвердить загрузку {animal}.wav. "
                        "Повтори команду, чтобы продолжить с принятых файлов."
                    ) from None
        if item.get("error"):
            raise RuntimeError(f"Диалоги не приняли файл {animal}.wav.")
        sound_id = str(UUID(item["id"]))
        if item["isProcessed"]:
            ready[animal] = sound_id
        else:
            pending[animal] = sound_id

    if pending:
        print(f"Проверяю готовность записей: {len(pending)}.", flush=True)
    deadline = time.monotonic() + 120
    while pending:
        if time.monotonic() >= deadline:
            raise RuntimeError("Диалоги ещё обрабатывают записи. Запусти команду повторно.")
        time.sleep(2)
        for animal, sound_id in list(pending.items()):
            item = api_request(f"{url}/{sound_id}", token)["sound"]
            if item.get("error"):
                raise RuntimeError(f"Диалоги не обработали файл {animal}.wav.")
            if item["isProcessed"]:
                ready[animal] = pending.pop(animal)
    return ready


def write_archive(output, skill_id, sounds):
    if set(sounds) != set(ANIMALS):
        raise ValueError("Для сборки нужны идентификаторы всех 54 звуков.")
    with NamedTemporaryFile(dir=output.parent, prefix=".zveriny-hor-", delete=False) as file:
        temporary = Path(file.name)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for filename in ("skill.py", "animals.txt"):
                archive.write(ROOT / filename, filename)
            archive.writestr(
                "sounds.json",
                json.dumps({"skill_id": skill_id, "sounds": sounds}, sort_keys=True),
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Загрузить звуки и собрать ZIP для Cloud Functions.")
    parser.add_argument("skill_id", help="Идентификатор навыка из Яндекс Диалогов")
    args = parser.parse_args()
    try:
        skill_id = str(UUID(args.skill_id))
        audio = get_audio(ROOT / "audio")
        token = getpass("OAuth-токен Яндекс Диалогов: ").strip()
        if not token:
            raise ValueError("Введите токен создателя навыка.")
        sounds = upload_sounds(skill_id, token, audio)
        output = ROOT / "zveriny-hor.zip"
        write_archive(output, skill_id, sounds)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Готово. Загружено или найдено {len(sounds)} звуков. Архив: {output}")


if __name__ == "__main__":
    main()
