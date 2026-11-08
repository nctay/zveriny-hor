import unittest

import quasar_scenarios as quasar
from skill import ANIMALS, answer


class QuasarTests(unittest.TestCase):
    def setUp(self):
        self.scenarios = quasar.catalog()
        self.config = {"skill_id": "test", "sounds": {animal: animal for animal in ANIMALS}}

    def test_all_phrases_and_commands_match_the_skill(self):
        self.assertEqual(len(self.scenarios), 54)
        phrases = []
        for animal, (_, action, triggers) in self.scenarios.items():
            phrases.extend(map(quasar.normalize, triggers))
            command = action.removeprefix("Попроси Звериный хор ")
            for phrase in [*triggers, command]:
                with self.subTest(phrase=phrase):
                    result = answer({"request": {"command": phrase}}, self.config)
                    self.assertEqual(result["response"]["text"], ANIMALS[animal])
                    self.assertIn(f"/{animal}.opus", result["response"]["tts"])
        self.assertEqual(len(phrases), len(set(phrases)))

    def test_verbs_cover_pet_names_and_diminutives(self):
        for animal, names, verbs in [
            ("dog", ["собака", "собачка", "пёс", "пёсик"], ["говорит", "лает", "гавкает"]),
            ("puppy", ["щенок", "щеночек"], ["говорит", "лает"]),
            ("cat", ["кот", "кошка", "кошечка"], ["говорит", "мяукает"]),
            ("kitten", ["котёнок", "котёночек"], ["говорит", "мяукает"]),
            ("hen", ["курица", "курочка"], ["говорит", "кудахтает", "кудахчет"]),
        ]:
            for noun in names:
                for verb in verbs:
                    self.assertIn(f"как {verb} {noun}", self.scenarios[animal][2])

    def test_rerun_and_existing_manual_phrase(self):
        dog = {"dog": self.scenarios["dog"]}
        name, command, phrases = dog["dog"]
        manual = quasar.payload("Лай собаки", command, ["как говорит собачка"], "station")
        manual["id"] = "manual"
        summary = {**manual, "triggers": [item["trigger"] for item in manual["triggers"]]}
        planned = quasar.plan_creation(dog, [summary], lambda _: manual, "station")
        self.assertEqual([item["name"] for item in planned], [name, f"{name} 2", f"{name} 3"])
        self.assertEqual(
            {phrase for item in planned for phrase in quasar.voice_phrases(item)},
            set(map(quasar.normalize, phrases)) - {"как говорит собачка"},
        )
        self.assertTrue(all(quasar.has_action(item, "station", command) for item in planned))
        created = [{**item, "id": str(index)} for index, item in enumerate(planned)]
        details = {item["id"]: item for item in [manual, *created]}
        self.assertEqual(quasar.plan_creation(dog, [summary, *created], details.__getitem__, "station"), [])

    def test_scenario_names_and_phrase_limits(self):
        planned = quasar.plan_creation(self.scenarios, [], None, "station")
        self.assertEqual(len({item["name"] for item in planned}), len(planned))
        self.assertEqual(sum(len(item["triggers"]) for item in planned), 319)
        for item in planned:
            self.assertRegex(item["name"], r"^[А-Яа-яЁё0-9 ]+$")
            self.assertLessEqual(len(item["triggers"]), 4)

    def test_refuses_to_steal_existing_phrase(self):
        puppy = {"puppy": self.scenarios["puppy"]}
        name, command, phrases = puppy["puppy"]
        other = quasar.payload(name, command, [phrases[0]], "other-station")
        other["id"] = "other"
        with self.assertRaisesRegex(ValueError, "занята"):
            quasar.plan_creation(puppy, [other], lambda _: other, "station")
