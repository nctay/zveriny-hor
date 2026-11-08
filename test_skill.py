from email import policy
from email.parser import BytesParser
import io
import json
import ssl
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError
from uuid import UUID
from zipfile import ZipFile

import prepare
from skill import ALIASES, ANIMALS, ROOT, answer

SKILL_ID = str(UUID(int=100))
SOUNDS = {animal: str(UUID(int=i)) for i, animal in enumerate(ANIMALS, 1)}
CONFIG = {"skill_id": SKILL_ID, "sounds": SOUNDS}


def event(command, *, new=False, last=None):
    return {
        "version": "1.0",
        "request": {"command": command, "type": "SimpleUtterance"},
        "session": {"new": new},
        "state": {"session": {"last_animal": last}},
    }


class SkillTests(unittest.TestCase):
    def assert_sound(self, result, animal, *, end=False):
        self.assertEqual(result["version"], "1.0")
        self.assertEqual(result["response"], {
            "text": ANIMALS[animal],
            "tts": f"<speaker audio='dialogs-upload/{SKILL_ID}/{SOUNDS[animal]}.opus'>",
            "end_session": end,
        })

    def test_each_alias_and_audio_file(self):
        self.assertEqual(set(ANIMALS), {path.stem for path in (ROOT / "audio").glob("*.wav")})
        self.assertEqual(len(ANIMALS), 54)
        for alias, animal in ALIASES.items():
            with self.subTest(alias=alias):
                self.assert_sound(answer(event(f"Как говорит {alias}?"), CONFIG), animal)

    def test_friendly_speech_and_distinct_animals(self):
        for command, animal in {
            "А как говорит собачка?": "dog",
            "Включи лай пёсика": "dog",
            "А щеночек как лает?": "puppy",
            "Как говорит детёныш собаки?": "puppy",
            "Покажи, как мяукает кошечка": "cat",
            "Котёнок!": "kitten",
            "Можно звук морской свинки?": "guinea_pig",
            "Как хрюкает свинка?": "pig",
            "Дикая свинья": "boar",
        }.items():
            with self.subTest(command=command):
                self.assert_sound(answer(event(command), CONFIG), animal)

    def test_one_shot_and_open_dialog(self):
        greeting = answer(event("", new=True), CONFIG)
        self.assertFalse(greeting["response"]["end_session"])
        self.assert_sound(answer(event("собака", new=True), CONFIG), "dog", end=True)
        result = answer(event("включить один раз щенка"), CONFIG)
        self.assert_sound(result, "puppy", end=True)
        self.assertNotIn("session_state", result)
        result = answer(event("собака"), CONFIG)
        self.assertEqual(result["session_state"], {"last_animal": "dog"})
        self.assert_sound(answer(event("ещё раз", last="dog"), CONFIG), "dog")

    def test_unknown_ambiguous_or_missing_sound(self):
        for command in ("дельфин", "кошка и собака", "подсобачка", "повтори"):
            with self.subTest(command=command):
                self.assertNotIn("tts", answer(event(command), CONFIG)["response"])
        self.assertNotIn("tts", answer(event("собачка"), {"sounds": {}})["response"])
        self.assertTrue(answer(event("выйти"), CONFIG)["response"]["end_session"])

    def test_archive_entry_point(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "skill.zip"
            prepare.write_archive(output, SKILL_ID, SOUNDS)
            with ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {"skill.py", "animals.txt", "sounds.json"})
                archive.extractall(directory)
            result = subprocess.check_output(
                [sys.executable, "-c", "import json, skill, sys; print(json.dumps(skill.handler(json.load(sys.stdin), None)))"],
                input=json.dumps(event("Покажи, как лает щенок", new=True)),
                text=True,
                cwd=directory,
            )
            self.assert_sound(json.loads(result), "puppy", end=True)

    def test_upload_request_body(self):
        with patch("prepare.urlopen", return_value=io.BytesIO(b'{"sound": {}}')) as urlopen:
            prepare.api_request(prepare.API, "example-token", ("dog.wav", b"RIFFtest"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "OAuth example-token")
        envelope = f"Content-Type: {request.get_header('Content-type')}\r\n\r\n".encode()
        part, = BytesParser(policy=policy.default).parsebytes(envelope + request.data).iter_parts()
        self.assertEqual(part.get_param("name", header="Content-Disposition"), "file")
        self.assertEqual(part.get_filename(), "dog.wav")
        self.assertEqual(part.get_payload(decode=True), b"RIFFtest")

    def test_upload_recovers_after_tls_eof_checking_processing(self):
        item = {"id": SOUNDS["dog"], "originalName": "dog-hash.wav", "isProcessed": False}
        tls_eof = URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
        with patch("prepare.urlopen", side_effect=[
            io.BytesIO(b'{"sounds": []}'),
            io.BytesIO(json.dumps({"sound": item}).encode()),
            tls_eof,
            io.BytesIO(json.dumps({"sound": {**item, "isProcessed": True}}).encode()),
        ]) as urlopen, patch("prepare.time.sleep"):
            sounds = prepare.upload_sounds(SKILL_ID, "token", {"dog": ("dog-hash.wav", b"dog")})
        self.assertEqual(sounds, {"dog": SOUNDS["dog"]})
        self.assertEqual([call.args[0].get_method() for call in urlopen.call_args_list],
                         ["GET", "POST", "GET", "GET"])

    def test_upload_finds_file_if_post_reply_is_lost(self):
        item = {"id": SOUNDS["dog"], "originalName": "dog-hash.wav", "isProcessed": True}
        tls_eof = URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
        with patch("prepare.urlopen", side_effect=[
            io.BytesIO(b'{"sounds": []}'),
            tls_eof,
            io.BytesIO(b'{"sounds": []}'),
            io.BytesIO(json.dumps({"sounds": [item]}).encode()),
        ]) as urlopen, patch("prepare.time.sleep"):
            sounds = prepare.upload_sounds(SKILL_ID, "token", {"dog": ("dog-hash.wav", b"dog")})
        self.assertEqual(sounds, {"dog": SOUNDS["dog"]})
        self.assertEqual([call.args[0].get_method() for call in urlopen.call_args_list],
                         ["GET", "POST", "GET", "GET"])

    def test_upload_reuses_processed_and_pending_files(self):
        dog = {"id": SOUNDS["dog"], "originalName": "dog-hash.wav", "isProcessed": True}
        puppy = {"id": SOUNDS["puppy"], "originalName": "puppy-hash.wav", "isProcessed": False}
        cat = {"id": SOUNDS["cat"], "originalName": "cat-hash.wav", "isProcessed": True}
        audio = {
            "dog": ("dog-hash.wav", b"dog"),
            "puppy": ("puppy-hash.wav", b"puppy"),
            "cat": ("cat-hash.wav", b"cat"),
        }
        with patch("prepare.api_request", side_effect=[
            {"sounds": [dog, puppy]},
            {"sound": cat},
            {"sound": {**puppy, "isProcessed": True}},
        ]) as request, patch("prepare.time.sleep"):
            self.assertEqual(prepare.upload_sounds(SKILL_ID, "token", audio), {
                animal: SOUNDS[animal] for animal in audio
            })
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[1].args[2], audio["cat"])
        self.assertTrue(request.call_args_list[2].args[0].endswith(SOUNDS["puppy"]))
