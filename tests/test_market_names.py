from dataclasses import dataclass

from braindamage.market_names import market_hash_name, parse_market_hash_name


@dataclass
class _FakeSkin:
    name: str
    stattrak: bool = False
    souvenir: bool = False


class TestParseMarketHashName:
    def test_round_trips_normal_skin(self):
        skin = _FakeSkin(name="Galil AR | Acid Dart")
        full = market_hash_name(skin, "Field-Tested")

        base_name, wear_name, stattrak, souvenir = parse_market_hash_name(full)

        assert base_name == "Galil AR | Acid Dart"
        assert wear_name == "Field-Tested"
        assert stattrak is False
        assert souvenir is False

    def test_round_trips_stattrak(self):
        skin = _FakeSkin(name="AK-47 | Redline", stattrak=True)
        full = market_hash_name(skin, "Minimal Wear")

        base_name, wear_name, stattrak, souvenir = parse_market_hash_name(full)

        assert base_name == "AK-47 | Redline"
        assert wear_name == "Minimal Wear"
        assert stattrak is True
        assert souvenir is False

    def test_round_trips_souvenir(self):
        skin = _FakeSkin(name="P250 | Sand Dune", souvenir=True)
        full = market_hash_name(skin, "Battle-Scarred")

        base_name, wear_name, stattrak, souvenir = parse_market_hash_name(full)

        assert base_name == "P250 | Sand Dune"
        assert souvenir is True
        assert stattrak is False

    def test_base_name_ending_in_its_own_parenthetical(self):
        # A real catalog skin: the base name itself ends in "(Dragon King)",
        # which must not be mistaken for the wear suffix.
        skin = _FakeSkin(name="M4A4 | 龍王 (Dragon King)")
        full = market_hash_name(skin, "Field-Tested")
        assert full == "M4A4 | 龍王 (Dragon King) (Field-Tested)"

        base_name, wear_name, stattrak, souvenir = parse_market_hash_name(full)

        assert base_name == "M4A4 | 龍王 (Dragon King)"
        assert wear_name == "Field-Tested"

    def test_no_wear_suffix_returns_none_wear(self):
        base_name, wear_name, stattrak, souvenir = parse_market_hash_name("AK-47 | Redline")

        assert wear_name is None
        assert base_name == "AK-47 | Redline"
